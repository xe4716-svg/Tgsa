import hashlib,json,zipfile
from pathlib import Path
import pytest
from scripts import prepare_github as package

@pytest.fixture
def source(tmp_path,monkeypatch):
 (tmp_path/'.local').mkdir()
 sql=tmp_path/'example.sql';sql.write_text('SELECT 1;')
 (tmp_path/'README.md').write_text('Reviewed source')
 baseline={'migrations':[{'file':'example.sql','sha256':hashlib.sha256(sql.read_bytes()).hexdigest()}],'tests':1,'test_exit_code':0,'failures':0,'errors':0}
 (tmp_path/'CLEAN_REBUILD_RESULTS.json').write_text(json.dumps(baseline))
 (tmp_path/'TEST_RESULTS.xml').write_text('<testsuites><testsuite tests="1" failures="0" errors="0"/></testsuites>')
 monkeypatch.setattr(package,'candidates',lambda root:sorted(root.glob('*.md'))+[sql,root/'CLEAN_REBUILD_RESULTS.json',root/'TEST_RESULTS.xml'])
 return tmp_path

def test_reviewed_package_contains_manifest_and_no_private_files(source):
 (source/'.local/app.json').write_text('{"password":"synthetic-do-not-upload-password"}')
 package.prepare(source)
 with zipfile.ZipFile(source/'TGMate_V2_GitHub_ready.zip') as z:
  assert z.testzip() is None and 'UPLOAD_MANIFEST.json' in z.namelist()
  assert not any(n.startswith('.local/') for n in z.namelist())
 report=json.loads((source/'GITHUB_PREPARATION_REPORT.json').read_text())
 assert report['remote_operations'] is False and report['database_tests_rerun_this_preparation'] is False

def test_secret_rejection_keeps_previous_package(source):
 secret='synthetic-do-not-upload-password'
 (source/'.local/app.json').write_text(json.dumps({'password':secret}))
 (source/'README.md').write_text(secret)
 destination=source/'TGMate_V2_GitHub_ready.zip';destination.write_bytes(b'previous-reviewed-package')
 with pytest.raises(ValueError,match='credential'):package.prepare(source)
 assert destination.read_bytes()==b'previous-reviewed-package'

def test_sql_drift_does_not_reuse_old_test_claim(source):
 (source/'example.sql').write_text('SELECT 2;')
 with pytest.raises(ValueError,match='SQL no longer matches'):package.prepare(source)
 assert not (source/'TGMate_V2_GitHub_ready.zip').exists()
