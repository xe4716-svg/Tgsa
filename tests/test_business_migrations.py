from pathlib import Path
import shutil
import os
import json
import hashlib
import subprocess
import sys
from uuid import uuid4
import pytest
from scripts.migrate_business import build_script, FILES

ROOT = Path(__file__).resolve().parents[1] / 'workstreams/tgmate-v2'


def test_release_order_and_isolated_ledger():
    script = build_script(ROOT)
    assert script.count('\nBEGIN;\n') == len(FILES)
    assert script.count('\nCOMMIT;\n') == len(FILES)
    assert 'pg_advisory_lock(847293018422)' in script
    assert 'pg_advisory_lock(847293018421)' not in script
    positions = [script.index(f"name='{i:03d}_{Path(name).name}'") for i, name in enumerate(FILES, 1)]
    assert positions == sorted(positions)
    assert 'INSERT INTO public.schema_migrations' not in script
    assert script.count('INSERT INTO public.business_schema_migrations') == len(FILES)
    assert script.index('Root identity migration required') < script.index('CREATE TABLE IF NOT EXISTS')
    assert script.index('Untracked business schema') < script.index('CREATE TABLE IF NOT EXISTS')


def test_missing_release_refused(tmp_path):
    with pytest.raises(ValueError, match='missing'):
        build_script(tmp_path)


def test_drift_changes_expected_hashes(tmp_path):
    for name in FILES:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    before = build_script(tmp_path)
    path.write_bytes(path.read_bytes() + b'\n-- changed\n')
    assert build_script(tmp_path) != before
    assert 'Applied migration missing or hash drift' in before


def test_cli_sanitizes_errors(monkeypatch, capsys):
    from scripts import migrate_business as module
    def fail(*args, **kwargs):
        raise OSError('private connection details')
    monkeypatch.setattr(module.subprocess, 'run', fail)
    assert module.main(['--directory', str(ROOT)]) == 1
    assert 'private connection details' not in capsys.readouterr().err


@pytest.mark.skipif(os.getenv('TGMATE_BUSINESS_MIGRATION_INTEGRATION') != '1',
                    reason='Disposable cluster opt-in required')
