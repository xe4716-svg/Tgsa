"""Bot possession verification and tenant-isolated encrypted token registration.
getMe proves token possession, not the human's Telegram legal ownership.
Registration deliberately never changes the remote webhook.
"""
import hashlib,re,uuid
import httpx,psycopg
from app.db import transaction,seal,unseal

class RegistrationRejected(ValueError): pass

class TelegramIdentityProbe:
 def __init__(self,client=None): self.client=client or httpx.Client(timeout=15,follow_redirects=False)
 def verify(self,token):
  try:
   response=self.client.post(f'https://api.telegram.org/bot{token}/getMe',json={})
   body=response.json()
  except (httpx.HTTPError,ValueError): raise RegistrationRejected('无法验证机器人凭据，请稍后重试') from None
  if response.status_code!=200 or not isinstance(body,dict) or body.get('ok') is not True: raise RegistrationRejected('机器人凭据验证失败')
  return body.get('result')

def register(tenant,actor,token,probe=None,existing_bot_id=None):
 if not isinstance(token,str) or not re.fullmatch(r'[1-9][0-9]{3,19}:[A-Za-z0-9_-]{20,200}',token): raise RegistrationRejected('机器人凭据格式无效')
 me=(probe or TelegramIdentityProbe()).verify(token)
 if not isinstance(me,dict) or me.get('is_bot') is not True or type(me.get('id')) is not int or me['id']<=0 or me['id']!=int(token.split(':')[0]): raise RegistrationRejected('机器人身份验证不一致')
 username=me.get('username');name=me.get('first_name')
 if not isinstance(username,str) or len(username)>64 or not isinstance(name,str) or len(name)>256: raise RegistrationRejected('机器人身份信息无效')
 fingerprint=hashlib.sha256(token.encode()).hexdigest()
 try:
  with transaction(tenant) as db:
   if not db.execute("SELECT 1 FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(actor,)).fetchone(): raise RegistrationRejected('操作者不属于当前工作区')
   db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'bot-registration:{me["id"]}',))
   old=db.execute('SELECT bot_id FROM tgmate.bot_ownership_claims WHERE external_bot_id=%s',(me['id'],)).fetchone()
   existing=db.execute('SELECT id FROM tgmate.telegram_bots WHERE telegram_bot_id=%s',(me['id'],)).fetchall()
   legacy=None
   if existing_bot_id is not None:
    legacy=db.execute('SELECT id,telegram_bot_id FROM tgmate.telegram_bots WHERE id=%s FOR UPDATE',(existing_bot_id,)).fetchone()
    if not legacy or legacy['telegram_bot_id']!=me['id']: raise RegistrationRejected('历史机器人记录与已验证身份不匹配')
    if old and str(old['bot_id'])!=str(existing_bot_id): raise RegistrationRejected('机器人已登记到其他记录，不能替换绑定')
    if any(str(x['id'])!=str(existing_bot_id) for x in existing): raise RegistrationRejected('历史机器人记录存在重复身份，需要管理员处理')
   elif not old and existing: raise RegistrationRejected('已有机器人记录需要管理员完成凭据迁移，不能重复登记')
   identifier=old['bot_id'] if old else legacy['id'] if legacy else uuid.uuid4()
   if old or legacy:
    db.execute("UPDATE tgmate.telegram_bots SET username=%s,display_name=%s,token_vault_ref=%s,token_fingerprint=%s,status='active',last_checked_at=now(),updated_at=now() WHERE id=%s",(username,name,f'bot-credential:{identifier}',fingerprint,identifier))
   else:
    db.execute("INSERT INTO tgmate.telegram_bots(id,tenant_id,display_name,username,telegram_bot_id,token_vault_ref,token_fingerprint,status,last_checked_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'active',now())",(identifier,tenant,name,username,me['id'],f'bot-credential:{identifier}',fingerprint))
   if not old:
    db.execute('INSERT INTO tgmate.bot_ownership_claims(external_bot_id,tenant_id,bot_id) VALUES(%s,%s,%s)',(me['id'],tenant,identifier))
   db.execute('INSERT INTO tgmate.bot_credentials(tenant_id,bot_id,token_ciphertext) VALUES(%s,%s,%s) ON CONFLICT(tenant_id,bot_id) DO UPDATE SET token_ciphertext=EXCLUDED.token_ciphertext,version=tgmate.bot_credentials.version+1,updated_at=now()',(tenant,identifier,seal({'token':token})))
   db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(%s,%s,%s,'telegram_bot',%s)",(tenant,actor,'bot.credential_rotated' if old else 'bot.credential_migrated' if legacy else 'bot.registered',identifier))
   return {'id':str(identifier),'username':username,'external_bot_id':me['id'],'status':'active','webhook_registered':False}
 except psycopg.errors.UniqueViolation: raise RegistrationRejected('该机器人无法登记，请联系管理员检查已有绑定') from None

def resolve_token(tenant,bot_id):
 # No process-global token map: every credential lookup requires tenant isolation.
 from cryptography.fernet import InvalidToken
 with transaction(tenant) as db:
  row=db.execute("SELECT c.token_ciphertext,b.telegram_bot_id,b.token_fingerprint FROM tgmate.bot_credentials c JOIN tgmate.telegram_bots b ON b.tenant_id=c.tenant_id AND b.id=c.bot_id JOIN tgmate.bot_ownership_claims o ON o.tenant_id=b.tenant_id AND o.bot_id=b.id AND o.external_bot_id=b.telegram_bot_id WHERE c.bot_id=%s AND b.status='active'",(bot_id,)).fetchone()
 if not row: return None
 try:
  payload=unseal(row['token_ciphertext']); token=payload['token']
  if not isinstance(token,str) or token.split(':')[0]!=str(row['telegram_bot_id']) or hashlib.sha256(token.encode()).hexdigest()!=row['token_fingerprint']: return None
  return token
 except (InvalidToken,ValueError,KeyError,TypeError): return None
