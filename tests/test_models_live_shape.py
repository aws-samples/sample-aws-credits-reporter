"""Parser tests against the payload shape observed from the real GetCredits API.

``get_credits_live_shape.json`` mirrors a real response (placeholder IDs):
six-decimal ``currencyAmount`` strings, full ISO 8601 timestamps with a UTC
offset, all 16 verified fields present, and no pagination token. boto3
deserializes those timestamps into ``tzlocal()`` datetimes, so the tests also
cover the datetime path with a non-UTC offset.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from aws_credits_reporter.models import CREDIT_FIELDS, parse_credit, parse_credits

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_live_shape_parses_with_no_missing_fields():
    page = _load("get_credits_live_shape.json")
    credits = parse_credits([page])
    assert len(credits) == 2
    # The live-shape credits are unbound, so only the optional Phase 6
    # cost-category binding fields (costCategoryArn, ruleName) are absent; they
    # appear only on credits bound to a cost-category sharing rule.
    phase6_binding = {"costCategoryArn", "ruleName"}
    for credit in credits:
        expected_missing = [n for n in CREDIT_FIELDS if n in phase6_binding]
        assert credit.missing_fields == expected_missing, credit.creditId
        for name in CREDIT_FIELDS:
            if name in phase6_binding:
                assert getattr(credit, name) is None, name
            else:
                assert getattr(credit, name) is not None, name

    first = credits[0]
    assert first.creditId == "1900000008X"
    assert first.initialAmount.amount == Decimal("35.000000")
    assert first.initialAmount.currency == "USD"
    assert first.remainingAmount.amount == Decimal("35.000000")
    assert first.startDate == date(2026, 3, 11)
    assert first.endDate == date(2026, 6, 11)
    assert first.shareableAccounts == []  # present-but-empty is not missing
    assert first.accountHasCreditSharingEnabled is True
    assert credits[1].remainingAmount.amount == Decimal("0.750000")


def test_tz_aware_datetime_uses_utc_calendar_date():
    """boto3 hands back tzlocal() datetimes; the date must be the UTC one."""
    plus_two = timezone(timedelta(hours=2))
    # 2026-06-12 01:30 at UTC+2 is 2026-06-11 23:30 UTC.
    raw = {"creditId": "x", "endDate": datetime(2026, 6, 12, 1, 30, tzinfo=plus_two)}
    assert parse_credit(raw).endDate == date(2026, 6, 11)

    minus_five = timezone(timedelta(hours=-5))
    # 2026-06-11 21:00 at UTC-5 is 2026-06-12 02:00 UTC.
    raw = {"creditId": "x", "endDate": datetime(2026, 6, 11, 21, 0, tzinfo=minus_five)}
    assert parse_credit(raw).endDate == date(2026, 6, 12)


def test_iso_string_with_offset_uses_utc_calendar_date():
    assert parse_credit({"startDate": "2026-06-12T01:30:00+02:00"}).startDate == date(2026, 6, 11)
    assert parse_credit({"startDate": "2026-03-11T17:00:00+00:00"}).startDate == date(2026, 3, 11)
    assert parse_credit({"startDate": "2026-03-11T23:59:59Z"}).startDate == date(2026, 3, 11)


def test_naive_datetime_is_taken_as_is():
    raw = {"endDate": datetime(2026, 6, 12, 1, 30)}
    assert parse_credit(raw).endDate == date(2026, 6, 12)
