import asyncio
from uuid import uuid4

import pytest

from app.billing.service import (
    FeatureAccessDenied,
    FeatureLimitReached,
    ensure_feature,
    ensure_limit,
    load_entitlements,
)


class FakeConnection:
    def __init__(self, subscription, features):
        self.subscription = subscription
        self.features = features

    async def fetchrow(self, query, *args):
        if "FROM tenant_subscriptions" in query:
            return self.subscription
        if "FROM membership_plans" in query:
            return self.subscription
        raise AssertionError("Unexpected fetchrow query")

    async def fetch(self, query, *args):
        return self.features


def test_entitlement_load_and_feature_gate():
    tenant_id = uuid4()
    conn = FakeConnection(
        {
            "subscription_status": "ACTIVE",
            "expires_at": None,
            "plan_code": "PRO",
            "plan_name": "专业版",
            "plan_id": uuid4(),
        },
        [
            {"feature_key": "channel_clone", "enabled": True, "limit_value": None, "config": {}},
            {"feature_key": "sticker_clone", "enabled": True, "limit_value": None, "config": {}},
        ],
    )
    entitlements = asyncio.run(load_entitlements(conn, tenant_id))
    assert entitlements.plan_code == "PRO"
    assert entitlements.features["channel_clone"]["enabled"] is True
    asyncio.run(ensure_feature(conn, tenant_id, "sticker_clone"))


def test_disabled_feature_is_denied():
    tenant_id = uuid4()
    conn = FakeConnection(
        {
            "subscription_status": "ACTIVE",
            "expires_at": None,
            "plan_code": "BASIC",
            "plan_name": "基础版",
            "plan_id": uuid4(),
        },
        [
            {"feature_key": "channel_clone", "enabled": True, "limit_value": None, "config": {}},
            {"feature_key": "sticker_clone", "enabled": False, "limit_value": None, "config": {}},
        ],
    )
    with pytest.raises(FeatureAccessDenied):
        asyncio.run(ensure_feature(conn, tenant_id, "sticker_clone"))


def test_account_limit_is_enforced():
    tenant_id = uuid4()
    conn = FakeConnection(
        {
            "subscription_status": "ACTIVE",
            "expires_at": None,
            "plan_code": "BASIC",
            "plan_name": "基础版",
            "plan_id": uuid4(),
        },
        [
            {"feature_key": "max_telegram_accounts", "enabled": True, "limit_value": 2, "config": {}},
        ],
    )
    with pytest.raises(FeatureLimitReached):
        asyncio.run(ensure_limit(conn, tenant_id, "max_telegram_accounts", 2))
