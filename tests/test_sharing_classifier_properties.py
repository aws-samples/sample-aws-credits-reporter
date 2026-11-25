"""Property tests for ``aws_credits_reporter.sharing_classifier`` (Task 18.2, revised Phase 8).

Property 11 / group totality (Validates: Requirements 14.1):
    For any list of credits and any list of cost categories,
    ``SharingClassifier.classify`` assigns each credit to exactly one of the
    two sharing-option groups (``cost-category-based``, ``Shared by default``);
    the union of the groups equals the input credits, with no credit in more
    than one group.

Property (precedence and binding, Validates: Requirements 14.4, 18.1, 18.2, 18.4):
    A credit is ``cost-category-based`` if and only if it has no
    shareableAccounts AND carries both a non-empty costCategoryArn and a
    non-empty ruleName. Every other credit, including one with non-empty
    shareableAccounts (even with a full binding), is ``Shared by default``.

Totality is checked structurally rather than by creditId, because creditId is
optional and two generated credits may collide on it (or both be None).

No mocking library is used: ``Credit`` and ``CostCategory`` instances are built
directly and fed through the production ``classify`` path.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from aws_credits_reporter.models import CostCategory, Credit
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SharingClassifier,
)

_SHARING_OPTIONS = {COST_CATEGORY_BASED, SHARED_BY_DEFAULT}

_ARNS = [None, "", "arn:aws:ce::111:costcategory/a", "arn:aws:ce::111:costcategory/b"]
_RULES = [None, "", "Rule_A", "Rule_B"]

_shareable_accounts = st.lists(
    st.sampled_from([f"acct-{i:03d}" for i in range(8)]),
    max_size=4,
)


@st.composite
def _credits(draw: st.DrawFn) -> Credit:
    """A Credit whose classification-relevant fields are drawn independently.

    creditId is drawn from a tiny alphabet (with None allowed) so collisions
    happen often, confirming totality does not rely on unique ids.
    """
    return Credit(
        creditId=draw(st.sampled_from([None, "c-1", "c-2", "c-3"])),
        shareableAccounts=draw(_shareable_accounts),
        costCategoryArn=draw(st.sampled_from(_ARNS)),
        ruleName=draw(st.sampled_from(_RULES)),
    )


_categories = st.builds(
    CostCategory,
    CostCategoryArn=st.sampled_from(_ARNS),
    Name=st.sampled_from([None, "Sharing Cat", "Other Cat"]),
)


@settings(max_examples=400)
@given(
    credits=st.lists(_credits(), max_size=12),
    categories=st.lists(_categories, max_size=6),
)
def test_group_totality(
    credits: list[Credit], categories: list[CostCategory]
) -> None:
    """Every credit lands in exactly one of the two groups; the groups
    partition the input credits with no overlap and no omission (Req 14.1)."""
    result = SharingClassifier().classify(credits, categories)
    groups = result.groups

    assert len(groups) == len(credits)
    for credit, group in zip(credits, groups):
        assert group.credit is credit
        assert group.sharing_option in _SHARING_OPTIONS

    indices_by_option: dict[str, set[int]] = {
        COST_CATEGORY_BASED: set(),
        SHARED_BY_DEFAULT: set(),
    }
    for i, group in enumerate(groups):
        indices_by_option[group.sharing_option].add(i)

    cost_cat, default = indices_by_option.values()
    assert cost_cat & default == set()
    assert cost_cat | default == set(range(len(credits)))


@settings(max_examples=400)
@given(
    credits=st.lists(_credits(), max_size=12),
    categories=st.lists(_categories, max_size=6),
)
def test_cost_category_based_iff_full_binding_and_no_shareable_accounts(
    credits: list[Credit], categories: list[CostCategory]
) -> None:
    """cost-category-based iff no shareableAccounts and both ARN and rule set."""
    result = SharingClassifier().classify(credits, categories)
    for group in result.groups:
        credit = group.credit
        expect_cc = (
            not credit.shareableAccounts
            and bool(credit.costCategoryArn)
            and bool(credit.ruleName)
        )
        expected = COST_CATEGORY_BASED if expect_cc else SHARED_BY_DEFAULT
        assert group.sharing_option == expected
