from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
import hashlib,json
import xml.etree.ElementTree as ET
root=Path(__file__).resolve().parents[1]
files=[p for p in root.glob('*') if p.suffix in {'.md','.sql'}]
files += [root/'.gitignore']
files += [root/n for n in ['requirements.txt','validate_sql.py','VALIDATION_REPORT.json','DATABASE_MIGRATION_RESULTS.json','BACKUP_RESTORE_RESULTS.json','HTTP_SMOKE_RESULTS.json','CLEAN_REBUILD_RESULTS.json','TEST_RESULTS.xml','tgmate_v2_004_static_check.txt']]
if (root/'source_001_003.zip').exists(): files.append(root/'source_001_003.zip')
for folder in ['source_001_003','app','tests']:
 files += [p for p in (root/folder).rglob('*') if p.is_file() and p.suffix in {'.py','.html','.sql','.txt'} and '__pycache__' not in p.parts]
files += [root/'scripts'/n for n in ['migrate_test_database.py','bootstrap_local.py','verify_backup_restore.py','run_worker_once.py','run_broadcast_once.py','run_moderation_once.py','bootstrap_auth.py','roles.py','verify_clean_rebuild.py','package_delivery.py']]
for name in ['RUNTIME_CREDENTIAL_TEST_RESULTS.xml','WEBHOOK_TEST_RESULTS.xml','MODERATION_CONTROL_TEST_RESULTS.xml','RELAY_OPERATOR_TEST_RESULTS.xml','RELAY_PRIVACY_TEST_RESULTS.xml']:
 p=root/name
 if p.exists():
  extra=ET.parse(p).getroot()
  assert not any(int(x.get('failures','0')) or int(x.get('errors','0')) for x in extra.iter('testsuite')), 'Failing focused tests'
  files.append(p)
manifest={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
secrets=[]
def collect(value):
 if isinstance(value,dict):
  for key,item in value.items():
   if key in ('password','secret','vault_key','api_hash','string_session','api_key','auth_rate_key') and isinstance(item,str): secrets.append(item)
   elif key=='api_keys' and isinstance(item,dict): secrets.extend(item.keys())
   elif key=='bot_tokens' and isinstance(item,dict): secrets.extend(v for v in item.values() if isinstance(v,str))
   collect(item)
 elif isinstance(value,list):
  for item in value: collect(item)
for name in ['app.json','db.json','owner_login.json']:
 p=root/'.local'/name
 if p.exists(): collect(json.loads(p.read_text()))
suite=ET.parse(root/'TEST_RESULTS.xml').getroot()
assert sum(int(item.get('tests','0')) for item in suite.iter('testsuite'))>0, 'No executed test evidence'
assert not any(int(item.get('failures','0')) or int(item.get('errors','0')) for item in suite.iter('testsuite')), 'Failing tests; delivery not replaced'
staging=root/'.local/delivery_candidate.zip'; staging.parent.mkdir(exist_ok=True)
try:
 with ZipFile(staging,'w',ZIP_DEFLATED) as z:
  for p in files: z.write(p,p.relative_to(root).as_posix())
  z.writestr('MANIFEST.json',json.dumps(manifest,indent=2))
 with ZipFile(staging) as z:
  assert z.testzip() is None
  assert not any(n.startswith(('.local/','.tools/')) or n.endswith('.dump') for n in z.namelist())
  for name in z.namelist():
   data=z.read(name)
   assert not any(secret.encode() in data for secret in secrets if secret), 'Secret detected; existing delivery package unchanged'
 staging.replace(root/'TGMate_V2_review_bundle.zip')
 print('Package integrity and credential exclusion passed; delivery replaced atomically.')
finally:
 if staging.exists(): staging.unlink()
