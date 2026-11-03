"""Provision a separate RLS-bound Bot worker role with existing reviewed grants."""
import argparse
import importlib.util
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

ROOT=Path(__file__).resolve().parents[1]
ROLE='tgs_bot_worker'


def configure(db, password, worker_id, *, rotate_password=False):
    if not isinstance(password,str) or len(password)<24:
        raise ValueError('A private worker password of at least 24 characters is required')
    flags=db.execute('''SELECT rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit
        FROM pg_roles WHERE rolname=%s''',(ROLE,)).fetchone()
    if flags is None:
        db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}')
                   .format(sql.Identifier(ROLE),sql.Literal(password)))
    elif flags!=(True,False,False,False,False,False):
        raise ValueError('Unsafe worker role')
    elif rotate_password:
        db.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(ROLE),sql.Literal(password)))
    if db.execute('''SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)
        UNION ALL SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=%s) LIMIT 1''',(ROLE,ROLE)).fetchone():
        raise ValueError('Worker role must not own tables or inherit other roles')
    source=ROOT/'workstreams/tgmate-v2'
    spec=importlib.util.spec_from_file_location('preserved_worker_grants',source/'scripts/roles.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.grant_runtime(db,source,role=ROLE)
    db.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON tgmate.bot_runtime_limits TO {}').format(sql.Identifier(ROLE)))
    db.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(ROLE)))
    db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION public.lock_business_worker_tenant(uuid) TO {}').format(sql.Identifier(ROLE)))
    db.execute(sql.SQL('GRANT SELECT(id,worker_type,status) ON tgmate.worker_nodes TO {}').format(sql.Identifier(ROLE)))
    db.execute("INSERT INTO tgmate.worker_nodes(id,worker_code,worker_type,status) VALUES(%s,%s,'bot','healthy') ON CONFLICT(id) DO NOTHING",(worker_id,'bot-'+str(worker_id)))
    if db.execute('SELECT worker_type,status FROM tgmate.worker_nodes WHERE id=%s',(worker_id,)).fetchone()!=('bot','healthy'):
        raise ValueError('Worker registration is unavailable')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',type=UUID,required=True)
    parser.add_argument('--rotate-password',action='store_true')
    args=parser.parse_args(argv)
    try:
        with psycopg.connect('') as db:
            configure(db,os.environ.get('BOT_WORKER_PASSWORD'),args.worker,rotate_password=args.rotate_password)
    except (psycopg.Error,ValueError,OSError):
        parser.exit(1,'Worker provisioning rejected; transaction rolled back.\n')
    print('Bot worker role and registration configured.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
