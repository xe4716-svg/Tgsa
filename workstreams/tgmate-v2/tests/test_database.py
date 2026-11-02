import uuid
import psycopg
import pytest
from conftest import operation

def test_rls_read_hides_other_tenant(db,setup):
 a,b=setup
 assert db.execute('SELECT count(*) FROM tgmate.relay_configs').fetchone()[0]==1
 assert db.execute('SELECT id FROM tgmate.relay_configs WHERE id=%s',(b['config'],)).fetchone() is None

def test_runtime_is_not_bypass_or_owner(db,setup):
 row=db.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
 assert row==(False,False)
 assert not db.execute("SELECT pg_get_userbyid(relowner)=current_user FROM pg_class WHERE oid='tgmate.relay_configs'::regclass").fetchone()[0]

def test_cross_tenant_insert_rejected(db,setup):
 a,b=setup
 with pytest.raises(psycopg.Error): operation(db,b)

def test_cross_tenant_reference_rejected(db,setup):
 a,b=setup
 with pytest.raises(psycopg.Error): operation(db,a,conversation_id=b['conversation'])

def test_duplicate_event_rejected(db,setup):
 a,b=setup; operation(db,a,event_key='same')
 with pytest.raises(psycopg.errors.UniqueViolation): operation(db,a,event_key='same')

def test_wrong_destination_rejected(db,setup):
 a,b=setup
 with pytest.raises(psycopg.Error): operation(db,a,target_chat_id=999)

def test_reply_must_have_customer_mapping(db,setup):
 a,b=setup
 with pytest.raises(psycopg.Error): operation(db,a,direction='to_customer',source_chat_id=100,target_chat_id=200,reply_to_operation_id=uuid.uuid4())

def test_success_and_reply(db,setup):
 a,b=setup; op=operation(db,a)
 db.execute("UPDATE tgmate.relay_operations SET status='sending' WHERE id=%s",(op,))
 db.execute("UPDATE tgmate.relay_operations SET status='succeeded',target_message_id=500 WHERE id=%s",(op,))
 reply=operation(db,a,direction='to_customer',source_chat_id=100,target_chat_id=200,reply_to_operation_id=op)
 assert reply

def test_unknown_cannot_automatically_retry(db,setup):
 a,b=setup; op=operation(db,a)
 db.execute("UPDATE tgmate.relay_operations SET status='sending' WHERE id=%s",(op,))
 db.execute("UPDATE tgmate.relay_operations SET status='uncertain' WHERE id=%s",(op,))
 with pytest.raises(psycopg.Error): db.execute("UPDATE tgmate.relay_operations SET status='sending' WHERE id=%s",(op,))

def test_pause_generation_blocks_stale(db,setup):
 a,b=setup; op=operation(db,a)
 db.execute("UPDATE tgmate.relay_configs SET status='paused' WHERE id=%s",(a['config'],))
 db.execute("UPDATE tgmate.relay_configs SET status='active' WHERE id=%s",(a['config'],))
 with pytest.raises(psycopg.Error): db.execute("UPDATE tgmate.relay_operations SET status='sending' WHERE id=%s",(op,))

def test_block_unblock_fences_queue(db,setup):
 a,b=setup; op=operation(db,a)
 db.execute("UPDATE tgmate.relay_conversations SET status='blocked' WHERE id=%s",(a['conversation'],))
 db.execute("UPDATE tgmate.relay_conversations SET status='open' WHERE id=%s",(a['conversation'],))
 with pytest.raises(psycopg.Error): db.execute("UPDATE tgmate.relay_operations SET status='sending' WHERE id=%s",(op,))

def test_audit_history_cannot_mutate(db):
 from conftest import seed
 a=seed(db)
 db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type) VALUES(%s,'test','test')",(a['tenant'],))
 with pytest.raises(psycopg.Error): db.execute('DELETE FROM tgmate.audit_logs WHERE tenant_id=%s',(a['tenant'],))

def test_transaction_failure_rolls_back(db):
 with pytest.raises(psycopg.Error):
  with db.transaction():
   db.execute('CREATE TABLE public.rollback_probe(id int)')
   db.execute('SELECT 1/0')
 assert db.execute("SELECT to_regclass('public.rollback_probe')").fetchone()[0] is None
