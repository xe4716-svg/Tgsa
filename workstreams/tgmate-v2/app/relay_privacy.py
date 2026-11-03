"""Actual privacy notice queue and confirmation gate, independent of legacy version field."""
import uuid
from psycopg.types.json import Jsonb
from .db import transaction
from .relay import Rejected
VERSION='v1'
TEXT='隐私告知（v1）：你发送给本机器人的消息将转交给客服运营者，用于回复咨询。客服可以查看消息内容。系统记录消息编号等投递信息，消息处理载荷加密保存，转发有效期为7天；到期不再用于发送。Telegram和客服端的副本不由本系统控制。请勿发送密码、验证码或其他敏感信息。告知投递成功后，已提交的咨询才会被转发；这不是已读或同意证明。'

def ensure(db,tenant,config,conversation):
 row=db.execute('SELECT * FROM tgmate.relay_privacy_notices WHERE conversation_id=%s AND version=%s',(conversation['id'],VERSION)).fetchone()
 if row: return row
 from .private_replies import enqueue
 job=enqueue(db,tenant,config['telegram_bot_id'],config['robot_instance_id'],conversation['customer_user_id'],TEXT,f'relay-privacy:{conversation["id"]}:{VERSION}',kind='relay_privacy_notice')
 return db.execute('INSERT INTO tgmate.relay_privacy_notices(tenant_id,conversation_id,version,outbox_id) VALUES(%s,%s,%s,%s) RETURNING *',(tenant,conversation['id'],VERSION,job)).fetchone()

def ready(db,tenant,config,conversation):
 row=ensure(db,tenant,config,conversation)
 if row['confirmed_at'] is not None: return True
 proof=db.execute("SELECT d.remote_message_ref FROM tgmate.outbox_events o JOIN tgmate.task_registry t ON t.tenant_id=o.tenant_id AND t.id=(o.payload->>'task_id')::uuid JOIN tgmate.task_deliveries d ON d.tenant_id=t.tenant_id AND d.task_id=t.id WHERE o.id=%s AND t.status='completed' AND d.status='succeeded' AND d.remote_message_ref IS NOT NULL",(row['outbox_id'],)).fetchone()
 if not proof or not proof['remote_message_ref'].isdigit() or int(proof['remote_message_ref'])<=0: return False
 db.execute('UPDATE tgmate.relay_privacy_notices SET confirmed_at=now(),remote_message_id=%s WHERE conversation_id=%s AND version=%s',(int(proof['remote_message_ref']),conversation['id'],VERSION))
 db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'relay.privacy_notice_confirmed','relay_conversation',%s)",(tenant,conversation['id']))
 return True

def retry(tenant,conversation_id,actor,evidence):
 if not isinstance(evidence,str) or not 10<=len(evidence.strip())<=1000: raise Rejected('请提供核对说明')
 with transaction(tenant) as db:
  if not db.execute("SELECT 1 FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(actor,)).fetchone(): raise Rejected('操作者身份无效')
  v=db.execute('SELECT * FROM tgmate.relay_conversations WHERE id=%s FOR UPDATE',(conversation_id,)).fetchone()
  if not v or v['status']!='open': raise Rejected('会话不可用')
  c=db.execute('SELECT * FROM tgmate.relay_configs WHERE id=%s',(v['config_id'],)).fetchone()
  if c['status']!='active': raise Rejected('客服入口未启用')
  row=db.execute('SELECT * FROM tgmate.relay_privacy_notices WHERE conversation_id=%s AND version=%s FOR UPDATE',(conversation_id,VERSION)).fetchone()
  if not row or row['confirmed_at'] is not None: raise Rejected('无需重发告知')
  job=db.execute("SELECT o.*,t.status AS task_status FROM tgmate.outbox_events o JOIN tgmate.task_registry t ON t.tenant_id=o.tenant_id AND t.id=(o.payload->>'task_id')::uuid WHERE o.id=%s FOR UPDATE OF o,t",(row['outbox_id'],)).fetchone()
  if not job or job['status']=='publishing' or job['task_status'] not in ('failed','cancelled') or job['payload'].get('unknown_outcome') is True: raise Rejected('告知仍在处理或结果不明，请先人工核对')
  from .private_replies import enqueue
  new=enqueue(db,tenant,c['telegram_bot_id'],c['robot_instance_id'],v['customer_user_id'],TEXT,f'relay-privacy:{conversation_id}:{VERSION}:{uuid.uuid4()}',kind='relay_privacy_notice')
  db.execute('UPDATE tgmate.relay_privacy_notices SET outbox_id=%s WHERE conversation_id=%s AND version=%s',(new,conversation_id,VERSION))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'relay.privacy_notice_requeued','relay_conversation',%s,%s)",(tenant,actor,conversation_id,Jsonb({'evidence':evidence})))
  return {'reply_id':str(new),'status':'queued'}