def test_real_business_migration_lifecycle(tmp_path):
    psycopg = pytest.importorskip('psycopg')
    from psycopg import sql
    root = ROOT.parents[1]
    config = dict(host=os.getenv('PGHOST', 'localhost'), port=int(os.getenv('PGPORT', '5432')),
                  user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'], dbname='postgres')
    psql = os.getenv('PG_PSQL', 'psql')
    names = ['tgs_business_' + uuid4().hex[:12] for _ in range(5)]
    password = __import__('secrets').token_urlsafe(32)
    evidence = {'production': False, 'databases': names, 'actual_postgresql': True}

    def run(script, database, source=None):
        command = [sys.executable, str(root / 'scripts' / script), '--psql', psql]
        if source is not None:
            command += ['--directory', str(source)]
        return subprocess.run(command, capture_output=True, env=dict(os.environ,
            PGDATABASE=database, PGCLIENTENCODING='UTF8', APP_DB_PASSWORD=password)).returncode

    with psycopg.connect(**config, autocommit=True) as admin:
        old = admin.execute("SELECT rolpassword FROM pg_authid WHERE rolname='tgs_runtime'").fetchone()
        for name in names:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            assert run('migrate_business.py', names[1]) != 0, 'Missing root was accepted'
            for name in (names[0], names[2], names[3]):
                assert run('migrate.py', name) == 0, 'Root initialization failed'
            assert run('migrate_business.py', names[0]) == 0, 'Initial business migration failed'
            assert run('migrate_business.py', names[0]) == 0, 'Repeat migration failed'
            with psycopg.connect(**dict(config, dbname=names[0]), autocommit=True) as db:
                assert db.execute('SELECT count(*) FROM public.schema_migrations').fetchone()[0] == 10
                ledger = db.execute('SELECT name,sha256 FROM public.business_schema_migrations ORDER BY name').fetchall()
                expected = [(f'{i:03d}_{Path(n).name}', hashlib.sha256((ROOT/n).read_bytes()).hexdigest()) for i,n in enumerate(FILES,1)]
                assert ledger == expected
                shutil.copytree(ROOT, tmp_path/'source', ignore=shutil.ignore_patterns('__pycache__', '.local'))
                changed = tmp_path/'source'/FILES[-1]
                changed.write_bytes(changed.read_bytes()+b'\n-- drift\n')
                assert run('migrate_business.py', names[0], tmp_path/'source') != 0
                assert db.execute('SELECT name,sha256 FROM public.business_schema_migrations ORDER BY name').fetchall() == ledger
            with psycopg.connect(**dict(config,dbname=names[2]),autocommit=True) as db:
                db.execute('CREATE SCHEMA tgmate')
            assert run('migrate_business.py', names[2]) != 0, 'Untracked schema was accepted'
            changed.write_text('BEGIN;\nCREATE TABLE public.business_rollback_probe(id int);\nSELECT 1/0;\nCOMMIT;\n',encoding='utf-8')
            assert run('migrate_business.py', names[3],tmp_path/'source') != 0
            with psycopg.connect(**dict(config,dbname=names[3]),autocommit=True) as db:
                assert db.execute('SELECT count(*) FROM public.business_schema_migrations').fetchone()[0] == 11
                assert db.execute("SELECT to_regclass('public.business_rollback_probe')").fetchone()[0] is None
            assert run('migrate_business.py', names[3]) == 0, 'Retry after rollback failed'
            assert run('bootstrap_combined.py', names[4]) == 0, 'Combined initialization failed'
            assert run('bootstrap_combined.py', names[4]) == 0, 'Combined repeat failed'
            with psycopg.connect(**dict(config,dbname=names[4]),autocommit=True) as db:
                assert db.execute('SELECT count(*) FROM public.schema_migrations').fetchone()[0] == 10
                assert db.execute('SELECT count(*) FROM public.business_schema_migrations').fetchone()[0] == 12
                for table, column in [('tgmate.users','email'),('tgmate.web_credentials','password_hash'),
                                      ('tgmate.relay_conversations','customer_user_id')]:
                    assert db.execute("SELECT has_column_privilege('tgs_runtime',%s,%s,'SELECT')",(table,column)).fetchone()[0] is False
                for table, column in [('tgmate.users','id'),('tgmate.users','status'),
                                      ('tgmate.relay_conversations','id'),('tgmate.relay_conversations','status')]:
                    assert db.execute("SELECT has_column_privilege('tgs_runtime',%s,%s,'SELECT')",(table,column)).fetchone()[0] is True
                for table in ['integration_tenant_map','integration_user_map','integration_member_map']:
                    for privilege in ['INSERT','UPDATE','DELETE']:
                        assert db.execute("SELECT has_table_privilege('tgs_runtime',%s,%s)",(table,privilege)).fetchone()[0] is False
                with db.transaction():
                    db.execute('SET LOCAL ROLE tgs_runtime')
                    assert db.execute('SELECT id,status FROM tgmate.relay_conversations').fetchall() == []
            evidence.update(passed=True, business_ledger_count=12, root_ledger_count=10,
                checks=['first','repeat','original SQL hashes','drift rejection','missing root rejection',
                        'untracked schema rejection','failed file rollback','resume after failure',
                        'combined bootstrap first/repeat','sensitive columns denied','metadata columns readable',
                        'mapping writes denied'],
                source_sha256={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in FILES})
        finally:
            if old:
                admin.execute(sql.SQL('ALTER ROLE tgs_runtime PASSWORD {}').format(sql.Literal(old[0])))
    target = os.getenv('TGMATE_BUSINESS_MIGRATION_REPORT')
    if target:
        Path(target).write_text(json.dumps(evidence,indent=2),encoding='utf-8')
