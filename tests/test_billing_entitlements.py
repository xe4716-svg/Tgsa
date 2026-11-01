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


def test_tenant_override_can_enable_missing_plan_feature_and_increase_limit():
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
            {
                "feature_key": "sticker_clone",
                "enabled": False,
                "limit_value": None,
                "config": {},
                "enabled_override": True,
                "limit_delta": 0,
            },
            {
                "feature_key": "max_telegram_accounts",
                "enabled": True,
                "limit_value": 2,
                "config": {},
                "enabled_override": None,
                "limit_delta": 3,
            },
        ],
    )
    entitlements = asyncio.run(load_entitlements(conn, tenant_id))
    assert entitlements.features["sticker_clone"]["enabled"] is True
    assert entitlements.features["max_telegram_accounts"]["limit"] == 5
    asyncio.run(ensure_feature(conn, tenant_id, "sticker_clone"))
    asyncio.run(ensure_limit(conn, tenant_id, "max_telegram_accounts", 4))
    with pytest.raises(FeatureLimitReached):
        asyncio.run(ensure_limit(conn, tenant_id, "max_telegram_accounts", 5))


def test_tenant_override_can_disable_feature_and_lower_quota_to_zero():
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
            {
                "feature_key": "channel_clone",
                "enabled": True,
                "limit_value": None,
                "config": {},
                "enabled_override": False,
                "limit_delta": 0,
            },
            {
                "feature_key": "max_active_clone_tasks",
                "enabled": True,
                "limit_value": 10,
                "config": {},
                "enabled_override": None,
                "limit_delta": -100,
            },
        ],
    )
    entitlements = asyncio.run(load_entitlements(conn, tenant_id))
    assert entitlements.features["channel_clone"]["enabled"] is False
    assert entitlements.features["max_active_clone_tasks"]["limit"] == 0
    with pytest.raises(FeatureAccessDenied):
        asyncio.run(ensure_feature(conn, tenant_id, "channel_clone"))
    with pytest.raises(FeatureLimitReached):
        asyncio.run(ensure_limit(conn, tenant_id, "max_active_clone_tasks", 0))


def test_unlimited_plan_limit_stays_unlimited_with_override_delta():
    conn = FakeConnection(
        {
            "subscription_status": "ACTIVE",
            "expires_at": None,
            "plan_code": "ENTERPRISE",
            "plan_name": "企业版",
            "plan_id": uuid4(),
        },
        [{
            "feature_key": "max_telegram_accounts",
            "enabled": True,
            "limit_value": None,
            "config": {},
            "enabled_override": None,
            "limit_delta": -3,
        }],
    )
    entitlements = asyncio.run(load_entitlements(conn, uuid4()))
    assert entitlements.features["max_telegram_accounts"]["limit"] is None
