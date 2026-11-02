"""Explicitly grant relay state control columns, never message contents."""
import argparse
import re
import subprocess
import sys

def grant_sql(role='tgs_runtime'):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',role):
        raise ValueError('Invalid runtime role')
    return f"""\\set ON_ERROR_STOP on
BEGIN;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='{role}' AND NOT rolsuper AND NOT rolbypassrls)
 OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner
 WHERE c.relnamespace='tgmate'::regnamespace AND r.rolname='{role}')
 THEN RAISE EXCEPTION 'Unsafe runtime role'; END IF;
END $$;
GRANT USAGE ON SCHEMA tgmate TO "{role}";
GRANT SELECT(id,tenant_id,status,generation), UPDATE(status)
 ON tgmate.relay_conversations TO "{role}";
GRANT SELECT(id,tenant_id,scope,idempotency_key,request_hash,response_status,response_body,expires_at),
 INSERT(tenant_id,scope,idempotency_key,request_hash,expires_at), UPDATE(response_status,response_body)
 ON tgmate.idempotency_records TO "{role}";
GRANT INSERT(tenant_id,actor_user_id,action,resource_type,resource_id,request_id,metadata)
 ON tgmate.audit_logs TO "{role}";
COMMIT;
"""

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role',default='tgs_runtime')
    parser.add_argument('--psql',default='psql')
    args=parser.parse_args(argv)
    try:
        result=subprocess.run([args.psql,'-X','-q','-v','ON_ERROR_STOP=1'],
            input=grant_sql(args.role),text=True,encoding='utf-8',capture_output=True)
        if result.returncode: raise RuntimeError()
    except (ValueError,OSError,RuntimeError):
        print('Relay control grants rejected.',file=sys.stderr)
        return 1
    print('Relay state control grants configured.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
