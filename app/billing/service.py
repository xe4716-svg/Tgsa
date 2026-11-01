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
          AND (
              s.status IN ('ACTIVE', 'TRIALING')
              OR (s.status = 'PAST_DUE' AND s.grace_until > NOW())
          )
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
        SELECT feature_key, enabled, limit_value, config
        FROM plan_features
        WHERE plan_id = $1
        """,
        plan_id,
    )
    features = {
        item["feature_key"]: {
            "enabled": item["enabled"],
            "limit": item["limit_value"],
            "config": item["config"] or {},
        }
        for item in feature_rows
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
