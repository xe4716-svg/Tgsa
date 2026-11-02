"""Rebuild all released SQL in a NEW local DB, apply least-privilege roles and run the suite."""
from pathlib import Path
import json,uuid,sys,os,hashlib,subprocess,xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from scripts.roles import grant_runtime,grant_auth
root=Path(__file__).resolve().parents[1]
owner=json.loads((root/'.local/db.json').read_text()); app=json.loads((root/'.local/app.json').read_text())
name='tgmate_clean_'+uuid.uuid4().hex[:12]
with psycopg.connect(**dict(owner,dbname='postgres'),autocommit=True) as db: db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
connection=dict(owner,dbname=name)
files=sorted((root/'source_001_003').glob('*.sql'))+[root/n for n in ['tgmate_v2_004_bidirectional.sql','004a_relay_runtime.sql','004b_moderation_control_hardening.sql','004c_web_auth.sql','004d_bot_credentials.sql','004e_bot_webhooks.sql','004f_relay_operator_auth.sql','004g_operator_identity_invalidation.sql','004h_relay_privacy_notice.sql']]
evidence=[]
with psycopg.connect(**connection,autocommit=True) as db:
 for path in files:
  db.execute(path.read_text(encoding='utf-8-sig')); evidence.append({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'executed':True})
 with db.transaction():
  grant_runtime(db,root); grant_auth(db)
  worker=uuid.uuid4(); db.execute("INSERT INTO tgmate.worker_nodes(id,worker_code,worker_type,status) VALUES(%s,%s,'local-verification','healthy')",(worker,str(worker)))
 table_count=db.execute("SELECT count(*) FROM pg_tables WHERE schemaname='tgmate'").fetchone()[0]
private=root/'.local/clean';private.mkdir(exist_ok=True)
owner_path=private/f'{name}_owner.json'; app_path=private/f'{name}_app.json'
owner_path.write_text(json.dumps(connection))
app['database']['dbname']=name;app['auth_database']['dbname']=name;app['worker_id']=str(worker)
app_path.write_text(json.dumps(app))
env=dict(os.environ,TGMATE_TEST_OWNER_CONFIG=str(owner_path),TGMATE_TEST_APP_CONFIG=str(app_path),TGMATE_CONFIG=str(app_path))
result=subprocess.run([sys.executable,'-m','pytest','tests','-q','--junitxml=TEST_RESULTS.xml'],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace')
(private/f'{name}_tests.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
report={'database':name,'production':False,'migrations':evidence,'schema_tables':table_count,'test_exit_code':result.returncode,'test_log':'private local log; excluded from delivery'}
if (root/'TEST_RESULTS.xml').exists():
 suites=ET.parse(root/'TEST_RESULTS.xml').getroot(); report.update(tests=sum(int(x.get('tests','0')) for x in suites.iter('testsuite')),failures=sum(int(x.get('failures','0')) for x in suites.iter('testsuite')),errors=sum(int(x.get('errors','0')) for x in suites.iter('testsuite')))
(root/'CLEAN_REBUILD_RESULTS.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
if result.returncode: raise SystemExit(result.returncode)
