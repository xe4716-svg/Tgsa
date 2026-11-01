import pytest,psycopg
from conftest import DB
from test_service import service,post,message,Adapter
from app.worker import run_once,UnknownSend,PermanentFailure
from app.private_worker import run_once as private_once
from app.db import transaction,unseal
from app.relay_privacy import retry,VERSION
from app.private_replies import review_reply
from app.relay import Rejected

class Notices:
 def __init__(self,fail=None):self.sent=[];self.fail=fail
 def send_private(self,bot,target,text,markup=None):
  self.sent.append((target,text))
  if self.fail: raise self.fail()
  return 800

def fresh(service):
 client,a,c=service
 with psycopg.connect(**DB) as db: db.execute('DELETE FROM tgmate.relay_privacy_notices WHERE conversation_id=%s',(a['conversation'],))
 return client,a,c

def due(a):
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.outbox_events SET available_at=now() WHERE event_type='relay.dispatch'")

def receipt(a):
 with transaction(a['tenant']) as db: return db.execute('SELECT * FROM tgmate.relay_privacy_notices WHERE conversation_id=%s',(a['conversation'],)).fetchone()

def test_first_content_waits_for_real_notice_delivery(service):
 client,a,c=fresh(service);assert post(client,a,message()).json()['status']=='queued'
 adapter=Adapter();assert run_once(a['tenant'],c['worker_id'],adapter)['status']=='waiting_for_privacy_notice'
 assert adapter.targets==[] and receipt(a)['confirmed_at'] is None
 notices=Notices();assert private_once(a['tenant'],c['worker_id'],notices)['status']=='succeeded'
 assert notices.sent[0][0]==200 and '隐私告知' in notices.sent[0][1]
 due(a);assert run_once(a['tenant'],c['worker_id'],adapter)['status']=='succeeded'
 assert adapter.targets==[100] and receipt(a)['remote_message_id']==800
 assert post(client,a,message(2)).status_code==200
 assert private_once(a['tenant'],c['worker_id'],notices)['status']=='idle'

def test_unknown_notice_blocks_relay_and_requires_manual_resolution(service):
 client,a,c=fresh(service);post(client,a,message())
 notices=Notices(UnknownSend);assert private_once(a['tenant'],c['worker_id'],notices)['status']=='unknown'
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='waiting_for_privacy_notice'
 row=receipt(a)
 with pytest.raises(Rejected): retry(a['tenant'],a['conversation'],a['user'],'已检查结果仍未知，禁止直接再次发送')
 review_reply(a['tenant'],row['outbox_id'],a['user'],'confirm_sent','人工查看客户私聊确认该告知已经发送',remote_message_id=900)
 due(a);assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='succeeded'
 assert receipt(a)['remote_message_id']==900 and len(notices.sent)==1

def test_failed_notice_can_be_explicitly_regenerated(service):
 client,a,c=fresh(service);post(client,a,message())
 assert private_once(a['tenant'],c['worker_id'],Notices(PermanentFailure))['status']=='failed'
 old=receipt(a)['outbox_id'];result=retry(a['tenant'],a['conversation'],a['user'],'已确认发送失败，重新生成有效的告知任务')
 assert result['reply_id']!=str(old)
 assert private_once(a['tenant'],c['worker_id'],Notices())['status']=='succeeded'
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='succeeded'

def test_legacy_version_field_is_not_proof_and_notice_deduplicates(service):
 client,a,c=fresh(service);post(client,a,message());post(client,a,message(2))
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT privacy_notice_version FROM tgmate.relay_conversations WHERE id=%s',(a['conversation'],)).fetchone()['privacy_notice_version']=='v1'
  assert db.execute('SELECT count(*) AS n FROM tgmate.relay_privacy_notices').fetchone()['n']==1
  assert db.execute("SELECT count(*) AS n FROM tgmate.outbox_events WHERE event_type='bot.private_reply'").fetchone()['n']==1
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='waiting_for_privacy_notice'

def test_blocked_conversation_cancels_notice_without_send(service):
 client,a,c=fresh(service);post(client,a,message())
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.relay_conversations SET status='blocked' WHERE id=%s",(a['conversation'],))
 notices=Notices();assert private_once(a['tenant'],c['worker_id'],notices)['status']=='cancelled'
 assert notices.sent==[]

def test_quota_failure_rolls_back_notice_and_content_together(service):
 client,a,c=fresh(service)
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.quota_accounts SET limit_value=0')
 assert post(client,a,message()).status_code==422
 assert receipt(a) is None
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.outbox_events').fetchone()['n']==0
  assert db.execute('SELECT count(*) AS n FROM tgmate.relay_operations').fetchone()['n']==0

def test_expired_notice_can_be_regenerated_without_releasing_content_early(service):
 client,a,c=fresh(service);post(client,a,message())
 with transaction(a['tenant']) as db:
  db.execute("UPDATE tgmate.relay_payloads SET created_at=now()-interval '2 days',expires_at=now()-interval '1 second' WHERE id=(SELECT (payload->>'payload_id')::uuid FROM tgmate.outbox_events WHERE id=%s)",(receipt(a)['outbox_id'],))
 assert private_once(a['tenant'],c['worker_id'],Notices())['status']=='cancelled'
 retry(a['tenant'],a['conversation'],a['user'],'告知内容已过保留期限，确认原任务已取消后重新生成')
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='waiting_for_privacy_notice'
 assert private_once(a['tenant'],c['worker_id'],Notices())['status']=='succeeded'
 due(a);assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='succeeded'

def test_private_notice_review_requires_its_actual_module_permission(service):
 import json,os
 from pathlib import Path
 client,a,c=fresh(service);post(client,a,message());private_once(a['tenant'],c['worker_id'],Notices(UnknownSend))
 job=receipt(a)['outbox_id'];path=Path(os.environ['TGMATE_CONFIG']);cfg=json.loads(path.read_text())
 for key,permissions in [('test-mod-only',['moderation.manage']),('test-relay-only',['relay.manage','relay.read'])]:
  cfg['api_keys'][key]={'tenant_id':str(a['tenant']),'role':'operator','user_id':str(a['user']),'permissions':permissions}
 path.write_text(json.dumps(cfg))
 body={'decision':'confirm_sent','evidence':'人工核对该客户聊天确认隐私告知已经成功送达','remote_message_id':901}
 assert client.post(f'/api/private-replies/{job}/resolve',headers={'Authorization':'Bearer test-mod-only'},json=body).status_code==403
 assert client.post(f'/api/private-replies/{job}/resolve',headers={'Authorization':'Bearer test-relay-only'},json=body).status_code==200
 status=client.get(f"/api/relay/conversations/{a['conversation']}/privacy",headers={'Authorization':'Bearer test-relay-only'})
 assert status.status_code==200 and status.json()[0]['notice_status']=='completed'
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='succeeded'
