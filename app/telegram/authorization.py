"""Lock current authority through a Telegram account write transaction."""


async def lock_login_authority(conn, tenant_id, user_id, family_id):
    # Call after other potentially blocking resource locks. Shared row locks
    # linearize disable/remove/revoke operations against the final write.
    tenant = await conn.fetchrow('SELECT status FROM tenants WHERE id=$1 FOR SHARE', tenant_id)
    user = await conn.fetchrow('SELECT status FROM users WHERE id=$1 FOR SHARE', user_id)
    member = await conn.fetchrow(
        'SELECT role FROM tenant_memberships WHERE tenant_id=$1 AND user_id=$2 FOR SHARE', tenant_id,user_id)
    sessions = await conn.fetch(
        '''SELECT token_hash FROM auth_refresh_tokens
           WHERE tenant_id=$1 AND user_id=$2 AND family_id=$3
             AND revoked_at IS NULL AND expires_at>clock_timestamp()
           ORDER BY token_hash FOR SHARE''',tenant_id,user_id,family_id)
    if (not tenant or tenant['status']!='ACTIVE' or not user or user['status']!='ACTIVE'
            or not member or member['role'] not in ('OWNER','ADMIN','MEMBER') or not sessions):
        raise PermissionError('Current session cannot write Telegram accounts')
