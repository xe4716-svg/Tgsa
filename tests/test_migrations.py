from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import pytest
from scripts.migrate import build_script, migration_body, write_manifest


def test_released_files_have_single_atomic_wrapper():
    root = Path(__file__).resolve().parents[1]
    script = build_script(root / 'migrations')
    manifest = json.loads((root / 'migrations' / 'manifest.json').read_text(encoding='utf-8'))
    expected_count = len(manifest['migrations'])
    names = [item['name'] for item in manifest['migrations']]
    assert names == sorted(names)
    assert len(names) == len(list((root / 'migrations').glob('*.sql')))
    assert script.count('\nBEGIN;\n') == expected_count
    assert script.count('\nCOMMIT;\n') == expected_count
    assert script.index('pg_advisory_lock') < script.index('CREATE TABLE')
    assert 'Migration hash drift' in script
    assert '\\getenv app_db_password APP_DB_PASSWORD' in script
    assert script.index('INSERT INTO public.schema_migrations') < script.index('\nCOMMIT;')
    assert script.index('Applied migration missing or hash drift') < script.index('\\if :applied')
    assert 'expected.name IS NULL' in script


def test_hash_changes_with_source(tmp_path):
    path = tmp_path / '001_test.sql'
    path.write_text('SELECT 1;')
    first = build_script(tmp_path, manifest=['001_test.sql'])
    path.write_text('SELECT 2;')
    assert first != build_script(tmp_path, manifest=['001_test.sql'])


def _copy_root_migrations(tmp_path):
    root = Path(__file__).resolve().parents[1] / 'migrations'
    for path in root.glob('*.sql'):
        shutil.copy2(path, tmp_path / path.name)
    shutil.copy2(root / 'manifest.json', tmp_path / 'manifest.json')
    return tmp_path


def test_manifest_writer_is_sorted_and_deterministic(tmp_path):
    second = tmp_path / '002_second.sql'
    first = tmp_path / '001_first.sql'
    second.write_bytes(b'SELECT 2;\\n')
    first.write_bytes(b'SELECT 1;\\n')
    manifest_path = write_manifest(tmp_path)
    original = manifest_path.read_text(encoding='utf-8')
    payload = json.loads(original)
    assert [item['name'] for item in payload['migrations']] == [
        '001_first.sql', '002_second.sql'
    ]
    assert payload['migrations'][0]['sha256'] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert write_manifest(tmp_path).read_text(encoding='utf-8') == original


def test_manifest_writer_rejects_symlink_without_touching_target(tmp_path):
    (tmp_path / '001_test.sql').write_text('SELECT 1;', encoding='utf-8')
    target = tmp_path / 'outside.txt'
    target.write_text('keep me', encoding='utf-8')
    manifest_path = tmp_path / 'manifest.json'
    try:
        manifest_path.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f'Symlinks are unavailable: {exc}')

    with pytest.raises(ValueError, match='regular file'):
        write_manifest(tmp_path)

    assert manifest_path.is_symlink()
    assert target.read_text(encoding='utf-8') == 'keep me'


def test_manifest_writer_rejects_non_regular_destination(tmp_path):
    (tmp_path / '001_test.sql').write_text('SELECT 1;', encoding='utf-8')
    (tmp_path / 'manifest.json').mkdir()

    with pytest.raises(ValueError, match='regular file'):
        write_manifest(tmp_path)


def test_manifest_rejects_missing_manifest(tmp_path):
    (tmp_path / '001_test.sql').write_text('SELECT 1;')
    with pytest.raises(ValueError, match='manifest missing'):
        build_script(tmp_path)


def test_manifest_rejects_missing_tail_sql(tmp_path):
    _copy_root_migrations(tmp_path)
    (tmp_path / '012_business_worker_guard.sql').unlink()
    with pytest.raises(ValueError, match='do not match manifest'):
        build_script(tmp_path)


def test_manifest_rejects_untracked_tail_sql(tmp_path):
    _copy_root_migrations(tmp_path)
    (tmp_path / '999_untracked.sql').write_text('SELECT 1;')
    with pytest.raises(ValueError, match='do not match manifest'):
        build_script(tmp_path)


def test_manifest_rejects_content_drift(tmp_path):
    _copy_root_migrations(tmp_path)
    with (tmp_path / '001_core.sql').open('a', encoding='utf-8') as stream:
        stream.write(chr(10) + '-- untracked change' + chr(10))
    with pytest.raises(ValueError, match='manifest hash mismatch'):
        build_script(tmp_path)


@pytest.mark.parametrize('text', ['BEGIN;\nSELECT 1;', 'COMMIT;', 'SELECT 1;\nBEGIN;\nCOMMIT;'])
def test_rejects_partial_wrappers(text):
    with pytest.raises(ValueError):
        migration_body(text)


def test_preserves_body():
    assert migration_body('-- heading\nBEGIN;\nSELECT 1;\nCOMMIT;') == '-- heading\nSELECT 1;'


@pytest.mark.skipif(os.getenv('TGMATE_MIGRATION_INTEGRATION') != '1',
                    reason='Requires an explicitly configured disposable CI database')
def test_database_migration_lifecycle(tmp_path):
    # Opt-in only: caller supplies a fresh disposable database through PG*.
    root = Path(__file__).resolve().parents[1]
    psql = os.getenv('PG_PSQL', 'psql')
    env = dict(os.environ, PGCLIENTENCODING='UTF8')

    def migrate(directory):
        return subprocess.run(
            [sys.executable, str(root / 'scripts/migrate.py'), '--psql', psql,
             '--directory', str(directory)], env=env, capture_output=True).returncode

    def query(statement):
        result = subprocess.run([psql, '-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1',
                                 '-c', statement], env=env, capture_output=True)
        assert result.returncode == 0, 'Database verification query failed'
        return result.stdout.decode('utf-8').strip()

    source = root / 'migrations'
    assert query("SELECT to_regclass('public.schema_migrations') IS NULL") == 't', \
        'Integration test requires a fresh disposable database'
    assert migrate(source) == 0, 'Initial migration failed'
    assert migrate(source) == 0, 'Idempotent migration failed'
    assert query('SELECT count(*) FROM public.schema_migrations') == str(len(list(source.glob('*.sql'))))
    for path in source.glob('*.sql'):
        shutil.copy2(path, tmp_path / path.name)
    write_manifest(tmp_path)
    first = tmp_path / '001_core.sql'
    original = first.read_bytes()
    first.write_bytes(original + b'\n-- hash drift probe\n')
    probe = tmp_path / '999_probe.sql'
    probe.write_text('CREATE TABLE public.migration_preflight_probe(id int);', encoding='utf-8')
    write_manifest(tmp_path)
    assert migrate(tmp_path) != 0, 'Hash drift was accepted'
    assert query("SELECT to_regclass('public.migration_preflight_probe') IS NULL") == 't'
    first.unlink()
    write_manifest(tmp_path)
    assert migrate(tmp_path) != 0, 'Missing applied migration was accepted'
    assert query("SELECT to_regclass('public.migration_preflight_probe') IS NULL") == 't'
    first.write_bytes(original)
    probe.write_text('CREATE TABLE public.migration_rollback_probe(id int); SELECT 1/0;', encoding='utf-8')
    write_manifest(tmp_path)
    assert migrate(tmp_path) != 0, 'Invalid migration unexpectedly succeeded'
    assert query("SELECT to_regclass('public.migration_rollback_probe') IS NULL") == 't'
    assert query("SELECT count(*) FROM public.schema_migrations WHERE name='999_probe.sql'") == '0'
