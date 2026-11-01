from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    import asyncpg


class FeatureAccessDenied(Exception):
    def __init__(self, feature_key: str):
        self.feature_key = feature_key
        super().__init__(f"Feature is not included in the current plan: {feature_key}")


class FeatureLimitReached(Exception):
    def __init__(self, feature_key: str, limit_value: int):
        self.feature_key = feature_key
        self.limit_value = limit_value
        super().__init__(f"Plan limit reached for {feature_key}: {limit_value}")


@dataclass(frozen=True)
class Entitlements:
    plan_code: str
    plan_name: str
    subscription_status: str
    expires_at: Any
    features: dict[str, dict[str, Any]]


async def load_entitlements(
    conn: asyncpg.Connection,
    tenant_id: UUID,
) -> Entitlements:
    """Read the effective plan for a tenant; expired subscriptions fall back to FREE."""
    row = await conn.fetchrow(
        """
        SELECT s.status AS subscription_status, s.expires_at,
               p.code AS plan_code, p.name AS plan_name, p.id AS plan_id
        FROM tenant_subscriptions s
        JOIN membership_plans p ON p.id = s.plan_id
        WHERE s.tenant_id = $1
          AND s.starts_at <= NOW()
          AND s.status IN ('ACTIVE', 'TRIALING')
          AND (s.expires_at IS NULL OR s.expires_at > NOW())
        ORDER BY s.starts_at DESC
        LIMIT 1
        """,
        tenant_id,
    )

    if row:
        plan_id = row["plan_id"]
        status = row["subscription_status"]
        expires_at = row["expires_at"]
        plan_code = row["plan_code"]
        plan_name = row["plan_name"]
    else:
        free_plan = await conn.fetchrow(
            "SELECT id, code, name FROM membership_plans WHERE code = 'FREE' AND is_active = TRUE"
        )
        if not free_plan:
            # Fail closed rather than accidentally granting premium features.
            return Entitlements("NONE", "No active plan", "EXPIRED", None, {})
        plan_id = free_plan["id"]
        plan_code = free_plan["code"]
        plan_name = free_plan["name"]
        status = "FALLBACK_FREE"
        expires_at = None

    feature_rows = await conn.fetch(
        """
        SELECT f.feature_key, f.enabled, f.limit_value, f.config,
               o.enabled_override, o.limit_delta
        FROM plan_features f
        LEFT JOIN tenant_feature_overrides o
          ON o.tenant_id = $2 AND o.feature_key = f.feature_key
        WHERE f.plan_id = $1
        """,
        plan_id,
        tenant_id,
    )
    features: dict[str, dict[str, Any]] = {}
    for item in feature_rows:
        # A missing override inherits the plan. NULL plan limit means unlimited.
        enabled_override = item.get("enabled_override")
        base_limit = item["limit_value"]
        limit_delta = item.get("limit_delta") or 0
        effective_limit = (
            None if base_limit is None else max(0, base_limit + limit_delta)
        )
        features[item["feature_key"]] = {
            "enabled": item["enabled"] if enabled_override is None else enabled_override,
            "limit": effective_limit,
            "config": item["config"] or {},
        }
    return Entitlements(
        plan_code=plan_code,
        plan_name=plan_name,
        subscription_status=status,
        expires_at=expires_at,
        features=features,
    )


async def ensure_feature(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    feature_key: str,
) -> Entitlements:
    entitlements = await load_entitlements(conn, tenant_id)
    feature = entitlements.features.get(feature_key)
    if not feature or not feature["enabled"]:
        raise FeatureAccessDenied(feature_key)
    return entitlements


async def ensure_limit(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    feature_key: str,
    current_usage: int,
) -> Entitlements:
    entitlements = await ensure_feature(conn, tenant_id, feature_key)
    limit_value = entitlements.features[feature_key]["limit"]
    if limit_value is not None and current_usage >= limit_value:
        raise FeatureLimitReached(feature_key, limit_value)
    return entitlements
