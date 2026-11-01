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

            await conn.execute(
                """
                UPDATE tenant_subscriptions
                SET status = 'CANCELED', updated_at = NOW()
                WHERE tenant_id = $1
                  AND status IN ('ACTIVE', 'TRIALING', 'PAST_DUE')
                """,
                tenant_id,
            )
            starts_at = datetime.now(timezone.utc)
            expires_at = (
                starts_at + timedelta(days=payload.duration_days)
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
                }),
            )
    return {
        "status": "assigned",
        "tenant_id": str(tenant_id),
        "plan_code": plan["code"],
        "plan_name": plan["name"],
        "expires_at": expires_at,
    }
