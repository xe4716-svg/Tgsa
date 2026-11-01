from datetime import datetime, timedelta, timezone

import pytest

from app.billing.commerce import calculate_remaining_credit, purchase_fingerprint


def test_remaining_credit_is_prorated_and_rounded_down():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=30)
    now = start + timedelta(days=15)
    terms = [{
        "currency": "USD",
        "net_paid_minor": 10001,
        "starts_at": start,
        "ends_at": end,
    }]
    assert calculate_remaining_credit(terms, "USD", now) == 5000


def test_future_renewal_term_keeps_full_unconsumed_value():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first_start = now - timedelta(days=15)
    first_end = now + timedelta(days=15)
    next_start = first_end
    next_end = next_start + timedelta(days=30)
    terms = [
        {
            "currency": "CNY",
            "net_paid_minor": 10000,
            "starts_at": first_start,
            "ends_at": first_end,
        },
        {
            "currency": "CNY",
            "net_paid_minor": 20000,
            "starts_at": next_start,
            "ends_at": next_end,
        },
    ]
    assert calculate_remaining_credit(terms, "CNY", now) == 25000


def test_expired_term_does_not_create_upgrade_credit():
    now = datetime(2026, 1, 31, tzinfo=timezone.utc)
    terms = [{
        "currency": "USD",
        "net_paid_minor": 10000,
        "starts_at": now - timedelta(days=30),
        "ends_at": now,
    }]
    assert calculate_remaining_credit(terms, "USD", now) == 0


def test_remaining_paid_value_cannot_be_converted_between_currencies():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    terms = [{
        "currency": "USD",
        "net_paid_minor": 10000,
        "starts_at": now - timedelta(days=1),
        "ends_at": now + timedelta(days=29),
    }]
    with pytest.raises(ValueError):
        calculate_remaining_credit(terms, "USDT", now)


def test_purchase_fingerprint_is_normalized_and_request_specific():
    first = purchase_fingerprint("pro", 30, "usd")
    assert first == purchase_fingerprint(" PRO ", 30, "USD")
    assert first != purchase_fingerprint("BASIC", 30, "USD")
    assert first != purchase_fingerprint("PRO", 90, "USD")
    assert first != purchase_fingerprint("PRO", 30, "USDT")
