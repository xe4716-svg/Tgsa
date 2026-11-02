"""Reviewed local deployment grants shared by bootstrap and clean rebuild verification."""
import re
from psycopg import sql
AUTH_FUNCTIONS=['auth_lookup(text,text)','auth_create_session(uuid,uuid,text,text,timestamptz,bigint)','auth_context(text)','auth_revoke_session(text)','auth_take_attempt(text,text)','auth_mark_success(uuid)','auth_password_record(text)','auth_change_password(text,bigint,text)']


def grant_runtime(db,root):
 flags=db.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='tgmate_app'").fetchone()
 if flags!=(False,False): raise RuntimeError('Runtime role must not bypass row isolation')
 db.execute('GRANT USAGE ON SCHEMA tgmate TO tgmate_app')
 mutable=['relay_configs','relay_conversations','relay_operations','relay_payloads','relay_runtime_links','inbox_events','outbox_events','task_registry','task_leases','audit_logs','quota_accounts','quota_reservations','quota_ledger','task_deliveries','delivery_attempts','broadcast_task_runs','broadcast_run_targets','bot_broadcast_run_executors','account_broadcast_run_accounts','bot_broadcast_run_target_checks','account_broadcast_run_target_checks','bot_broadcast_deliveries','account_broadcast_deliveries','bot_broadcast_delivery_attempts','account_broadcast_delivery_attempts']
 mutable += [t for t in re.findall(r'CREATE TABLE tgmate\.(\w+)',(root/'source_001_003/003_group_moderation.sql').read_text(encoding='utf-8-sig')) if t!='moderation_platform_defaults']
 for table in mutable: db.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON tgmate.{} TO tgmate_app').format(sql.Identifier(table)))
 db.execute('GRANT SELECT,INSERT,UPDATE ON tgmate.telegram_bots,tgmate.bot_credentials TO tgmate_app')
 db.execute('GRANT SELECT,INSERT ON tgmate.bot_ownership_claims TO tgmate_app')
 db.execute('GRANT SELECT,INSERT,UPDATE ON tgmate.bot_webhook_registrations TO tgmate_app')
 db.execute('GRANT SELECT,INSERT,UPDATE ON tgmate.relay_operator_authorizations TO tgmate_app')
 db.execute('GRANT SELECT,INSERT,UPDATE ON tgmate.relay_privacy_notices TO tgmate_app')
 db.execute('GRANT DELETE ON tgmate.task_leases TO tgmate_app')
 for table in ['message_templates','message_template_versions','broadcast_task_versions','broadcast_task_version_targets','bot_broadcast_tasks','account_broadcast_tasks','account_broadcast_task_accounts','idempotency_records']:
  db.execute(sql.SQL('GRANT SELECT,INSERT ON tgmate.{} TO tgmate_app').format(sql.Identifier(table)))
 db.execute('GRANT SELECT ON tgmate.telegram_destinations,tgmate.telegram_sessions,tgmate.telegram_identities,tgmate.tenant_members,tgmate.robot_instances,tgmate.executor_bindings,tgmate.telegram_bots TO tgmate_app')
 db.execute('GRANT SELECT,INSERT,UPDATE ON tgmate.bot_destination_access TO tgmate_app')


def grant_auth(db):
 flags=db.execute("SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname='tgmate_auth_owner'").fetchone()
 if flags is None: db.execute('CREATE ROLE tgmate_auth_owner NOLOGIN NOSUPERUSER NOBYPASSRLS')
 elif flags!=(False,False,False): raise RuntimeError('Unexpected authentication owner properties')
 login=db.execute("SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname='tgmate_auth'").fetchone()
 if login!=(True,False,False): raise RuntimeError('Unexpected authentication login role properties')
 db.execute('GRANT USAGE,CREATE ON SCHEMA tgmate TO tgmate_auth_owner')
 db.execute('GRANT SELECT ON tgmate.users,tgmate.tenants,tgmate.tenant_members,tgmate.roles,tgmate.role_permissions,tgmate.tenant_member_roles TO tgmate_auth_owner')
 db.execute('GRANT INSERT ON tgmate.audit_logs TO tgmate_auth_owner')
 db.execute('GRANT SELECT,INSERT,UPDATE ON tgmate.web_credentials,tgmate.web_sessions,tgmate.web_login_attempts TO tgmate_auth_owner')
 db.execute('GRANT USAGE ON SCHEMA tgmate TO tgmate_auth')
 for signature in AUTH_FUNCTIONS:
  db.execute(f'ALTER FUNCTION tgmate.{signature} OWNER TO tgmate_auth_owner')
  db.execute(f'GRANT EXECUTE ON FUNCTION tgmate.{signature} TO tgmate_auth')
 db.execute('REVOKE CREATE ON SCHEMA tgmate FROM tgmate_auth_owner')
