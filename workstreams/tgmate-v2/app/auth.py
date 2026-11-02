"""Password hashes in the database and opaque, revocable web sessions."""
import base64,hashlib,hmac,secrets
from datetime import datetime,timedelta,timezone
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from app.db import settings

class LoginRejected(ValueError): pass
class LoginLimited(ValueError): pass


def digest(value): return hashlib.sha256(value.encode()).hexdigest()

def password_hash(password):
 if not isinstance(password,str) or not 12<=len(password)<=128: raise LoginRejected('密码需为12至128个字符')
 salt=secrets.token_bytes(16)
 key=hashlib.scrypt(password.encode(),salt=salt,n=32768,r=8,p=3,dklen=32,maxmem=64*1024*1024)
 return 'scrypt$32768$8$3$'+base64.urlsafe_b64encode(salt).decode()+'$'+base64.urlsafe_b64encode(key).decode()


def check_password(password,encoded):
 if not isinstance(password,str) or not 1<=len(password)<=128: return False
 try:
  method,n,r,p,salt,expected=encoded.split('$')
  # Only the reviewed work factor is accepted; records cannot request unbounded work.
  if (method,n,r,p)!=('scrypt','32768','8','3'): return False
  salt=base64.urlsafe_b64decode(salt); expected=base64.urlsafe_b64decode(expected)
  if len(salt)!=16 or len(expected)!=32: return False
  actual=hashlib.scrypt(password.encode(),salt=salt,n=32768,r=8,p=3,dklen=32,maxmem=64*1024*1024)
  return hmac.compare_digest(actual,expected)
 except (ValueError,TypeError): return False

_DUMMY=password_hash('not-a-real-account-password')

@contextmanager
def auth_transaction():
 config=settings().get('auth_database')
 if not config: raise RuntimeError('Web authentication not configured')
 with psycopg.connect(**config,row_factory=dict_row) as db: yield db


def login(email,workspace,password,ip):
 if not isinstance(email,str) or not isinstance(workspace,str) or not isinstance(password,str): raise LoginRejected('登录信息无效')
 email=email.strip(); workspace=workspace.strip().lower()
 if len(email)>254 or len(workspace)>80 or not email or not workspace or len(password)>128: raise LoginRejected('工作区、邮箱或密码不正确')
 pepper=settings()['auth_rate_key'].encode()
 with auth_transaction() as db: record=db.execute('SELECT * FROM tgmate.auth_lookup(%s,%s)',(email,workspace)).fetchone()
 account_identity=f'login-user:{record["user_id"]}' if record else f'login-unknown:{email.casefold()}'
 account_key=hmac.new(pepper,account_identity.encode(),hashlib.sha256).hexdigest()
 ip_key=hmac.new(pepper,str(ip).encode(),hashlib.sha256).hexdigest()
 # Reserve the attempt in its own transaction so failed authentication is counted.
 with auth_transaction() as db:
  attempt=db.execute('SELECT tgmate.auth_take_attempt(%s,%s) AS id',(account_key,ip_key)).fetchone()['id']
 if not attempt: raise LoginLimited('登录尝试过多，请稍后再试')
 valid=check_password(password,record['password_hash'] if record else _DUMMY)
 if not record or not valid: raise LoginRejected('工作区、邮箱或密码不正确')
 token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24); expires=datetime.now(timezone.utc)+timedelta(hours=12)
 try:
  with auth_transaction() as db:
   db.execute('SELECT tgmate.auth_create_session(%s,%s,%s,%s,%s,%s)',(record['user_id'],record['tenant_id'],digest(token),digest(csrf),expires,record['credential_version']))
   db.execute('SELECT tgmate.auth_mark_success(%s)',(attempt,))
 except psycopg.errors.RaiseException: raise LoginRejected('账号状态已变更，请重新登录') from None
 return {'token':token,'csrf':csrf,'expires_at':expires}


def context(token):
 if not isinstance(token,str) or not 20<=len(token)<=256: return None
 with auth_transaction() as db: record=db.execute('SELECT * FROM tgmate.auth_context(%s)',(digest(token),)).fetchone()
 if not record: return None
 codes=record['permission_codes']
 return {'user_id':str(record['user_id']),'tenant_id':str(record['tenant_id']),'email':record['email'],'permissions':codes,'roles':record['role_codes'],'role':'admin' if 'tenant.admin' in codes else 'operator' if any(c.endswith('.manage') for c in codes) else 'viewer','_csrf_hash':record['csrf_hash'],'_auth_method':'session'}


def logout(token):
 if not isinstance(token,str) or len(token)>256: return
 with auth_transaction() as db: db.execute('SELECT tgmate.auth_revoke_session(%s)',(digest(token),))

def change_password(token,old_password,new_password,ip):
 if not isinstance(token,str) or not token: raise LoginRejected('请先通过网页登录')
 if not isinstance(old_password,str) or not isinstance(new_password,str) or len(old_password)>128: raise LoginRejected('密码格式无效')
 with auth_transaction() as db: record=db.execute('SELECT * FROM tgmate.auth_password_record(%s)',(digest(token),)).fetchone()
 if not record: raise LoginRejected('登录已过期')
 pepper=settings()['auth_rate_key'].encode()
 account=hmac.new(pepper,f'password-change:{record["user_id"]}'.encode(),hashlib.sha256).hexdigest(); ipkey=hmac.new(pepper,str(ip).encode(),hashlib.sha256).hexdigest()
 with auth_transaction() as db: attempt=db.execute('SELECT tgmate.auth_take_attempt(%s,%s) AS id',(account,ipkey)).fetchone()['id']
 if not attempt: raise LoginLimited('尝试次数过多，请稍后再试')
 if not check_password(old_password,record['password_hash']): raise LoginRejected('旧密码不正确')
 if hmac.compare_digest(old_password.encode(),new_password.encode()): raise LoginRejected('新密码不能与旧密码相同')
 encoded=password_hash(new_password)
 try:
  with auth_transaction() as db:
   db.execute('SELECT tgmate.auth_change_password(%s,%s,%s)',(digest(token),record['credential_version'],encoded))
   db.execute('SELECT tgmate.auth_mark_success(%s)',(attempt,))
 except psycopg.errors.RaiseException: raise LoginRejected('账号状态已变更，请重新登录') from None
 # Remove an obsolete local initial password only for the original demo identity.
 from app.db import ROOT
 import json
 demo=ROOT/'.local/demo.json'; initial=ROOT/'.local/owner_login.json'
 if demo.exists() and initial.exists() and json.loads(demo.read_text()).get('user')==str(record['user_id']):
  value=json.loads(initial.read_text()); value.pop('password',None); value['password_changed']=True; initial.write_text(json.dumps(value,indent=2))
