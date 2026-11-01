from pathlib import Path
import subprocess,sys,zipfile
ROOT=Path(__file__).resolve().parents[1]


def workspace(tmp_path,leak=False):
 (tmp_path/'scripts').mkdir(); (tmp_path/'.local').mkdir()
 source=(ROOT/'scripts/package_delivery.py').read_text(encoding='utf-8-sig')
 (tmp_path/'scripts/package_delivery.py').write_text(source,encoding='utf-8')
 root_files=['.gitignore','requirements.txt','validate_sql.py','VALIDATION_REPORT.json','DATABASE_MIGRATION_RESULTS.json','BACKUP_RESTORE_RESULTS.json','HTTP_SMOKE_RESULTS.json','CLEAN_REBUILD_RESULTS.json','TEST_RESULTS.xml','source_001_003.zip','tgmate_v2_004_static_check.txt']
 script_files=['migrate_test_database.py','bootstrap_local.py','verify_backup_restore.py','run_worker_once.py','run_broadcast_once.py','run_moderation_once.py','bootstrap_auth.py','roles.py','verify_clean_rebuild.py']
 for name in root_files: (tmp_path/name).write_text('test artifact')
 for name in script_files: (tmp_path/'scripts'/name).write_text('# test fixture')
 (tmp_path/'.local/app.json').write_text('{"vault_key":"synthetic-private-key-for-test"}')
 (tmp_path/'TEST_RESULTS.xml').write_text('<testsuites><testsuite tests="1" failures="0" errors="0"/></testsuites>')
 (tmp_path/'README.md').write_text('synthetic-private-key-for-test' if leak else 'safe documentation')
 return tmp_path


def test_package_never_replaces_delivery_with_detected_secret(tmp_path):
 root=workspace(tmp_path,True); delivery=root/'TGMate_V2_review_bundle.zip'; delivery.write_bytes(b'previous-reviewed-delivery')
 result=subprocess.run([sys.executable,str(root/'scripts/package_delivery.py')],capture_output=True,text=True)
 assert result.returncode!=0 and 'Secret detected' in result.stderr
 assert delivery.read_bytes()==b'previous-reviewed-delivery'
 assert not (root/'.local/delivery_candidate.zip').exists()


def test_package_manifest_and_local_exclusion(tmp_path):
 root=workspace(tmp_path); result=subprocess.run([sys.executable,str(root/'scripts/package_delivery.py')],capture_output=True,text=True)
 assert result.returncode==0
 with zipfile.ZipFile(root/'TGMate_V2_review_bundle.zip') as z:
  assert z.testzip() is None and 'MANIFEST.json' in z.namelist()
  assert 'scripts/roles.py' in z.namelist() and 'scripts/bootstrap_auth.py' in z.namelist()
  assert not any(name.startswith('.local/') for name in z.namelist())
