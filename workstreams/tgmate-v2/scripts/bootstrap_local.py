from pathlib import Path
import secrets,json,uuid,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from cryptography.fernet import Fernet
root=Path(__file__).resolve().parents[1]
if (root/'.local/app.json').exists(): raise SystemExit('Local runtime already configured; existing credentials unchanged')
c=json.loads((root/'.local/db.json').read_text())
with psycopg.connect(**c,autocommit=True) as db:
 db.execute((root/'004a_relay_runtime.sql').read_text(encoding='utf-8-sig'))
 db.execute((root/'004b_moderation_control_hardening.sql').read_text(encoding='utf-8-sig'))
 runtime_password=secrets.token_urlsafe(32)
 db.execute(sql.SQL('CREATE ROLE tgmate_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}').format(sql.Literal(runtime_password)))
 from scripts.roles import grant_runtime
 grant_runtime(db,root)
 worker=uuid.uuid4()
 db.execute("INSERT INTO tgmate.worker_nodes(id,worker_code,worker_type,status) VALUES(%s,%s,'relay','healthy')",(worker,str(worker)))
 # Create only sandbox seed data. No real Bot is registered by this script.
 import importlib.util
 spec=importlib.util.spec_from_file_location('fixtures',root/'tests/conftest.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
 with db.transaction(): a=m.seed(db)
 api=secrets.token_urlsafe(32); webhook=secrets.token_urlsafe(32)
 app={'database':dict(c,user='tgmate_app',password=runtime_password),'vault_key':Fernet.generate_key().decode(),'api_keys':{api:{'tenant_id':str(a['tenant']),'role':'admin','user_id':str(a['user'])}},'webhooks':{str(a['config']):{'tenant_id':str(a['tenant']),'secret':webhook}},'worker_id':str(worker),'bot_tokens':{},'mode':'sandbox'}
 (root/'.local/app.json').write_text(json.dumps(app,indent=2))
 (root/'.local/demo.json').write_text(json.dumps({k:str(v) for k,v in a.items()}))
print('Sandbox runtime configured; credentials saved locally, not printed.')
