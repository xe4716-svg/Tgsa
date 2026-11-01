from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.billing.automation import (
    _format_amount,
    _validate_timezone,
    reminder_bucket,
)


def test_reminder_bucket_uses_one_day_windows():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert reminder_bucket(now + timedelta(days=6, hours=12), now, [7, 3, 1, 0]) == 7
    assert reminder_bucket(now + timedelta(days=2, hours=12), now, [7, 3, 1, 0]) == 3
    assert reminder_bucket(now + timedelta(hours=12), now, [7, 3, 1, 0]) == 1
    assert reminder_bucket(now - timedelta(hours=2), now, [7, 3, 1, 0]) == 0


def test_reminder_bucket_does_not_repeat_outside_configured_windows():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert reminder_bucket(now + timedelta(days=5), now, [7, 3, 1, 0]) is None
    assert reminder_bucket(now - timedelta(days=2), now, [7, 3, 1, 0]) is None


def test_timezone_must_be_a_valid_iana_zone():
    assert _validate_timezone("America/Los_Angeles") == "America/Los_Angeles"
    with pytest.raises(HTTPException) as error:
        _validate_timezone("Mars/Olympus_Mons")
    assert error.value.status_code == 422


def test_currency_amounts_use_integer_minor_units():
    assert _format_amount("CNY", 12345) == "CNY 123.45"
    assert _format_amount("USD", 999) == "USD 9.99"
    assert _format_amount("USDT", 1234567) == "USDT 1.234567"
