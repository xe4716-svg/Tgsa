"""Explicit offline identity linking. Supply migration-owner credentials through PG*."""
import argparse
import subprocess
import sys
from uuid import UUID


def plan(tenant, user, business_tenant, business_user, apply=False):
    t, u, bt, bu = (str(UUID(str(value))) for value in (tenant, user, business_tenant, business_user))
    checks = f"""
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.tenant_memberships m
 JOIN public.users u ON u.id=m.user_id JOIN public.tenants t ON t.id=m.tenant_id
 WHERE m.tenant_id='{t}' AND m.user_id='{u}' AND u.status='ACTIVE' AND t.status='ACTIVE')
 THEN RAISE EXCEPTION 'Root membership unavailable'; END IF;
 IF NOT EXISTS(SELECT 1 FROM tgmate.tenant_members m
 JOIN tgmate.users u ON u.id=m.user_id JOIN tgmate.tenants t ON t.id=m.tenant_id
 WHERE m.tenant_id='{bt}' AND m.user_id='{bu}' AND m.status='active' AND u.status='active' AND t.status='active')
 THEN RAISE EXCEPTION 'Business membership unavailable'; END IF;
 IF EXISTS(SELECT 1 FROM public.integration_tenant_map WHERE
 (tenant_id='{t}' OR business_tenant_id='{bt}') AND
 (tenant_id<>'{t}' OR business_tenant_id<>'{bt}'))
 OR EXISTS(SELECT 1 FROM public.integration_user_map WHERE
 (user_id='{u}' OR business_user_id='{bu}') AND
 (user_id<>'{u}' OR business_user_id<>'{bu}'))
 OR EXISTS(SELECT 1 FROM public.integration_member_map WHERE tenant_id='{t}' AND user_id='{u}' AND business_tenant_id<>'{bt}')
 THEN RAISE EXCEPTION 'Identity mapping conflict'; END IF;
END $$;
"""
    writes = f"""
INSERT INTO public.integration_tenant_map VALUES ('{t}','{bt}') ON CONFLICT DO NOTHING;
INSERT INTO public.integration_user_map VALUES ('{u}','{bu}') ON CONFLICT DO NOTHING;
INSERT INTO public.integration_member_map VALUES ('{t}','{u}','{bt}') ON CONFLICT DO NOTHING;
""" if apply else ""
    return "\\set ON_ERROR_STOP on\nBEGIN;\nLOCK public.integration_tenant_map,public.integration_user_map,public.integration_member_map IN SHARE ROW EXCLUSIVE MODE;\n" + checks + writes + "COMMIT;\n"


def runtime_plan():
    return r"""\set ON_ERROR_STOP on
BEGIN;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='tgs_runtime' AND NOT rolsuper AND NOT rolbypassrls)
 OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner
 WHERE c.relnamespace IN ('public'::regnamespace,'tgmate'::regnamespace) AND r.rolname='tgs_runtime')
 THEN RAISE EXCEPTION 'Unsafe runtime role'; END IF;
END $$;
GRANT USAGE ON SCHEMA tgmate TO tgs_runtime;
GRANT SELECT(id,status) ON tgmate.users,tgmate.tenants TO tgs_runtime;
GRANT SELECT(tenant_id,user_id,status) ON tgmate.tenant_members TO tgs_runtime;
GRANT SELECT(tenant_id,user_id,role_id) ON tgmate.tenant_member_roles TO tgs_runtime;
GRANT SELECT(tenant_id,role_id,permission_code) ON tgmate.role_permissions TO tgs_runtime;
COMMIT;
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant')
    parser.add_argument('--user')
    parser.add_argument('--business-tenant')
    parser.add_argument('--business-user')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--configure-runtime-read', action='store_true')
    parser.add_argument('--psql', default='psql')
    args = parser.parse_args(argv)
    try:
        if args.configure_runtime_read:
            if args.apply or any((args.tenant,args.user,args.business_tenant,args.business_user)):
                raise ValueError('Runtime configuration is a separate operation')
            script = runtime_plan()
        else:
            script = plan(args.tenant,args.user,args.business_tenant,args.business_user,args.apply)
        result = subprocess.run([args.psql,'-X','-q','-v','ON_ERROR_STOP=1'],
            input=script, text=True, encoding='utf-8', capture_output=True)
        if result.returncode:
            raise RuntimeError('Identity configuration rejected')
    except (ValueError,TypeError,OSError,RuntimeError):
        print('Identity configuration failed; no mapping changes committed.',file=sys.stderr)
        return 1
    print('Runtime read grants configured.' if args.configure_runtime_read else
          'Identity mapping applied.' if args.apply else 'Identity mapping checks passed; no mapping written.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
