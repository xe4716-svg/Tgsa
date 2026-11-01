from __future__ import annotations

from uuid import UUID

import asyncpg

from app.security.session_vault import SessionVault
from app.telegram.client_factory import validate_connection_choice
from app.billing.service import FeatureLimitReached, ensure_limit


async def persist_authorized_account(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    user_id: UUID,
    account: dict,
    session_string: str,
    vault: SessionVault,
    connection_mode: str,
    proxy_profile: dict | None,
    proxy_profile_id: UUID | None,
    notice_version: str,
) -> UUID:
    """Atomically create account, encrypted session and consent record.

    The caller must already have authenticated user_id. Membership is checked
    again inside the same transaction. Raw session material is encrypted before
    it reaches PostgreSQL and is never included in audit data.
    """
    validate_connection_choice(connection_mode, proxy_profile)

    telegram_user_id = account.get("telegram_user_id")
    if not isinstance(telegram_user_id, int):
        raise ValueError("Telegram did not return a valid user ID")
    if not notice_version or len(notice_version) > 30:
        raise ValueError("Connection notice version is required")

    mode = connection_mode.upper()
    if mode == "PROXY" and proxy_profile_id is None:
        raise ValueError("Proxy profile ID is required in proxy mode")
    if mode == "DIRECT" and proxy_profile_id is not None:
        raise ValueError("Direct mode cannot be bound to a proxy")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            tenant_lock = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE",
                tenant_id,
            )
            if not tenant_lock:
                raise PermissionError("Tenant not found")

            membership = await conn.fetchrow(
                """
                SELECT role
                FROM tenant_memberships
                WHERE tenant_id = $1 AND user_id = $2
                """,
                tenant_id,
                user_id,
            )
            if not membership:
                raise PermissionError("User is not a member of this tenant")

            current_account_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM telegram_accounts
                WHERE tenant_id = $1 AND status <> 'REVOKED'
                """,
                tenant_id,
            )
            try:
                await ensure_limit(
                    conn,
                    tenant_id,
                    "max_telegram_accounts",
                    current_account_count,
                )
            except FeatureLimitReached as exc:
                raise ValueError(
                    f"Telegram account limit reached for current plan: {exc.limit_value}"
                ) from exc

            if mode == "PROXY":
                proxy_row = await conn.fetchrow(
                    """
                    SELECT id
                    FROM proxy_profiles
                    WHERE tenant_id = $1
                      AND id = $2
                      AND status = 'ACTIVE'
                      AND verification_status = 'VERIFIED'
                    """,
                    tenant_id,
                    proxy_profile_id,
                )
                if not proxy_row:
                    raise ValueError("Selected proxy is not verified and active")

            account_id = await conn.fetchval(
                """
                INSERT INTO telegram_accounts (
                    tenant_id, telegram_user_id, phone_masked, username,
                    display_name, status, security_state, connection_mode
                )
                VALUES ($1, $2, $3, $4, $5, 'AUTHORIZED', 'NEW_DEVICE', $6)
                RETURNING id
                """,
                tenant_id,
                telegram_user_id,
                account.get("phone_masked"),
                account.get("username"),
                " ".join(
                    part for part in [
                        account.get("first_name"),
                        account.get("last_name"),
                    ] if part
                )[:160] or None,
                mode,
            )

            encrypted = vault.encrypt(tenant_id, account_id, session_string)
            await conn.execute(
                """
                INSERT INTO telegram_sessions (
                    account_id, tenant_id, ciphertext, nonce,
                    wrapped_dek, kms_key_id, encryption_version
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                account_id,
                tenant_id,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.wrapped_dek,
                encrypted.kms_key_id,
                encrypted.encryption_version,
            )

            await conn.execute(
                """
                INSERT INTO connection_consents (
                    tenant_id, user_id, connection_mode,
                    proxy_profile_id, notice_version
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                tenant_id,
                user_id,
                mode,
                proxy_profile_id,
                notice_version,
            )

            await conn.execute(
                """
                INSERT INTO audit_logs (
                    tenant_id, actor_user_id, action, resource_type,
                    resource_id, details
                )
                VALUES ($1, $2, 'TELEGRAM_ACCOUNT_CONNECTED',
                        'telegram_account', $3, $4::jsonb)
                """,
                tenant_id,
                user_id,
                account_id,
                '{"connection_stage":"authorized","security_state":"NEW_DEVICE"}',
            )

            return account_id
