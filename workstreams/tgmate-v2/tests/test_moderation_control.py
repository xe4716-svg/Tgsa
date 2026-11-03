import uuid,pytest,psycopg
from test_service import service
from test_moderation import moderation,Adapter,rule
from conftest import DB,seed
from app.db import transaction
from app.moderation_control import set_enabled
from app.relay import Rejected


def disable(a): return set_enabled(a['tenant'],a['group'],a['user'],False)

def test_disable_after_binding_loss_and_cannot_reenable(moderation):
 a=moderation
 with psycopg.connect(**DB) as db: db.execute("UPDATE tgmate.executor_bindings SET status='paused' WHERE id=(SELECT executor_binding_id FROM tgmate.moderation_group_configs WHERE id=%s)",(a['group'],))
 assert disable(a)['enabled'] is False
 with pytest.raises(Rejected,match='不可用'): set_enabled(a['tenant'],a['group'],a['user'],True,adapter=Adapter())

def test_enable_sandbox_explicit_and_does_not_store_fake_rights(moderation):
 a=moderation;disable(a)
 result=set_enabled(a['tenant'],a['group'],a['user'],True,sandbox=True)
 assert result['verification']=='sandbox_simulated'
 assert set(result['required_permissions'])=={'can_send_messages','can_delete_messages','can_restrict_members'}
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT enabled FROM tgmate.moderation_group_configs WHERE id=%s',(a['group'],)).fetchone()['enabled']
  assert db.execute('SELECT checked_at FROM tgmate.bot_destination_access WHERE telegram_bot_id=%s',(a['modbot'],)).fetchone()['checked_at'] is None

def test_permission_denial_keeps_disabled(moderation):
 a=moderation;disable(a)
 with pytest.raises(Rejected,match='所需权限'): set_enabled(a['tenant'],a['group'],a['user'],True,adapter=Adapter(allowed=False))
 with transaction(a['tenant']) as db: assert not db.execute('SELECT enabled FROM tgmate.moderation_group_configs WHERE id=%s',(a['group'],)).fetchone()['enabled']

def test_preflight_cannot_enable_after_concurrent_disable_or_policy_change(moderation):
 a=moderation;disable(a)
 class Changing(Adapter):
  def permissions(self,config):
   disable(a)
   return super().permissions(config)
 with pytest.raises(Rejected,match='已变更'): set_enabled(a['tenant'],a['group'],a['user'],True,adapter=Changing())

def test_cross_tenant_and_invalid_actor_rejected(moderation):
 a=moderation
 with psycopg.connect(**DB) as db: other=seed(db)
 with pytest.raises(Rejected): set_enabled(other['tenant'],a['group'],other['user'],False)
 with pytest.raises(Rejected,match='操作者'): set_enabled(a['tenant'],a['group'],uuid.uuid4(),False)

def test_rule_change_during_preflight_is_fenced(moderation):
 a=moderation;disable(a)
 class Changing(Adapter):
  def permissions(self,config):
   rule(a,code='new-rule',action='ban')
   return super().permissions(config)
 with pytest.raises(Rejected,match='已变更'): set_enabled(a['tenant'],a['group'],a['user'],True,adapter=Changing())


def test_production_cannot_request_sandbox_preflight(moderation,monkeypatch):
 a=moderation;disable(a)
 monkeypatch.setattr('app.moderation_control.settings',lambda:{'mode':'production'})
 with pytest.raises(Rejected,match='仅本地沙盒'): set_enabled(a['tenant'],a['group'],a['user'],True,sandbox=True)

def test_control_http_endpoints_and_viewer_denial(moderation):
 import json,os
 from pathlib import Path
 from fastapi.testclient import TestClient
 from app.main import app
 a=moderation;client=TestClient(app);headers={'Authorization':'Bearer test-admin-key'}
 assert client.get('/api/moderation/configs',headers=headers).status_code==200
 url=f"/api/moderation/configs/{a['group']}/state"
 assert client.post(url,headers=headers,json={'enabled':False}).json()['enabled'] is False
 assert client.post(url,headers=headers,json={'enabled':True,'sandbox':True}).json()['verification']=='sandbox_simulated'
 p=Path(os.environ['TGMATE_CONFIG']);cfg=json.loads(p.read_text());cfg['api_keys']['test-viewer-key']={'tenant_id':str(a['tenant']),'role':'viewer','user_id':str(a['user'])};p.write_text(json.dumps(cfg))
 assert client.post(url,headers={'Authorization':'Bearer test-viewer-key'},json={'enabled':False}).status_code==403
