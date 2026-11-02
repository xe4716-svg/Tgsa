"""Prepare an explicit, reviewed source ZIP. Never touches Git or the network."""
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
import hashlib,json,re,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
DOCS=['README.md','PROJECT_PLAN.md','ROADMAP_PROGRESS.md','WORKFLOW.md','BACKLOG.md','GITHUB_READINESS.md','DELIVERY_STATUS.md','FIRST_RELEASE_SCOPE.md','USDT_PAYMENT_DESIGN.md','AUTH_RUNTIME.md','BOT_CREDENTIALS.md','BOT_WEBHOOKS.md','BOT_GATEWAY_APPEALS.md','BROADCAST_RUNTIME.md','MODERATION_RUNTIME.md','MODERATION_CONTROL.md','VERIFICATION_RUNTIME.md','RELAY_OPERATOR_AUTH.md','RELAY_PRIVACY_NOTICE.md','tgmate_v2_004_bidirectional_design.md','004_INTEGRATION_CONTRACT.md']
SCRIPTS=['roles.py','verify_clean_rebuild.py','verify_backup_restore.py','migrate_test_database.py','bootstrap_local.py','bootstrap_auth.py','run_worker_once.py','run_broadcast_once.py','run_moderation_once.py','package_delivery.py','prepare_github.py']
EVIDENCE=['GITHUB_PREPARATION_TEST_RESULTS.xml','CLEAN_REBUILD_RESULTS.json','TEST_RESULTS.xml','VALIDATION_REPORT.json','DATABASE_MIGRATION_RESULTS.json','BACKUP_RESTORE_RESULTS.json','HTTP_SMOKE_RESULTS.json','RELAY_PRIVACY_TEST_RESULTS.xml','RELAY_OPERATOR_TEST_RESULTS.xml','MODERATION_CONTROL_TEST_RESULTS.xml','WEBHOOK_TEST_RESULTS.xml','RUNTIME_CREDENTIAL_TEST_RESULTS.xml','tgmate_v2_004_static_check.txt']

def candidates(root):
 files=[root/n for n in DOCS+EVIDENCE+['.gitignore','requirements.txt','validate_sql.py']]
 files += sorted(root.glob('004[a-h]_*.sql'))+[root/'tgmate_v2_004_bidirectional.sql']
 files += [root/'scripts'/n for n in SCRIPTS]
 for folder,extensions in [('app',{'.py','.html'}),('tests',{'.py'}),('source_001_003',{'.sql','.txt'}),('.github',{'.md','.yml','.yaml'})]:
  files += sorted(p for p in (root/folder).rglob('*') if p.is_file() and p.suffix in extensions and '__pycache__' not in p.parts)
 files=sorted(set(files))
 for p in files:
  if not p.is_file() or p.is_symlink(): raise ValueError('Missing or linked upload source: '+p.name)
  if p.resolve()!=p.absolute(): raise ValueError('Resolved source differs from reviewed path')
 return files

def known_secrets(root):
 values=set()
 def collect(value):
  if isinstance(value,dict):
   for key,item in value.items():
    if key in {'password','secret','vault_key','api_hash','string_session','api_key','auth_rate_key','token'} and isinstance(item,str) and len(item)>=6: values.add(item.encode())
    if key=='api_keys' and isinstance(item,dict): values.update(x.encode() for x in item if len(x)>=6)
    if key=='bot_tokens' and isinstance(item,dict): values.update(x.encode() for x in item.values() if isinstance(x,str) and len(x)>=6)
    collect(item)
  elif isinstance(value,list):
   for item in value: collect(item)
 for p in (root/'.local').rglob('*.json'):
  try: collect(json.loads(p.read_text(encoding='utf-8-sig')))
  except (ValueError,UnicodeError): raise ValueError('Cannot scan private configuration safely') from None
 return values

def prepare(root=ROOT):
 files=candidates(root);secrets=known_secrets(root)
 manifest={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
 for p in files:
  data=p.read_bytes()
  if any(s in data for s in secrets) or re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data) or re.search(rb'\b[1-9][0-9]{5,15}:[A-Za-z0-9_-]{30,}\b',data): raise ValueError('Potential credential in reviewed source; package not created: '+p.name)
 baseline=json.loads((root/'CLEAN_REBUILD_RESULTS.json').read_text(encoding='utf-8'))
 if not baseline.get('migrations') or baseline.get('test_exit_code')!=0 or baseline.get('failures',0) or baseline.get('errors',0): raise ValueError('No successful executed database baseline')
 for row in baseline['migrations']:
  path=root/'source_001_003'/row['file'] if row['file'].startswith(('001_','002_','003_')) else root/row['file']
  if path not in files: raise ValueError('Executed migration missing from upload manifest')
  if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']: raise ValueError('SQL no longer matches executed baseline')
 suites=ET.parse(root/'TEST_RESULTS.xml').getroot()
 count=sum(int(s.get('tests','0')) for s in suites.iter('testsuite'))
 if count!=baseline['tests'] or any(int(s.get('failures','0')) or int(s.get('errors','0')) for s in suites.iter('testsuite')): raise ValueError('Baseline test evidence inconsistent')
 staging=root/'.local/github_upload_candidate.zip';staging.parent.mkdir(exist_ok=True)
 try:
  with ZipFile(staging,'w',ZIP_DEFLATED) as z:
   for p in files: z.write(p,p.relative_to(root).as_posix())
   z.writestr('UPLOAD_MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
  with ZipFile(staging) as z:
   if z.testzip() is not None: raise ValueError('Archive integrity failed')
   if any(n.split('/')[0] in {'.local','.tools','.git','relay_mvp'} or n.endswith(('.session','.dump','.zip')) for n in z.namelist()): raise ValueError('Excluded artifact entered package')
  destination=root/'TGMate_V2_GitHub_ready.zip';staging.replace(destination)
  report={'status':'prepared_locally_not_uploaded','file_count':len(files),'archive':destination.name,'archive_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'checks':['explicit_allowlist','known_local_credentials_excluded','private_key_and_bot_token_patterns','zip_crc','executed_sql_hashes','baseline_junit_count'],'baseline_tests':count,'database_tests_rerun_this_preparation':False,'github_actions_executed':False,'remote_operations':False,'missing_before_production':['reproducible_setup','database_CI','full_console','continuous_workers','physical_retention_cleanup','latest_restore','real_Telegram_validation','commercial_payment_flow']}
  (root/'GITHUB_PREPARATION_REPORT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps(report,ensure_ascii=False,indent=2))
 finally:
  if staging.exists(): staging.unlink()

if __name__=='__main__': prepare()
