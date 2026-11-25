"""Unit tests for the ``get-credits`` parser (task 1.3).

These tests exercise ``parse_credit`` / ``parse_credits`` against the recorded
sample payload fixtures in ``tests/fixtures/``. They pin the two behaviors
called out by the task:

* a fully populated record parses every field and records nothing as missing;
* a record that omits fields lists exactly those omitted fields in
  ``missing_fields`` (Requirements 1.2, 1.6).

The ``get_credits_live_shape.json`` fixture (real API shape, ISO 8601
timestamps) is covered separately in ``test_models_live_shape.py``.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from aws_credits_reporter.models import (
    CREDIT_FIELDS,
    Credit,
    CurrencyAmount,
    parse_credit,
    parse_credits,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------------------
# Full record: every field parses, nothing recorded as missing
# --------------------------------------------------------------------------


def test_full_record_parses_every_field():
    """A fully populated credit records no missing fields and parses each
    field into its typed value (Req 1.2)."""
    page = _load("get_credits_page1.json")
    credit = parse_credit(page["credits"][0])

    # Every field the record carried is populated; none recorded missing.
    # (This record omits only ``exhaustDate``, so assert on the concrete
    # values rather than "no field is missing".)
    assert credit.creditId == "credit-0001"
    assert credit.accountId == "111111111111"
    assert credit.creditType == "PROMOTIONAL"
    assert credit.initialAmount == CurrencyAmount(Decimal("10000.00"), "USD")
    assert credit.remainingAmount == CurrencyAmount(Decimal("7250.50"), "USD")
    assert credit.estimatedAmount == CurrencyAmount(Decimal("7100.25"), "USD")
    assert credit.applicableProductNames == [
        "Amazon Elastic Compute Cloud",
        "Amazon Simple Storage Service",
    ]
    assert credit.description == "Migration Acceleration Program credit"
    assert credit.startDate == date(2024, 1, 15)
    assert credit.endDate == date(2025, 1, 14)
    assert credit.applicationType == "AUTOMATIC"
    assert credit.shareableAccounts == ["222222222222", "333333333333"]
    assert credit.accountHasCreditSharingEnabled is True
    assert credit.creditConsoleVisibility == "VISIBLE"
    assert credit.creditSharingType == "ACCOUNT"
    assert credit.creditStatus == "ENABLED"
    # This fixture record omits the optional exhaustDate field and the Phase 6
    # cost-category binding fields (costCategoryArn, ruleName), which are
    # present only for credits bound to a cost-category sharing rule.
    assert credit.exhaustDate is None
    assert credit.costCategoryArn is None
    assert credit.ruleName is None
    assert credit.missing_fields == ["exhaustDate", "costCategoryArn", "ruleName"]


def test_record_with_every_field_present_records_nothing_missing():
    """When a record carries every field it has, only the optional Phase 6
    cost-category binding fields are recorded missing, and every other
    attribute is populated (Req 1.2).

    The live-shape fixture is an unbound credit, so ``costCategoryArn`` and
    ``ruleName`` are legitimately absent; they appear only on credits bound to
    a cost-category sharing rule (Req 17.1-17.3)."""
    page = _load("get_credits_live_shape.json")
    credit = parse_credit(page["credits"][0])

    phase6_binding = {"costCategoryArn", "ruleName"}
    assert credit.missing_fields == [n for n in CREDIT_FIELDS if n in phase6_binding]
    for name in CREDIT_FIELDS:
        if name in phase6_binding:
            assert getattr(credit, name) is None, name
        else:
            assert getattr(credit, name) is not None, name


# --------------------------------------------------------------------------
# Omitted fields: recorded exactly, treated as absent
# --------------------------------------------------------------------------


def test_omitted_fields_are_listed_exactly_in_missing_fields():
    """A record that omits fields lists exactly the omitted names in
    ``missing_fields``, in ``CREDIT_FIELDS`` order, and leaves them at their
    default (Req 1.6)."""
    page = _load("get_credits_missing_fields.json")
    credit = parse_credit(page["credits"][0])

    # The fixture omits estimatedAmount, description, endDate, shareableAccounts,
    # exhaustDate, and the Phase 6 cost-category binding fields costCategoryArn
    # and ruleName (creditSharingType is present with value "NONE").
    expected_missing = [
        "estimatedAmount",
        "description",
        "endDate",
        "shareableAccounts",
        "exhaustDate",
        "costCategoryArn",
        "ruleName",
    ]
    assert credit.missing_fields == expected_missing

    # missing_fields must equal exactly the set of fields absent from the raw
    # record, computed independently of the hand-written list above.
    raw = page["credits"][0]
    computed_missing = [name for name in CREDIT_FIELDS if name not in raw]
    assert credit.missing_fields == computed_missing

    # Omitted scalar fields default to None; omitted list fields default to [].
    assert credit.estimatedAmount is None
    assert credit.description is None
    assert credit.endDate is None
    assert credit.exhaustDate is None
    assert credit.shareableAccounts == []
    # creditSharingType is present in this fixture with the value "NONE".
    assert credit.creditSharingType == "NONE"

    # Present fields are still parsed correctly alongside the omissions.
    assert credit.creditId == "credit-0004"
    assert credit.remainingAmount == CurrencyAmount(Decimal("800.00"), "USD")
    assert credit.startDate == date(2024, 11, 1)
    assert credit.accountHasCreditSharingEnabled is False


def test_missing_fields_preserves_credit_fields_order():
    """``missing_fields`` follows the canonical CREDIT_FIELDS order, not the
    order fields happen to appear (or be absent) in the raw record."""
    # Omit two fields whose canonical order differs from insertion order.
    raw = {"creditStatus": "ENABLED", "creditId": "c-1", "accountId": "1"}
    credit = parse_credit(raw)

    # Every field except the three provided should be recorded, in order.
    provided = {"creditStatus", "creditId", "accountId"}
    expected = [name for name in CREDIT_FIELDS if name not in provided]
    assert credit.missing_fields == expected


def test_empty_record_records_all_fields_missing():
    """An empty dict records every field as missing and never raises."""
    credit = parse_credit({})
    assert credit.missing_fields == list(CREDIT_FIELDS)
    assert isinstance(credit, Credit)


# --------------------------------------------------------------------------
# parse_credits: page flattening across the sample fixtures
# --------------------------------------------------------------------------


def test_parse_credits_flattens_all_pages_in_order():
    """``parse_credits`` concatenates the credits of every page in order
    (the two-page pagination fixtures)."""
    pages = [_load("get_credits_page1.json"), _load("get_credits_page2.json")]
    credits = parse_credits(pages)

    assert [c.creditId for c in credits] == [
        "credit-0001",
        "credit-0002",
        "credit-0003",
    ]


def test_parse_credits_empty_input_returns_empty_list():
    assert parse_credits([]) == []


# --------------------------------------------------------------------------
# Log/notification injection sanitization (CWE-117)
# --------------------------------------------------------------------------


def test_parse_str_escapes_crlf_and_control_chars():
    """Parsed strings have CR/LF and C0 controls escaped so an API-sourced
    value cannot forge lines in logs, CLI output, or the SNS email body."""
    from aws_credits_reporter.models import parse_credit

    credit = parse_credit(
        {"creditId": "c1", "description": "legit\r\nERROR injected line\x00end"}
    )
    assert "\n" not in credit.description
    assert "\r" not in credit.description
    assert "\x00" not in credit.description
    assert credit.description == "legit\\x0d\\x0aERROR injected line\\x00end"


def test_parse_str_preserves_tab_and_clean_text():
    """TAB and ordinary text are not altered by sanitization."""
    from aws_credits_reporter.models import parse_credit

    credit = parse_credit({"creditId": "c1", "description": "col1\tcol2 normal"})
    assert credit.description == "col1\tcol2 normal"
