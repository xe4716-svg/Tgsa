"""One persistent webhook per Bot; no remote calls during prepare or ingress.
Secret envelopes authenticate tenant routing before any tenant database query.
"""
import hashlib,hmac,secrets,uuid
from urllib.parse import urlsplit
from cryptography.fernet import InvalidToken
from app.db import transaction,settings,seal,unseal
from app.bot_credentials import resolve_token
from app.worker import PermanentFailure,UnknownSend,RetryLater

class WebhookRejected(ValueError): pass

def description(row):
 return {k:row[k] for k in ('bot_id','url','status','last_result','last_authenticated_at')}

def destination(bot):
 origin=settings().get('public_origin','')
 parsed=urlsplit(origin)
 if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.port not in (None,443): raise WebhookRejected('请先配置有效的HTTPS公开域名')
 return origin.rstrip('/')+'/webhooks/bots/'+str(bot)

def prepare(tenant,actor,bot):
 target=destination(bot)
 with transaction(tenant) as db:
  if not db.execute("SELECT 1 FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(actor,)).fetchone(): raise WebhookRejected('操作者不属于当前工作区')
  if not resolve_token(tenant,bot): raise WebhookRejected('请先验证并登记机器人凭据')
  db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'webhook:{bot}',))
  old=db.execute('SELECT * FROM tgmate.bot_webhook_registrations WHERE bot_id=%s',(bot,)).fetchone()
  if old:
   if old['url']!=target: raise WebhookRejected('公开地址已变更，需要管理员迁移已有入口')
   return description(old)
  envelope=seal(['wh1',uuid.UUID(str(tenant)).hex,uuid.UUID(str(bot)).hex,secrets.token_hex(4)]).rstrip('=')
  if len(envelope)>256: raise RuntimeError('Webhook envelope exceeds Telegram limit')
  row=db.execute("INSERT INTO tgmate.bot_webhook_registrations(tenant_id,bot_id,url,secret_hash,secret_ciphertext) VALUES(%s,%s,%s,%s,%s) RETURNING *",(tenant,bot,target,hashlib.sha256(envelope.encode()).hexdigest(),seal({'secret':envelope}))).fetchone()
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(%s,%s,'bot.webhook_prepared','telegram_bot',%s)",(tenant,actor,bot))
  return description(row)

def ingress(bot,secret):
 if not isinstance(secret,str) or not 100<=len(secret)<=256: return None
 try:
  data=unseal(secret+'='*((-len(secret))%4))
  if not isinstance(data,list) or len(data)!=4 or data[0]!='wh1' or str(uuid.UUID(data[2]))!=str(bot): return None
  tenant=str(uuid.UUID(data[1]))
 except (InvalidToken,ValueError,TypeError,KeyError): return None
 with transaction(tenant) as db:
  row=db.execute("SELECT w.* FROM tgmate.bot_webhook_registrations w JOIN tgmate.telegram_bots b ON b.tenant_id=w.tenant_id AND b.id=w.bot_id WHERE w.bot_id=%s AND b.status='active' FOR UPDATE OF w",(bot,)).fetchone()
  if not row or row['status'] not in ('registering','uncertain','active') or not hmac.compare_digest(row['secret_hash'],hashlib.sha256(secret.encode()).hexdigest()): return None
  db.execute("UPDATE tgmate.bot_webhook_registrations SET last_authenticated_at=now(),status='active',last_result='authenticated_ingress',updated_at=now() WHERE bot_id=%s",(bot,))
 return {'tenant_id':tenant}

