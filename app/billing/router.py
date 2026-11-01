from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.billing.service import load_entitlements


router = APIRouter(tags=["membership and billing"])

FEATURE_KEYS = {
    "group_search",
    "global_search",
    "bot_search",
    "link_import",
    "account_asset_scan",
    "channel_clone",
    "sticker_clone",
    "realtime_sync",
    "max_telegram_accounts",
    "max_active_clone_tasks",
}


class FeatureUpdateRequest(BaseModel):
    enabled: bool = True
    limit_value: int | None = Field(default=None, ge=0)
    config: dict = Field(default_factory=dict)


class AssignSubscriptionRequest(BaseModel):
    plan_code: str = Field(min_length=2, max_length=40)
    duration_days: int | None = Field(default=30, ge=1, le=3650)


class TenantFeatureOverrideRequest(BaseModel):
    enabled_override: bool | None = None
    limit_delta: int = Field(default=0, ge=-1000000, le=1000000)
    reason: str = Field(min_length=3, max_length=500)


def _require_platform_admin(principal: AuthPrincipal) -> None:
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator permission required")


@router.get("/billing/plans")
async def list_public_plans(request: Request):
    async with request.app.state.db_pool.acquire() as conn:
        plans = await conn.fetch(
            """
            SELECT id, code, name, level, sort_order
            FROM membership_plans
            WHERE is_active = TRUE
            ORDER BY sort_order, level
            """
        )
        result = []
        for plan in plans:
            features = await conn.fetch(
                """
                SELECT feature_key, enabled, limit_value, config
                FROM plan_features
                WHERE plan_id = $1
                ORDER BY feature_key
                """,
                plan["id"],
            )
            result.append({
                "code": plan["code"],
                "name": plan["name"],
                "level": plan["level"],
                "features": {
                    f["feature_key"]: {
                        "enabled": f["enabled"],
                        "limit": f["limit_value"],
                        "config": f["config"] or {},
                    }
                    for f in features
                },
            })
    return {"plans": result}


