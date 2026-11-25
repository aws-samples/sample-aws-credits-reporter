"""Unit tests for the Phase 6 cost-category binding fields (task 22.2).

Phase 6 added two fields to the Credit model that authoritatively bind a
credit to a cost-category sharing rule: ``costCategoryArn`` and ``ruleName``
(task 22.1). These tests exercise ``parse_credit`` against the four captured
Phase 6 ``get-credits`` fixtures and pin:

* the bound fixtures parse ``costCategoryArn`` and ``ruleName`` into typed
  string attributes and do not record them as missing;
* the unbound fixtures leave both at ``None`` and record both in
  ``missing_fields`` (Requirements 17.1, 17.2);
* amounts still parse from the live ``currencyCode`` / ``currencyAmount``
  shape alongside the new fields.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from aws_credits_reporter.models import CurrencyAmount, parse_credit

FIXTURES = Path(__file__).parent / "fixtures"

BOUND_ARN = "arn:aws:ce::111111111111:costcategory/11111111-aaaa-bbbb-cccc-000000000010"


def _first_credit(name: str) -> dict:
    page = json.loads((FIXTURES / name).read_text())
    return page["credits"][0]


# --------------------------------------------------------------------------
# Bound fixtures: the binding fields parse and are not recorded missing
# --------------------------------------------------------------------------


def test_bound_active_fixture_parses_cost_category_arn_and_rule_name():
    """The bound-active fixture populates ``costCategoryArn`` and ``ruleName``
    and records neither as missing (Req 17.1)."""
    credit = parse_credit(_first_credit("get_credits_cc_bound_active.json"))

    assert credit.costCategoryArn == BOUND_ARN
    assert credit.ruleName == "Credit_Rule"
    assert "costCategoryArn" not in credit.missing_fields
    assert "ruleName" not in credit.missing_fields


def test_bound_paused_fixture_parses_cost_category_arn_and_rule_name():
    """The bound-paused fixture is DISABLED but still carries the binding, so
    both fields parse and neither is recorded missing (Req 17.1)."""
    credit = parse_credit(_first_credit("get_credits_cc_bound_paused.json"))

    assert credit.costCategoryArn == BOUND_ARN
    assert credit.ruleName == "Credit_Rule"
    assert "costCategoryArn" not in credit.missing_fields
    assert "ruleName" not in credit.missing_fields
    assert credit.creditStatus == "DISABLED"


# --------------------------------------------------------------------------
# Unbound fixtures: both binding fields are absent and recorded missing
# --------------------------------------------------------------------------


def test_unbound_active_fixture_records_binding_fields_missing():
    """The unbound-active fixture omits both binding fields, so each is left
    at ``None`` and listed in ``missing_fields`` (Req 17.2)."""
    credit = parse_credit(_first_credit("get_credits_cc_unbound_active.json"))

    assert credit.costCategoryArn is None
    assert credit.ruleName is None
    assert "costCategoryArn" in credit.missing_fields
    assert "ruleName" in credit.missing_fields


def test_unbound_paused_fixture_records_binding_fields_missing():
    """The unbound-paused fixture omits both binding fields, so each is left
    at ``None`` and listed in ``missing_fields`` (Req 17.2)."""
    credit = parse_credit(_first_credit("get_credits_cc_unbound_paused.json"))

    assert credit.costCategoryArn is None
    assert credit.ruleName is None
    assert "costCategoryArn" in credit.missing_fields
    assert "ruleName" in credit.missing_fields


# --------------------------------------------------------------------------
# Amounts still parse from the live currencyCode / currencyAmount shape
# --------------------------------------------------------------------------


def test_amounts_parse_alongside_binding_fields_on_bound_fixture():
    """Adding the binding fields does not disturb amount parsing: the live
    ``currencyCode`` / ``currencyAmount`` shape still yields a CurrencyAmount."""
    credit = parse_credit(_first_credit("get_credits_cc_bound_active.json"))

    assert credit.initialAmount == CurrencyAmount(Decimal("5000.000000"), "USD")
    assert credit.remainingAmount == CurrencyAmount(Decimal("4200.000000"), "USD")
    assert credit.estimatedAmount == CurrencyAmount(Decimal("4200.000000"), "USD")


def test_amounts_parse_on_unbound_fixture_despite_missing_binding():
    """A missing binding does not affect amount parsing on an unbound credit."""
    credit = parse_credit(_first_credit("get_credits_cc_unbound_active.json"))

    assert credit.initialAmount == CurrencyAmount(Decimal("5000.000000"), "USD")
    assert credit.remainingAmount == CurrencyAmount(Decimal("4200.000000"), "USD")
    assert credit.estimatedAmount == CurrencyAmount(Decimal("4200.000000"), "USD")
