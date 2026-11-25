"""Tests for the "NEEDS ATTENTION" block of ``build_dashboard_body`` (Requirement 26).

Unit tests pin the block's placement (after the "Credits by sharing option"
block, before the trailing EXPIRED/INACTIVE CREDITS listing), its absence when
there are no items, the one-bullet-per-creditId format, and byte-for-byte
stability of the rest of the body. Property 23 from design.md checks the same
for generated inputs.

The body is built through the real analysis pipeline. No AWS client is built.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from aws_credits_reporter.attention_builder import AttentionItem, build_attention_items
from aws_credits_reporter.dashboard import build_dashboard
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import Credit, CurrencyAmount, Snapshot
from aws_credits_reporter.new_credit_detector import NewCreditDetector
from aws_credits_reporter.report_body import OVERVIEW_HEADING, DASHBOARD_SUMMARY_HEADING, EXPIRED_CREDITS_HEADING, INACTIVE_CREDITS_HEADING, NEEDS_ATTENTION_HEADING, SHARING_SUMMARY_HEADING, build_dashboard_body
from aws_credits_reporter.sharing_classifier import SharingClassifier

NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
AS_OF = NOW.date()


def _credit(
    credit_id: str,
    *,
    remaining: str | None = "10.00",
    end: date | None = date(2027, 1, 1),
    status: str | None = "ENABLED",
    description: str | None = None,
) -> Credit:
    return Credit(
        creditId=credit_id,
        remainingAmount=(
            CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None
        ),
        endDate=end,
        creditStatus=status,
        description=description,
    )


def _parts(credits: list[Credit]):
    lifecycle = LifecycleAnalyzer(AS_OF).partition(credits)
    expiration = ExpirationAnalyzer(NOW).classify(lifecycle.active)
    current = Snapshot(key="current", taken_at=NOW, raw_get_credits=[])
    new = NewCreditDetector().detect(current, None, prior_read_ok=True)
    sharing = SharingClassifier().classify(credits, [])
    dashboard = build_dashboard(lifecycle, expiration, new, sharing)
    return lifecycle, expiration, new, sharing, dashboard


def _body(credits: list[Credit], attention: list[AttentionItem] | None = None) -> str:
    lifecycle, expiration, new, sharing, dashboard = _parts(credits)
    if attention is None:
        return build_dashboard_body(dashboard, expiration, new, lifecycle.inactive, sharing)
    return build_dashboard_body(
        dashboard, expiration, new, lifecycle.inactive, sharing, attention
    )


def _trailing_heading_index(body: str) -> int:
    for heading in (EXPIRED_CREDITS_HEADING, INACTIVE_CREDITS_HEADING):
        index = body.find(f"\n{heading}\n")
        if index != -1:
            return index + 1
    return len(body)  # no trailing listing: the last block runs to the end


def _expected_block(items: list[AttentionItem]) -> str:
    lines = [NEEDS_ATTENTION_HEADING]
    lines.extend(f"- {i.credit_id}: {'; '.join(i.reasons)}" for i in items)
    # Removing this text (blank line before, newline after) restores the body
    # built without the block, whether or not a trailing listing follows it.
    return "\n" + "\n".join(lines) + "\n"


ITEMS = [
    AttentionItem("19000000001", ["remaining amount, end date missing, so the credit cannot be classified"]),
    AttentionItem(
        "19000000007",
        ["only one of cost category and rule is set", "end date missing"],
    ),
]


# --------------------------------------------------------------------------
# Absence
# --------------------------------------------------------------------------


def test_no_block_when_attention_is_omitted():
    assert NEEDS_ATTENTION_HEADING not in _body([_credit("a")])


def test_no_block_when_attention_is_empty():
    assert NEEDS_ATTENTION_HEADING not in _body([_credit("a")], [])


def test_empty_attention_body_is_byte_identical_to_the_default():
    credits = [_credit("a"), _credit("b", end=date(2025, 1, 1))]

    assert _body(credits, []) == _body(credits)


# --------------------------------------------------------------------------
# Placement and format
# --------------------------------------------------------------------------


def test_block_sits_between_the_sharing_block_and_the_trailing_listing():
    body = _body([_credit("a"), _credit("old", end=date(2025, 1, 1))], ITEMS)

    sharing_at = body.index(SHARING_SUMMARY_HEADING)
    attention_at = body.index(NEEDS_ATTENTION_HEADING)
    trailing_at = _trailing_heading_index(body)

    assert body.count(NEEDS_ATTENTION_HEADING) == 1
    assert sharing_at < attention_at < trailing_at
    # Directly followed by the trailing listing, after one blank line.
    assert body[attention_at:trailing_at] == _expected_block(ITEMS).lstrip("\n") + "\n"


def test_block_precedes_an_inactive_listing_too():
    credits = [_credit("a"), _credit("old", end=date(2025, 1, 1))]
    body = _body(credits, ITEMS)

    # Only an Expired credit is listed, so the trailing listing is the EXPIRED one.
    assert f"\n{EXPIRED_CREDITS_HEADING}\n" in body
    assert body.index(NEEDS_ATTENTION_HEADING) < body.index(EXPIRED_CREDITS_HEADING)


def test_block_follows_the_dashboard_summary_and_alerts():
    body = _body([_credit("a", end=date(2026, 5, 31))], ITEMS)

    assert body.index(OVERVIEW_HEADING) < body.index(DASHBOARD_SUMMARY_HEADING)
    assert body.index(DASHBOARD_SUMMARY_HEADING) < body.index(NEEDS_ATTENTION_HEADING)


def test_one_bullet_per_credit_with_every_reason_on_that_bullet():
    body = _body([_credit("a")], ITEMS)

    block = body[body.index(NEEDS_ATTENTION_HEADING):_trailing_heading_index(body)]
    bullets = [line for line in block.splitlines() if line.startswith("- ")]

    assert bullets == [
        "- 19000000001: remaining amount, end date missing, so the credit cannot be classified",
        "- 19000000007: only one of cost category and rule is set; end date missing",
    ]


def test_rest_of_the_body_is_unchanged_when_the_block_is_added():
    credits = [_credit("a", end=date(2026, 5, 31)), _credit("old", end=date(2025, 1, 1))]
    without = _body(credits)
    with_block = _body(credits, ITEMS)

    assert with_block.replace(_expected_block(ITEMS), "", 1) == without


def test_body_built_from_the_real_builder_lists_each_affected_credit_once():
    credits = [
        _credit("good"),
        _credit("bad", end=None, status=None),
    ]
    lifecycle, _, _, sharing, _ = _parts(credits)
    items = build_attention_items(credits, lifecycle, sharing)

    body = _body(credits, items)

    assert body.count("- bad: ") == 1
    assert "- good: " not in body
    assert "end date, credit status missing, so the credit cannot be classified" in body


# ==========================================================================
# Property 23: the NEEDS ATTENTION block is present only when non-empty and
# sits between the sharing block and the trailing listing.
#
# Feature: aws-credits-reporter, Property 23.
# Validates: Requirements 26.1, 26.2, 26.4, 26.7, 5.3, 5.4
# ==========================================================================

_dates = st.one_of(st.none(), st.dates(min_value=date(2025, 1, 1), max_value=date(2028, 1, 1)))


@st.composite
def _p23_credits(draw) -> list[Credit]:
    count = draw(st.integers(min_value=0, max_value=6))
    credits = []
    for index in range(count):
        remaining = draw(st.one_of(st.none(), st.sampled_from(["0.00", "5.00", "120.50"])))
        credits.append(
            Credit(
                creditId=f"c{index:03d}",
                remainingAmount=(
                    CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None
                ),
                endDate=draw(_dates),
                creditStatus=draw(st.sampled_from([None, "ENABLED", "DISABLED"])),
                description=draw(st.one_of(st.none(), st.sampled_from(["Promo", "Trial"]))),
            )
        )
    return credits


_reason = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters=" ,"),
    min_size=1,
    max_size=40,
).filter(lambda text: text.strip() == text and text != "")


@st.composite
def _p23_attention(draw) -> list[AttentionItem]:
    ids = draw(st.lists(st.integers(min_value=0, max_value=999), max_size=5, unique=True))
    return [
        AttentionItem(
            credit_id=f"id{n:03d}",
            reasons=draw(st.lists(_reason, min_size=1, max_size=3)),
        )
        for n in sorted(ids)
    ]


@settings(max_examples=200, deadline=None)
@given(_p23_credits(), _p23_attention())
def test_property_23_block_presence_placement_and_isolation(credits, attention):
    base = _body(credits, [])
    body = _body(credits, attention)

    # No attachment footer or "attachments omitted" wording, ever (Req 5.3, 5.4).
    assert "attachment" not in body.lower()

    if not attention:
        assert NEEDS_ATTENTION_HEADING not in body
        assert body == base
        return

    # Present exactly once, between the sharing block and the trailing listing.
    assert body.count(NEEDS_ATTENTION_HEADING) == 1
    sharing_at = body.index(SHARING_SUMMARY_HEADING)
    attention_at = body.index(NEEDS_ATTENTION_HEADING)
    trailing_at = _trailing_heading_index(body)
    assert sharing_at < attention_at < trailing_at

    # Exactly one bullet per item, naming its creditId.
    block = body[attention_at:trailing_at]
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert [line.split(":", 1)[0] for line in bullets] == [f"- {i.credit_id}" for i in attention]

    # Every other block is byte-for-byte what it is with an empty list.
    assert body.replace(_expected_block(attention), "", 1) == base
