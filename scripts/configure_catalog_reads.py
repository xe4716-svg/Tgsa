"""Register separate read permissions and grant only inventory metadata columns."""
import argparse,re,subprocess,sys

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
INSERT INTO tgmate.permissions(code,description) VALUES
 ('bot_broadcast.read','Read bot broadcast inventory'),
 ('account_broadcast.read','Read account broadcast inventory'),
 ('moderation.read','Read moderation inventory') ON CONFLICT(code) DO NOTHING;
GRANT USAGE ON SCHEMA tgmate TO "{role}";
GRANT SELECT(id,tenant_id,status,task_type,business_module,robot_instance_id,scheduled_at,started_at,finished_at,created_at,updated_at)
 ON tgmate.task_registry TO "{role}";
GRANT SELECT(tenant_id,task_id,telegram_bot_id) ON tgmate.bot_broadcast_tasks TO "{role}";
GRANT SELECT(tenant_id,task_id,execution_mode,target_scope,failure_policy) ON tgmate.account_broadcast_tasks TO "{role}";
GRANT SELECT(id,tenant_id,destination_id,enabled,cleanup_mode,violation_threshold,threshold_action,verification_enabled,created_at,updated_at)
 ON tgmate.moderation_group_configs TO "{role}";
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
        if result.returncode:raise RuntimeError()
    except (ValueError,OSError,RuntimeError):
        print('Business inventory grants rejected.',file=sys.stderr)
        return 1
    print('Inventory columns and permission catalog configured; member roles unchanged.')
    return 0
if __name__=='__main__':
    raise SystemExit(main())