@router.get("/billing/entitlements")
async def current_entitlements(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            entitlements = await load_entitlements(conn, principal.tenant_id)
    return {
        "plan": {
            "code": entitlements.plan_code,
            "name": entitlements.plan_name,
        },
        "subscription_status": entitlements.subscription_status,
        "expires_at": entitlements.expires_at,
        "features": entitlements.features,
    }


@router.get("/admin/plans")
async def admin_list_plans(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_platform_admin(principal)
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.code, p.name, p.level, p.is_active, p.sort_order,
                   f.feature_key, f.enabled, f.limit_value, f.config
            FROM membership_plans p
            LEFT JOIN plan_features f ON f.plan_id = p.id
            ORDER BY p.sort_order, p.level, f.feature_key
            """
        )
    plans: dict[str, dict] = {}
    for row in rows:
        plan = plans.setdefault(row["code"], {
            "code": row["code"],
            "name": row["name"],
            "level": row["level"],
            "is_active": row["is_active"],
            "sort_order": row["sort_order"],
            "features": {},
        })
        if row["feature_key"]:
            plan["features"][row["feature_key"]] = {
                "enabled": row["enabled"],
                "limit": row["limit_value"],
                "config": row["config"] or {},
            }
    return {"plans": list(plans.values())}


@router.put("/admin/plans/{plan_code}/features/{feature_key}")
async def update_plan_feature(
    plan_code: str,
    feature_key: str,
    payload: FeatureUpdateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_platform_admin(principal)
    if feature_key not in FEATURE_KEYS:
        raise HTTPException(status_code=422, detail="Unknown feature key")

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            plan = await conn.fetchrow(
                "SELECT id FROM membership_plans WHERE code = $1",
                plan_code.upper(),
            )
            if not plan:
                raise HTTPException(status_code=404, detail="Membership plan not found")
            await conn.execute(
                """
                INSERT INTO plan_features (
                    plan_id, feature_key, enabled, limit_value, config
                )
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (plan_id, feature_key)
                DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    limit_value = EXCLUDED.limit_value,
                    config = EXCLUDED.config,
                    updated_at = NOW()
                """,
                plan["id"],
                feature_key,
                payload.enabled,
                payload.limit_value,
                json.dumps(payload.config),
            )
    return {
        "status": "updated",
        "plan_code": plan_code.upper(),
        "feature": feature_key,
        "enabled": payload.enabled,
        "limit": payload.limit_value,
    }


@router.post("/admin/tenants/{tenant_id}/subscription")
async def assign_subscription(
    tenant_id: UUID,
    payload: AssignSubscriptionRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_platform_admin(principal)
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            plan = await conn.fetchrow(
                """
                SELECT id, code, name FROM membership_plans
                WHERE code = $1 AND is_active = TRUE
                """,
                payload.plan_code.upper(),
            )
            if not plan:
                raise HTTPException(status_code=404, detail="Active membership plan not found")

            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            tenant = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE",
                tenant_id,
            )
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")

            db_now = await conn.fetchval("SELECT NOW()")
            current = await conn.fetchrow(
                """
                SELECT s.id, s.plan_id, s.starts_at, s.expires_at,
                       s.status, p.code AS current_plan_code
                FROM tenant_subscriptions s
                JOIN membership_plans p ON p.id = s.plan_id
                WHERE s.tenant_id = $1
                  AND s.status IN ('ACTIVE', 'TRIALING', 'PAST_DUE')
                ORDER BY s.starts_at DESC
                LIMIT 1
                FOR UPDATE OF s
                """,
                tenant_id,
            )

            if current and current["plan_id"] == plan["id"]:
                # Same-tier manual renewal extends from the existing expiry.
                # An indefinite subscription must never be shortened by a 30-day default.
                if current["expires_at"] is None:
                    starts_at = current["starts_at"]
                    expires_at = None
                elif current["expires_at"] > db_now:
                    starts_at = current["starts_at"]
                    expires_at = (
                        current["expires_at"] + timedelta(days=payload.duration_days)
                        if payload.duration_days is not None
                        else None
                    )
                else:
                    starts_at = db_now
                    expires_at = (
                        db_now + timedelta(days=payload.duration_days)
                        if payload.duration_days is not None
                        else None
                    )
                await conn.execute(
                    """
                    UPDATE tenant_subscriptions
                    SET status = 'ACTIVE', starts_at = $2, expires_at = $3,
                        billing_source = 'MANUAL', updated_at = NOW()
                    WHERE id = $1 AND tenant_id = $4
                    """,
                    current["id"],
                    starts_at,
                    expires_at,
                    tenant_id,
                )
            else:
                if current:
                    # A manual plan replacement consumes the old paid term;
                    # it must not later be counted again as upgrade credit.
                    await conn.execute(
                        """
                        UPDATE subscription_terms
                        SET status = 'SUPERSEDED'
                        WHERE tenant_id = $1
                          AND status = 'ACTIVE'
                          AND ends_at > $2
                        """,
                        tenant_id,
                        db_now,
                    )
                    await conn.execute(
                        """
                        UPDATE tenant_subscriptions
                        SET status = 'SUPERSEDED', updated_at = NOW()
                        WHERE id = $1 AND tenant_id = $2
                        """,
                        current["id"],
                        tenant_id,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE tenant_subscriptions
                        SET status = 'EXPIRED', updated_at = NOW()
                        WHERE tenant_id = $1
                          AND status IN ('ACTIVE', 'TRIALING', 'PAST_DUE')
                          AND expires_at IS NOT NULL
                          AND expires_at <= $2
                        """,
                        tenant_id,
                        db_now,
                    )
                starts_at = db_now
                expires_at = (
                    db_now + timedelta(days=payload.duration_days)
                    if payload.duration_days is not None
                    else None
                )
                await conn.execute(
                    """
                    INSERT INTO tenant_subscriptions (
                        tenant_id, plan_id, status, starts_at, expires_at,
                        billing_source
                    )
                    VALUES ($1, $2, 'ACTIVE', $3, $4, 'MANUAL')
                    """,
                    tenant_id,
                    plan["id"],
                    starts_at,
                    expires_at,
                )

            await conn.execute(
                """
                INSERT INTO audit_logs (
                    tenant_id, actor_user_id, action, resource_type,
                    resource_id, details
                )
                VALUES ($1, $2, 'SUBSCRIPTION_ASSIGNED', 'tenant_subscription',
                        NULL, $3::jsonb)
                """,
                tenant_id,
                principal.user_id,
                json.dumps({
                    "plan_code": plan["code"],
                    "duration_days": payload.duration_days,
                    "previous_plan_code": current["current_plan_code"] if current else None,
                    "previous_expires_at": (
                        current["expires_at"].isoformat()
                        if current and current["expires_at"] else None
                    ),
                    "effective_start_at": starts_at.isoformat(),
                    "effective_end_at": expires_at.isoformat() if expires_at else None,
                }),
            )
    return {
        "status": "assigned",
        "tenant_id": str(tenant_id),
        "plan_code": plan["code"],
        "plan_name": plan["name"],
        "starts_at": starts_at,
        "expires_at": expires_at,
    }


@router.put("/admin/tenants/{tenant_id}/feature-overrides/{feature_key}")
async def put_tenant_feature_override(
    tenant_id: UUID,
    feature_key: str,
    payload: TenantFeatureOverrideRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Set a tenant-specific feature or quota exception with an audit trail."""
    _require_platform_admin(principal)
    if feature_key not in FEATURE_KEYS:
        raise HTTPException(status_code=422, detail="Unknown feature key")
    if payload.limit_delta and not feature_key.startswith("max_"):
        raise HTTPException(status_code=422, detail="Quota delta only applies to limit features")
    if payload.enabled_override is None and payload.limit_delta == 0:
        raise HTTPException(status_code=422, detail="Provide an override or nonzero limit delta")

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            tenant = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE", tenant_id
            )
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            previous = await conn.fetchrow(
                """
                SELECT enabled_override, limit_delta, reason
                FROM tenant_feature_overrides
                WHERE tenant_id = $1 AND feature_key = $2
                """, tenant_id, feature_key
            )
            await conn.execute(
                """
                INSERT INTO tenant_feature_overrides
                    (tenant_id, feature_key, enabled_override, limit_delta,
                     reason, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id, feature_key) DO UPDATE
                SET enabled_override = EXCLUDED.enabled_override,
                    limit_delta = EXCLUDED.limit_delta,
                    reason = EXCLUDED.reason,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                tenant_id, feature_key, payload.enabled_override,
                payload.limit_delta, payload.reason, principal.user_id,
            )
            await conn.execute(
                """
                INSERT INTO audit_logs
                    (tenant_id, actor_user_id, action, resource_type, details)
                VALUES ($1, $2, 'TENANT_FEATURE_OVERRIDE_SET',
                        'tenant_feature_override', $3::jsonb)
                """,
                tenant_id, principal.user_id,
                json.dumps({
                    "feature_key": feature_key,
                    "before": dict(previous) if previous else None,
                    "after": {
                        "enabled_override": payload.enabled_override,
                        "limit_delta": payload.limit_delta,
                        "reason": payload.reason,
                    },
                }),
            )
    return {"status": "updated", "feature_key": feature_key,
            "enabled_override": payload.enabled_override,
            "limit_delta": payload.limit_delta}


@router.delete("/admin/tenants/{tenant_id}/feature-overrides/{feature_key}")
async def delete_tenant_feature_override(
    tenant_id: UUID,
    feature_key: str,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Remove a tenant exception, reverting to the plan default."""
    _require_platform_admin(principal)
    if feature_key not in FEATURE_KEYS:
        raise HTTPException(status_code=422, detail="Unknown feature key")
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            tenant = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE", tenant_id
            )
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            previous = await conn.fetchrow(
                """
                DELETE FROM tenant_feature_overrides
                WHERE tenant_id = $1 AND feature_key = $2
                RETURNING enabled_override, limit_delta, reason
                """, tenant_id, feature_key
            )
            if previous:
                await conn.execute(
                    """
                    INSERT INTO audit_logs
                        (tenant_id, actor_user_id, action, resource_type, details)
                    VALUES ($1, $2, 'TENANT_FEATURE_OVERRIDE_CLEARED',
                            'tenant_feature_override', $3::jsonb)
                    """,
                    tenant_id, principal.user_id,
                    json.dumps({"feature_key": feature_key,
                                "before": dict(previous)}),
                )
    return {"status": "cleared", "feature_key": feature_key}
