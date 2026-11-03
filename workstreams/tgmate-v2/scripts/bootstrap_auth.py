from pathlib import Path
import json,secrets,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from app.auth import password_hash
root=Path(__file__).resolve().parents[1]; owner=json.loads((root/'.local/db.json').read_text()); config_path=root/'.local/app.json'; config=json.loads(config_path.read_text())
if config.get('mode')!='sandbox': raise SystemExit('This setup command is only for the local sandbox')
with psycopg.connect(**owner) as db:
 if not db.execute("SELECT to_regclass('tgmate.web_credentials')").fetchone()[0]: db.execute((root/'004c_web_auth.sql').read_text(encoding='utf-8-sig'))
 if 'auth_database' not in config:
  password=secrets.token_urlsafe(32)
  db.execute(sql.SQL('CREATE ROLE tgmate_auth LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}').format(sql.Literal(password)))
  config['auth_database']=dict(owner,user='tgmate_auth',password=password)
 from scripts.roles import grant_auth
 grant_auth(db)
 config.setdefault('auth_rate_key',secrets.token_urlsafe(32))
 # Seed the existing local demonstration identity only, never a production account.
 demo=json.loads((root/'.local/demo.json').read_text()); user=demo['user']; tenant=demo['tenant']
 if not db.execute('SELECT user_id FROM tgmate.web_credentials WHERE user_id=%s',(user,)).fetchone():
  password=secrets.token_urlsafe(24); encoded=password_hash(password)
  db.execute('INSERT INTO tgmate.web_credentials(user_id,password_hash) VALUES(%s,%s)',(user,encoded))
  details=db.execute('SELECT u.email::text,t.slug FROM tgmate.users u CROSS JOIN tgmate.tenants t WHERE u.id=%s AND t.id=%s',(user,tenant)).fetchone()
  (root/'.local/owner_login.json').write_text(json.dumps({'email':details[0],'workspace':details[1],'password':password},indent=2))
 db.execute("SELECT set_config('app.tenant_id',%s,true)",(tenant,))
 role=db.execute("INSERT INTO tgmate.roles(tenant_id,code,name,is_system) VALUES(%s,'owner','工作区所有者',true) ON CONFLICT(tenant_id,code) DO UPDATE SET name=EXCLUDED.name RETURNING id",(tenant,)).fetchone()[0]
 db.execute('INSERT INTO tgmate.role_permissions(tenant_id,role_id,permission_code) SELECT %s,%s,code FROM tgmate.permissions ON CONFLICT DO NOTHING',(tenant,role))
 db.execute('INSERT INTO tgmate.tenant_member_roles(tenant_id,user_id,role_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,user,role))
config_path.write_text(json.dumps(config,indent=2))
print('Local web auth configured; no plaintext credentials printed. Production account setup remains separate.')
