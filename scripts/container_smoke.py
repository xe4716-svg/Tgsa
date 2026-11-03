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
    private_files=[runtime,root/'.env.bot.runtime',root/'.env.mtproto.runtime']
    owned_files=[]
    if any(path.exists() for path in private_files):
        print('Smoke test requires a checkout without private runtime files.', file=sys.stderr)
        return 1
    project = 'tgmate_ci_' + uuid4().hex[:12]
    app_password = secrets.token_urlsafe(32)
    env = dict(os.environ, POSTGRES_PASSWORD=secrets.token_urlsafe(32),
               APP_DB_PASSWORD=app_password, POSTGRES_DB='tgs', API_PORT='0', MAILPIT_PORT='0',BOT_PORT='0',
               BOT_WORKER_PASSWORD=secrets.token_urlsafe(32))
    contents = '\n'.join([
        'APP_ENV=development', 'SMTP_HOST=mailpit', 'SMTP_PORT=1025',
        'DATABASE_URL=postgresql://tgs_runtime:'+app_password+'@db:5432/tgs',
        'REDIS_URL=redis://redis:6379/0',
        'JWT_SIGNING_SECRET='+secrets.token_urlsafe(48),
        'SESSION_KEK_BASE64='+base64.b64encode(secrets.token_bytes(32)).decode(),
    ])+'\n'
    try:
        with runtime.open('x', encoding='utf-8') as output:
            owned_files.append(runtime)
            runtime.chmod(0o600)
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
        # Empty, mapped synthetic tenants make both optional workers exercise
        # real startup/DB paths without creating any Telegram clients or jobs.
        tenant,business,worker=[uuid4() for _ in range(3)]
        run(['exec','-T','db','psql','-U','postgres','-d','tgs','-v','ON_ERROR_STOP=1','-c',
             f"INSERT INTO public.tenants(id,name) VALUES('{tenant}','CI worker fixture'); "
             f"INSERT INTO tgmate.tenants(id,name,slug) VALUES('{business}','CI worker fixture','{business}'); "
             f"INSERT INTO public.integration_tenant_map(tenant_id,business_tenant_id) VALUES('{tenant}','{business}');"],
            'worker fixture mapping')
        run(['run','--rm','--no-deps','-e','BOT_WORKER_PASSWORD','migrate','python',
             'scripts/configure_bot_worker.py','--worker',str(worker)], 'restricted Bot worker provisioning')
        worker_configs={
            root/'.env.bot.runtime':[
                'BOT_WORKER_DATABASE_URL=postgresql://tgs_bot_worker:'+env['BOT_WORKER_PASSWORD']+'@db:5432/tgs',
                'BUSINESS_VAULT_KEY='+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                'BOT_WORKER_ID='+str(worker),'BOT_TENANT_IDS='+str(business)],
            root/'.env.mtproto.runtime':[
                'DATABASE_URL=postgresql://tgs_runtime:'+app_password+'@db:5432/tgs',
                'TG_API_ID=12345','TG_API_HASH='+'a'*32,
                'SESSION_KEK_BASE64='+base64.b64encode(secrets.token_bytes(32)).decode(),
                'GATEWAY_TENANT_IDS='+str(tenant)]}
        for path,values in worker_configs.items():
            with path.open('x',encoding='utf-8') as output:
                owned_files.append(path);path.chmod(0o600)
                output.write('\n'.join(values)+'\n')
        base+=['--file',str(root/'docker-compose.workers.yml')]
        config=json.loads(run(['config','--format','json'],'worker Compose configuration'))
        for service in ('bot','mtproto'):
            assert not {'POSTGRES_PASSWORD','APP_DB_PASSWORD','BOT_WORKER_PASSWORD'}.intersection(config['services'][service].get('environment',{}))
        assert config['services']['bot']['environment']['BOT_WORKER_DATABASE_URL'].startswith('postgresql://tgs_bot_worker:')
        run(['up','--build','--wait','--wait-timeout','180','bot','mtproto'],'worker startup and readiness')
        bot_probe="import urllib.request,urllib.error;\ntry: urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/webhooks/bots/00000000-0000-0000-0000-000000000000', data=b'{}', headers={'Content-Type':'application/json'}))\nexcept urllib.error.HTTPError as e: assert e.code==403\nelse: raise AssertionError('Unauthenticated webhook accepted')"
        run(['exec','-T','bot','python','-c',bot_probe],'Bot webhook authorization')
        run(['exec','-T','mtproto','python','-c','import app.workers.gateway'], 'live empty-route supervisor process')
        print('Compose API/Bot/MTProto readiness, isolated secrets, schema replay and authorization passed; no Telegram jobs or clients created.')
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
            for path in owned_files:
                path.unlink(missing_ok=True)

if __name__=='__main__':
    raise SystemExit(main())