def apply(tenant,actor,bot,allow_replace=False,adapter=None,retry_uncertain=False):
 from app.telegram_adapters import BotBroadcast
 adapter=adapter or BotBroadcast()
 operation=uuid.uuid4()
 with transaction(tenant) as db:
  if not db.execute("SELECT 1 FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(actor,)).fetchone(): raise WebhookRejected('操作者不属于当前工作区')
  row=db.execute('SELECT * FROM tgmate.bot_webhook_registrations WHERE bot_id=%s FOR UPDATE',(bot,)).fetchone()
  if not row: raise WebhookRejected('请先生成入口预览')
  if row['url']!=destination(bot): raise WebhookRejected('公开地址已变更，不能应用旧预览')
  if row['status']!='prepared' and not (row['status']=='uncertain' and retry_uncertain is True): raise WebhookRejected('入口已应用或结果待核实，禁止重复注册')
  if not resolve_token(tenant,bot): raise WebhookRejected('机器人凭据不可用')
  secret=unseal(row['secret_ciphertext'])['secret']
  db.execute("UPDATE tgmate.bot_webhook_registrations SET status='registering',operation_token=%s,updated_at=now() WHERE bot_id=%s",(operation,bot))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(%s,%s,'bot.webhook_apply_started','telegram_bot',%s)",(tenant,actor,bot))
 config={'tenant_id':tenant,'telegram_bot_id':bot}; submitted=False
 try:
  info=adapter.call(config,'getWebhookInfo',{})
  if not isinstance(info,dict) or not isinstance(info.get('url'),str): raise UnknownSend()
  if info['url'] and info['url']!=row['url'] and not allow_replace: raise WebhookRejected('机器人已有其他Webhook；请核对后明确允许替换')
  submitted=True
  result=adapter.call(config,'setWebhook',{'url':row['url'],'secret_token':secret,'drop_pending_updates':False,'allowed_updates':['message','edited_message','callback_query','my_chat_member']})
  if result is not True: raise UnknownSend()
  status='active'; result_code='set_confirmed'
 except WebhookRejected: status='prepared'; result_code='replacement_requires_confirmation'
 except (PermanentFailure,RetryLater): status='prepared'; result_code='request_rejected'
 except Exception: status='uncertain' if submitted else 'prepared'; result_code='set_outcome_unknown' if submitted else 'preflight_unavailable'
 with transaction(tenant) as db:
  current=db.execute('SELECT * FROM tgmate.bot_webhook_registrations WHERE bot_id=%s FOR UPDATE',(bot,)).fetchone()
  if current['operation_token']!=operation or current['status'] not in ('registering','active'): return {'status':'fenced'}
  # Authenticated ingress can prove a timeout actually installed our secret.
  if current['status']=='active' and current['last_result']=='authenticated_ingress': status='active'; result_code='authenticated_ingress'
  db.execute('UPDATE tgmate.bot_webhook_registrations SET status=%s,last_result=%s,updated_at=now() WHERE bot_id=%s',(status,result_code,bot))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(%s,%s,%s,'telegram_bot',%s)",(tenant,actor,'bot.webhook_'+status,bot))
  return description(db.execute('SELECT * FROM tgmate.bot_webhook_registrations WHERE bot_id=%s',(bot,)).fetchone())

def reconcile(tenant,bot,adapter=None):
 """Read remote URL only. Matching URL cannot prove secret_token installation."""
 from app.telegram_adapters import BotBroadcast
 adapter=adapter or BotBroadcast()
 with transaction(tenant) as db:
  row=db.execute('SELECT * FROM tgmate.bot_webhook_registrations WHERE bot_id=%s',(bot,)).fetchone()
  if not row: raise WebhookRejected('入口尚未准备')
 try:
  info=adapter.call({'tenant_id':tenant,'telegram_bot_id':bot},'getWebhookInfo',{})
  if not isinstance(info,dict) or not isinstance(info.get('url'),str): raise UnknownSend()
 except Exception: return dict(description(row),remote_url_matches=None,inspection='unavailable')
 with transaction(tenant) as db:
  current=db.execute("SELECT *,updated_at<now()-interval '90 seconds' AS stale FROM tgmate.bot_webhook_registrations WHERE bot_id=%s FOR UPDATE",(bot,)).fetchone()
  if current['operation_token']!=row['operation_token']: return dict(description(current),inspection='fenced')
  matches=info['url']==current['url']
  if current['status']=='registering' and current['stale']:
   db.execute("UPDATE tgmate.bot_webhook_registrations SET status='uncertain',last_result='registration_interrupted',updated_at=now() WHERE bot_id=%s",(bot,))
  elif current['status']=='active' and not matches:
   db.execute("UPDATE tgmate.bot_webhook_registrations SET status='uncertain',last_result='remote_url_changed',updated_at=now() WHERE bot_id=%s",(bot,))
  result=description(db.execute('SELECT * FROM tgmate.bot_webhook_registrations WHERE bot_id=%s',(bot,)).fetchone())
  return dict(result,remote_url_matches=matches,inspection='url_only_secret_not_exposed_by_telegram')
