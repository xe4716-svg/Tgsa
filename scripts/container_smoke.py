"""Boot an isolated Compose test project; never reuse a deployment's env file."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.migrate_business import FILES

def main():
    root = Path(__file__).resolve().parents[1]
    runtime = root/'.env.runtime'
    if runtime.exists():
        print('Smoke test requires a checkout without .env.runtime.', file=sys.stderr)
        return 1
    project = 'tgmate_ci_' + uuid4().hex[:12]
    app_password = secrets.token_urlsafe(32)
    env = dict(os.environ, POSTGRES_PASSWORD=secrets.token_urlsafe(32),
               APP_DB_PASSWORD=app_password, POSTGRES_DB='tgs', API_PORT='0', MAILPIT_PORT='0')
    contents = '\n'.join([
        'APP_ENV=development', 'SMTP_HOST=mailpit', 'SMTP_PORT=1025',
        'DATABASE_URL=postgresql://tgs_runtime:'+app_password+'@db:5432/tgs',
        'REDIS_URL=redis://redis:6379/0',
        'JWT_SIGNING_SECRET='+secrets.token_urlsafe(48),
        'SESSION_KEK_BASE64='+base64.b64encode(secrets.token_bytes(32)).decode(),
    ])+'\n'
    try:
        with runtime.open('x', encoding='utf-8') as output:
            output.write(contents)
    except FileExistsError:
        print('Smoke test refuses to replace an existing runtime file.', file=sys.stderr)
        return 1
    base = ['docker','compose','-p',project,'--file',str(root/'docker-compose.yml')]
    def run(arguments, stage):
        result = subprocess.run(base+arguments, cwd=root, env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(stage)
        return result.stdout
    try:
        config = json.loads(run(['config','--format','json'], 'compose configuration'))
        for service in ('api','billing'):
            assert 'POSTGRES_PASSWORD' not in config['services'][service].get('environment', {})
            assert 'APP_DB_PASSWORD' not in config['services'][service].get('environment', {})
        assert config['services']['api']['depends_on']['migrate']['condition']=='service_completed_successfully'
        run(['up','--build','--wait','--wait-timeout','240','api','billing'], 'container startup and readiness')
        expected = str(len(list((root/'migrations').glob('*.sql'))))+'|'+str(len(FILES))
        actual = run(['exec','-T','db','psql','-U','postgres','-d','tgs','-Atqc',
                      "SELECT (SELECT count(*) FROM public.schema_migrations)::text || '|' || (SELECT count(*) FROM public.business_schema_migrations)::text"],
                     'schema ledgers').strip()
        assert actual == expected
        run(['run','--rm','--no-deps','migrate'], 'idempotent migration replay')
        probe = "import urllib.request,urllib.error,json;\ntry: urllib.request.urlopen('http://127.0.0.1:8000/api/v1/relay/conversations')\nexcept urllib.error.HTTPError as e: assert e.code==401 and json.load(e)['success'] is False\nelse: raise AssertionError('Unauthenticated access accepted')"
        run(['exec','-T','api','python','-c',probe], 'API authorization probe')
        print('Compose readiness, isolated secrets, schema initialization/replay and API authorization passed.')
        return 0
    except (OSError, RuntimeError, AssertionError, ValueError, KeyError) as exc:
        print('Compose smoke test failed: '+(str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__), file=sys.stderr)
        return 1
    finally:
        try:
            subprocess.run(base+['down','--volumes','--remove-orphans'], cwd=root, env=env, capture_output=True)
        except OSError:
            pass
        finally:
            runtime.unlink(missing_ok=True)

if __name__=='__main__':
    raise SystemExit(main())
