import uuid,psycopg,pytest
from conftest import DB
from test_service import service
from app.db import settings
from app.auth import password_hash,check_password,login,context,logout,LoginRejected,LoginLimited,auth_transaction,digest

PASSWORD='test-only-correct-horse-password'

@pytest.fixture
def web_user(service):
 cl,a,c=service; encoded=password_hash(PASSWORD)
 with psycopg.connect(**DB) as db:
  db.execute('INSERT INTO tgmate.web_credentials(user_id,password_hash) VALUES(%s,%s)',(a['user'],encoded))
  role=uuid.uuid4()
  db.execute("INSERT INTO tgmate.roles(id,tenant_id,code,name) VALUES(%s,%s,'owner','test owner')",(role,a['tenant']))
  db.execute("INSERT INTO tgmate.role_permissions(tenant_id,role_id,permission_code) VALUES(%s,%s,'tenant.admin')",(a['tenant'],role))
  db.execute('INSERT INTO tgmate.tenant_member_roles(tenant_id,user_id,role_id) VALUES(%s,%s,%s)',(a['tenant'],a['user'],role))
  email,slug=db.execute('SELECT u.email::text,t.slug FROM tgmate.users u CROSS JOIN tgmate.tenants t WHERE u.id=%s AND t.id=%s',(a['user'],a['tenant'])).fetchone()
 return a,email,slug,'test-ip:'+str(uuid.uuid4())


def test_password_hash_is_salted_and_bounded():
 a=password_hash(PASSWORD); b=password_hash(PASSWORD)
 assert a!=b and PASSWORD not in a
 assert check_password(PASSWORD,a) and not check_password('incorrect',a)
 assert not check_password(PASSWORD,'scrypt$999999999$8$3$x$x')
 with pytest.raises(LoginRejected): password_hash('short')


def test_session_mint_context_and_revocation(web_user):
 a,email,slug,ip=web_user; result=login(email,slug,PASSWORD,ip)
 who=context(result['token'])
 assert who['tenant_id']==str(a['tenant']) and who['user_id']==str(a['user']) and 'tenant.admin' in who['permissions']
 with psycopg.connect(**DB) as db:
  stored=db.execute('SELECT token_hash,csrf_hash FROM tgmate.web_sessions WHERE token_hash=%s',(digest(result['token']),)).fetchone()
  assert stored==(digest(result['token']),digest(result['csrf']))
 logout(result['token']); assert context(result['token']) is None


def test_failed_login_generic_error(web_user):
 a,email,slug,ip=web_user
 with pytest.raises(LoginRejected) as wrong: login(email,slug,'wrong password',ip)
 with pytest.raises(LoginRejected) as absent: login('absent@example.invalid',slug,'wrong password',ip)
 assert str(wrong.value)==str(absent.value)


def test_account_rate_limit_persists_failed_attempts(web_user):
 a,email,slug,ip=web_user
 for _ in range(10):
  with pytest.raises(LoginRejected): login(email,slug,'incorrect',ip)
 with pytest.raises(LoginLimited): login(email,slug,PASSWORD,ip)


def test_membership_revocation_invalidates_session(web_user):
 a,email,slug,ip=web_user; result=login(email,slug,PASSWORD,ip)
 with psycopg.connect(**DB) as db: db.execute("UPDATE tgmate.tenant_members SET status='suspended' WHERE tenant_id=%s AND user_id=%s",(a['tenant'],a['user']))
 assert context(result['token']) is None


def test_password_version_change_invalidates_session(web_user):
 a,email,slug,ip=web_user; result=login(email,slug,PASSWORD,ip)
 with psycopg.connect(**DB) as db: db.execute('UPDATE tgmate.web_credentials SET credential_version=credential_version+1 WHERE user_id=%s',(a['user'],))
 assert context(result['token']) is None


