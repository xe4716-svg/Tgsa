"""Run preserved V2 tests in an explicitly disposable local PostgreSQL cluster.

Requires TGMATE_V2_REGRESSION=1 and PGHOST/PGPORT/PGUSER/PGPASSWORD.
Never point this helper at a shared or production cluster: root migrations and
legacy fixtures create cluster-level roles. No real Telegram calls are made.
"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from uuid import uuid4

import psycopg
from psycopg import sql
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.getenv('TGMATE_V2_REGRESSION') != '1' or os.getenv('PGHOST') not in ('localhost', '127.0.0.1', '::1'):
        raise SystemExit('Explicit disposable local cluster opt-in required')
    config = dict(host=os.environ['PGHOST'], port=int(os.getenv('PGPORT', '5432')),
                  user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'], dbname='postgres')
    name = 'tgs_v2_ci_' + uuid4().hex[:12]
    source = ROOT / 'workstreams/tgmate-v2'
    report = ROOT / 'docs/validation/work-v2.xml'
    env = dict(os.environ, PGDATABASE=name, APP_DB_PASSWORD=secrets.token_urlsafe(32))
    # Refuse existing application/auth roles rather than changing their passwords.
    with psycopg.connect(**config, autocommit=True) as admin:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname IN ('tgmate_app','tgmate_auth','tgmate_auth_owner','tgmate_test_runtime')").fetchone():
            raise SystemExit('Use a fresh dedicated cluster; existing V2 roles are unchanged')
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            result = subprocess.run([sys.executable, str(ROOT/'scripts/bootstrap_combined.py')], env=env, capture_output=True)
            if result.returncode:
                raise RuntimeError('Combined initialization failed')
            owner = dict(config, dbname=name)
            with tempfile.TemporaryDirectory(prefix='tgmate-v2-tests-') as temp:
                temp = Path(temp)
                password = secrets.token_urlsafe(32)
                spec = importlib.util.spec_from_file_location('v2_roles', source/'scripts/roles.py')
                roles = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(roles)
                with psycopg.connect(**owner) as db:
                    for role in ('tgmate_app', 'tgmate_auth'):
                        db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
                    roles.grant_runtime(db, source)
                    roles.grant_auth(db)
                    worker = uuid4()
                    db.execute("INSERT INTO tgmate.worker_nodes(id,worker_code,worker_type,status) VALUES(%s,%s,'relay','healthy')", (worker,str(worker)))
                app = dict(database=dict(owner,user='tgmate_app',password=password),
                           auth_database=dict(owner,user='tgmate_auth',password=password),
                           auth_rate_key=secrets.token_urlsafe(32), vault_key=Fernet.generate_key().decode(),
                           worker_id=str(worker), mode='sandbox', api_keys={},webhooks={},bot_tokens={})
                for path, content in [(temp/'owner.json',owner), (temp/'app.json',app)]:
                    path.write_text(json.dumps(content))
                    path.chmod(0o600)
                env.update(TGMATE_TEST_OWNER_CONFIG=str(temp/'owner.json'), TGMATE_TEST_APP_CONFIG=str(temp/'app.json'))
                result = subprocess.run([sys.executable,'-m','pytest','tests','-q',f'--junitxml={report}'],cwd=source,env=env)
                return result.returncode
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in ('tgmate_test_runtime','tgmate_auth','tgmate_app','tgmate_auth_owner'):
                admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


if __name__ == '__main__':
    raise SystemExit(main())
