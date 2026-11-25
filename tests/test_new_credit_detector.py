"""Task 10.3: unit tests for New_Credit_Detector (Requirements 6.2, 6.5, 6.6, 6.7).

Covers the four scenarios called out by the task: a fixture pair with one
added credit, identical snapshots yielding zero new credits, a missing prior
snapshot yielding the initial baseline, and an unreadable prior yielding a
comparison error.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from aws_credits_reporter.models import Snapshot
from aws_credits_reporter.new_credit_detector import (
    BASELINE_MESSAGE,
    COMPARISON_ERROR_MESSAGE,
    ZERO_NEW_MESSAGE,
    NewCreditDetector,
    NewCreditResult,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _snapshot(pages: list[dict], key: str = "snapshots/x.json") -> Snapshot:
    return Snapshot(key=key, taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc), raw_get_credits=pages)


PAGE1 = _load("get_credits_page1.json")
PAGE2 = _load("get_credits_page2.json")


def test_detects_one_added_credit_with_its_remaining_total() -> None:
    prior = _snapshot([PAGE1], key="snapshots/prior.json")
    current = _snapshot([PAGE1, PAGE2], key="snapshots/current.json")

    result = NewCreditDetector().detect(current, prior)

    assert isinstance(result, NewCreditResult)
    assert result.new_credit_ids == {"credit-0003"}
    assert [c.creditId for c in result.new_credits] == ["credit-0003"]
    assert result.total_new_remaining == Decimal("1234.56")
    assert result.new_remaining_currency == "USD"
    assert result.is_initial_baseline is False
    assert result.comparison_error is False
    assert result.messages == []


def test_identical_snapshots_yield_zero_new_credits() -> None:
    prior = _snapshot([PAGE1, PAGE2])
    current = _snapshot([PAGE1, PAGE2])

    result = NewCreditDetector().detect(current, prior)

    assert result.new_credit_ids == set()
    assert result.new_credits == []
    assert result.total_new_remaining == Decimal("0")
    assert result.new_remaining_currency is None
    assert result.messages == [ZERO_NEW_MESSAGE]


def test_missing_prior_is_initial_baseline() -> None:
    current = _snapshot([PAGE1, PAGE2])

    result = NewCreditDetector().detect(current, None)

    assert result.is_initial_baseline is True
    assert result.comparison_error is False
    assert result.new_credit_ids == set()
    assert result.new_credits == []
    assert result.messages == [BASELINE_MESSAGE]
    assert "no prior run available" in BASELINE_MESSAGE


def test_unreadable_prior_is_comparison_error_and_takes_precedence() -> None:
    current = _snapshot([PAGE1, PAGE2])

    with_prior = NewCreditDetector().detect(current, _snapshot([PAGE1]), prior_read_ok=False)
    without_prior = NewCreditDetector().detect(current, None, prior_read_ok=False)

    for result in (with_prior, without_prior):
        assert result.comparison_error is True
        assert result.is_initial_baseline is False
        assert result.new_credit_ids == set()
        assert result.new_credits == []
        assert result.messages == [COMPARISON_ERROR_MESSAGE]
        assert result.messages[0].startswith("comparison error:")


def test_total_sums_only_new_credits_and_treats_missing_amount_as_zero() -> None:
    prior = _snapshot([PAGE1])
    extra = {
        "credits": [
            {"creditId": "credit-0004", "remainingAmount": {"currencyAmount": "10.00", "currencyCode": "USD"}},
            {"creditId": "credit-0005"},  # no remainingAmount -> counts as 0
            {"accountId": "999999999999"},  # no creditId -> ignored for detection
        ]
    }
    current = _snapshot([PAGE1, PAGE2, extra])

    result = NewCreditDetector().detect(current, prior)

    assert result.new_credit_ids == {"credit-0003", "credit-0004", "credit-0005"}
    assert [c.creditId for c in result.new_credits] == ["credit-0003", "credit-0004", "credit-0005"]
    # 1234.56 (credit-0003) + 10.00 (credit-0004) + 0 (credit-0005); credit-0001's
    # 7250.50 is not new and must not be included.
    assert result.total_new_remaining == Decimal("1244.56")
    assert result.new_remaining_currency == "USD"