def test_auth_tables_not_readable_by_runtime_or_auth_login_role(web_user):
 for key in ['database','auth_database']:
  with psycopg.connect(**settings()[key]) as db:
   with pytest.raises(psycopg.errors.InsufficientPrivilege): db.execute('SELECT password_hash FROM tgmate.web_credentials')


def test_definer_owner_has_no_superuser_or_rls_bypass(web_user):
 with psycopg.connect(**DB) as db:
  assert db.execute("SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname='tgmate_auth_owner'").fetchone()==(False,False,False)
  assert db.execute("SELECT pg_get_userbyid(proowner),prosecdef FROM pg_proc WHERE oid='tgmate.auth_context(text)'::regprocedure").fetchone()==('tgmate_auth_owner',True)


def test_same_user_different_tenant_has_only_that_tenants_roles(web_user):
 a,email,slug,ip=web_user; other=uuid.uuid4(); role=uuid.uuid4()
 with psycopg.connect(**DB) as db:
  db.execute('INSERT INTO tgmate.tenants(id,name,slug) VALUES(%s,%s,%s)',(other,'viewer tenant',str(other)))
  db.execute("INSERT INTO tgmate.tenant_members(tenant_id,user_id,status) VALUES(%s,%s,'active')",(other,a['user']))
  db.execute("INSERT INTO tgmate.roles(id,tenant_id,code,name) VALUES(%s,%s,'viewer','viewer')",(role,other))
  db.execute("INSERT INTO tgmate.role_permissions(tenant_id,role_id,permission_code) VALUES(%s,%s,'relay.read')",(other,role))
  db.execute('INSERT INTO tgmate.tenant_member_roles(tenant_id,user_id,role_id) VALUES(%s,%s,%s)',(other,a['user'],role))
 who=context(login(email,str(other),PASSWORD,ip)['token'])
 assert who['tenant_id']==str(other) and who['permissions']==['relay.read']
 assert who['role']=='viewer'

def test_password_change_revokes_all_sessions(web_user):
 from app.auth import change_password
 a,email,slug,ip=web_user
 first=login(email,slug,PASSWORD,ip); second=login(email,slug,PASSWORD,ip)
 change_password(first['token'],PASSWORD,PASSWORD+'-new',ip)
 assert context(first['token']) is None and context(second['token']) is None
 with pytest.raises(LoginRejected): login(email,slug,PASSWORD,ip)
 assert context(login(email,slug,PASSWORD+'-new',ip)['token'])['user_id']==str(a['user'])


def test_wrong_old_password_cannot_change_credential(web_user):
 from app.auth import change_password
 a,email,slug,ip=web_user; result=login(email,slug,PASSWORD,ip)
 with pytest.raises(LoginRejected): change_password(result['token'],'wrong-old',PASSWORD+'-new',ip)
 assert context(result['token'])

def test_old_password_verification_cannot_mint_new_version_session(web_user):
 from datetime import datetime,timedelta,timezone
 a,email,slug,ip=web_user
 with auth_transaction() as db: old=db.execute('SELECT * FROM tgmate.auth_lookup(%s,%s)',(email,slug)).fetchone()
 with psycopg.connect(**DB) as db: db.execute('UPDATE tgmate.web_credentials SET credential_version=credential_version+1 WHERE user_id=%s',(a['user'],))
 with pytest.raises(psycopg.errors.RaiseException):
  with auth_transaction() as db: db.execute('SELECT tgmate.auth_create_session(%s,%s,%s,%s,%s,%s)',(a['user'],a['tenant'],digest('synthetic-session-that-must-be-rejected'),digest('synthetic-csrf'),datetime.now(timezone.utc)+timedelta(hours=1),old['credential_version']))


def test_runtime_cannot_call_password_lookup(web_user):
 with psycopg.connect(**settings()['database']) as db:
  with pytest.raises(psycopg.errors.InsufficientPrivilege): db.execute("SELECT * FROM tgmate.auth_lookup('someone@example.invalid','workspace')")
