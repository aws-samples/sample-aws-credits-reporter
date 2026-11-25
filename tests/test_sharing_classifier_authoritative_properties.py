"""Property tests for the reworked Sharing_Classifier (Task 25.2, Phase 6).

Two properties over the authoritative (``costCategoryArn`` and ``ruleName``)
binding path and its interaction with the default branch:

Property 14 (option selection, Validates: Requirements 18.2, 18.4, 18.6):
    For any credit with an **empty** ``shareableAccounts``:

    * if it carries BOTH a non-empty ``costCategoryArn`` and a non-empty
      ``ruleName`` it is classified ``cost-category-based``, no matter which
      other categories exist (the binding is the authoritative signal);
    * otherwise it is ``Shared by default``.

Property 15 (member-account derivation, Validates: Requirements 19.3, 19.3a,
19.5):

    For an authoritative credit, when the injected retriever returns a
    definition, ``member_accounts`` are exactly the ``LINKED_ACCOUNT`` ``Values``
    of the rule whose ``Value == ruleName``, in order, and empty when no rule
    matches; when the retriever returns ``None`` (definition unavailable) the
    group sets ``member_accounts_unavailable=True`` with empty members. In every
    case the credit stays ``cost-category-based``.

No mocking library is used: the retriever and resolver are tiny hand-written
stubs with the production ``.describe(arn)`` / ``.resolve(ids)`` shapes, and
``Credit`` / ``CostCategory`` / ``CostCategoryDefinition`` instances are built
directly and fed through the production ``classify`` path.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import assume, given, settings, strategies as st

from aws_credits_reporter.models import CostCategory, CostCategoryDefinition, Credit
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SharingClassifier,
)


# --------------------------------------------------------------------------
# Stubs (production adapter shapes; no mocking library)
# --------------------------------------------------------------------------


@dataclass
class _Resolution:
    """Mirror of ``AccountNameResolution``: carries a ``.names`` dict."""

    names: dict[str, str | None]


class _StubResolver:
    """Resolver stub: maps requested ids to a fixed name table ."""

    def __init__(self, names: dict[str, str | None] | None = None):
        self._names = names or {}

    def resolve(self, account_ids: list[str]) -> _Resolution:
        return _Resolution(names={aid: self._names.get(aid) for aid in account_ids})


class _StubRetriever:
    """Retriever stub: returns a fixed definition for any ARN, or ``None``.

    ``None`` models a failed ``describe-cost-category-definition`` call, which
    must drive ``member_accounts_unavailable`` (Req 19.5).
    """

    def __init__(self, definition: CostCategoryDefinition | None):
        self._definition = definition

    def describe(self, cost_category_arn: str) -> CostCategoryDefinition | None:
        return self._definition


def _linked_account_rule(value: str, account_ids: list[str]) -> dict:
    """Build a cost-category rule binding ``value`` to LINKED_ACCOUNT ids."""
    return {
        "Value": value,
        "Rule": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": list(account_ids)}},
    }


# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------

_ARN = st.sampled_from(
    [f"arn:aws:ce::123456789012:costcategory/cat-{i}" for i in range(4)]
)
_RULE_NAME = st.sampled_from(["Credit_Rule", "Other_Rule", "Rule_X"])
_ACCOUNT_ID = st.sampled_from([f"{8000 + i:012d}" for i in range(8)])


# --------------------------------------------------------------------------
# Property 14: sharing-option selection for empty-shareableAccounts credits
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(
    arn=_ARN,
    rule_name=_RULE_NAME,
    has_matching_reference=st.booleans(),
)
def test_full_binding_is_cost_category_based(
    arn: str, rule_name: str, has_matching_reference: bool
) -> None:
    """Empty shareableAccounts + BOTH costCategoryArn and ruleName =>
    cost-category-based, whether or not a reference resolves the Name.
    **Validates: Requirements 18.2, 18.6**
    """
    credit = Credit(creditId="c", shareableAccounts=[], costCategoryArn=arn, ruleName=rule_name)
    categories = (
        [CostCategory(CostCategoryArn=arn, Name="CreditGroup")]
        if has_matching_reference
        else []
    )
    result = SharingClassifier(_StubRetriever(None)).classify([credit], categories)
    group = result.groups[0]
    assert group.sharing_option == COST_CATEGORY_BASED
    assert group.cost_category_arn == arn


@settings(max_examples=400)
@given(
    arn=st.one_of(st.none(), _ARN),
    rule_name=st.one_of(st.none(), _RULE_NAME),
)
def test_partial_or_no_binding_is_shared_by_default(
    arn: str | None, rule_name: str | None
) -> None:
    """Empty shareableAccounts and fewer than both binding fields: Shared by default.
    There is no fallback path (Req 18.4): nothing is guessed.
    **Validates: Requirements 18.4**
    """
    assume(arn is None or rule_name is None)
    credit = Credit(creditId="c", shareableAccounts=[], costCategoryArn=arn, ruleName=rule_name)
    categories = [CostCategory(CostCategoryArn=arn, Name="Team Sharing")] if arn else []
    result = SharingClassifier(_StubRetriever(None)).classify([credit], categories)
    group = result.groups[0]
    assert group.sharing_option == SHARED_BY_DEFAULT
    assert group.cost_category_arn is None


# --------------------------------------------------------------------------
# Property 15: member-account derivation on the authoritative path
# --------------------------------------------------------------------------


@settings(max_examples=400)
@given(
    arn=_ARN,
    rule_name=_RULE_NAME,
    bound_ids=st.lists(_ACCOUNT_ID, max_size=5),
    # Other rules in the definition that must NOT contribute members.
    other_rules=st.lists(
        st.tuples(st.sampled_from(["Zzz_Rule", "Qqq_Rule"]), st.lists(_ACCOUNT_ID, max_size=3)),
        max_size=3,
    ),
    rule_matches=st.booleans(),
)
def test_members_equal_matching_rule_values(
    arn: str,
    rule_name: str,
    bound_ids: list[str],
    other_rules: list[tuple[str, list[str]]],
    rule_matches: bool,
) -> None:
    """Members equal the LINKED_ACCOUNT Values of the rule whose Value == ruleName,
    in order; empty when no rule matches. The credit stays authoritative
    cost-category-based either way.

    **Validates: Requirements 19.3, 19.3a**
    """
    rules: list[dict] = [_linked_account_rule(v, ids) for v, ids in other_rules]
    if rule_matches:
        rules.append(_linked_account_rule(rule_name, bound_ids))
    definition = CostCategoryDefinition(arn=arn, name="CreditGroup", rules=rules)

    credit = Credit(creditId="c", shareableAccounts=[], costCategoryArn=arn, ruleName=rule_name)
    classifier = SharingClassifier(_StubRetriever(definition), _StubResolver())
    group = classifier.classify([credit], []).groups[0]

    assert group.sharing_option == COST_CATEGORY_BASED
    assert group.member_accounts_unavailable is False

    member_ids = [m.account_id for m in group.member_accounts]
    if rule_matches:
        assert member_ids == bound_ids
        assert group.rule_name == rule_name
    else:
        assert member_ids == []


@settings(max_examples=200)
@given(
    arn=_ARN,
    rule_name=_RULE_NAME,
)
def test_definition_unavailable_sets_members_unavailable(
    arn: str, rule_name: str
) -> None:
    """When the retriever returns no definition, members are empty and marked
    unavailable, while the credit stays authoritative cost-category-based.

    **Validates: Requirements 19.5**
    """
    credit = Credit(creditId="c", shareableAccounts=[], costCategoryArn=arn, ruleName=rule_name)
    group = SharingClassifier(_StubRetriever(None)).classify([credit], []).groups[0]

    assert group.sharing_option == COST_CATEGORY_BASED
    assert group.member_accounts == []
    assert group.member_accounts_unavailable is True
