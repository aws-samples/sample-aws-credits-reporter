"""Unit tests for the reworked Sharing_Classifier against captured Phase 6 fixtures.

Phase 6 reworked the classifier to resolve ARN-bound credits authoritatively
(Req 18.2, 18.3, 18.6), derive the bound rule and its member accounts from a
``describe-cost-category-definition`` payload (Req 19.3, 19.3a), and degrade
gracefully when a rule or member names cannot be resolved. These tests drive
the classifier with the four real ``get-credits`` states captured for credit
``19000000003`` and the real ``describe-cost-category-definition`` payload.

The two AWS adapters are injected as small stubs built directly from the
describe fixture, in the style of the fake clients used by
``test_category_definition_retriever.py`` and ``test_account_name_resolver.py``.
No real AWS account is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_credits_reporter.models import (
    CostCategory,
    CostCategoryDefinition,
    parse_credits,
)
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    UNRESOLVED_NAME,
    SharingClassifier,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _credits(name: str):
    """Parse the credits from a captured single-page ``get-credits`` fixture."""
    return parse_credits([_load(name)])


def _definition_from_fixture() -> CostCategoryDefinition:
    """Build a ``CostCategoryDefinition`` from the describe fixture's payload."""
    payload = _load("describe_cost_category_definition.json")["CostCategory"]
    return CostCategoryDefinition(
        arn=payload["CostCategoryArn"],
        name=payload["Name"],
        rules=payload["Rules"],
    )


def _reference_category() -> CostCategory:
    """A Phase-5 reference whose ARN matches the bound credit's ``costCategoryArn``.

    The classifier resolves the Bound_Cost_Category Name by matching the
    credit's ``costCategoryArn`` against ``CostCategory.CostCategoryArn`` from
    the categories list (Req 18.3). The real captured category carries no
    ``SupportedResourceTypes`` so it is not a Phase-5 credit-sharing category;
    the authoritative path classifies via the ARN regardless.
    """
    payload = _load("describe_cost_category_definition.json")["CostCategory"]
    return CostCategory(
        CostCategoryArn=payload["CostCategoryArn"],
        Name=payload["Name"],
    )


class StubRetriever:
    """Returns the fixture-derived definition for its ARN, else ``None``."""

    def __init__(self, definition: CostCategoryDefinition | None):
        self._definition = definition
        self.calls: list[str] = []

    def describe(self, cost_category_arn: str):
        self.calls.append(cost_category_arn)
        if self._definition is not None and cost_category_arn == self._definition.arn:
            return self._definition
        return None


class StubResolver:
    """Resolves the given id->name map, exposing it as ``.names``."""

    def __init__(self, names: dict[str, str | None]):
        self._names = names
        self.calls: list[list[str]] = []

    def resolve(self, account_ids: list[str]):
        self.calls.append(list(account_ids))
        return _Resolution(self._names)


class _Resolution:
    def __init__(self, names: dict[str, str | None]):
        self.names = names


MEMBER_IDS = ["444444444444", "555555555555"]


# --------------------------------------------------------------------------
# bound-active: authoritative, Name / rule / member accounts all resolved
# (Req 18.2, 18.3, 18.6, 19.3)
# --------------------------------------------------------------------------


def test_bound_active_is_authoritative_with_name_rule_and_members():
    credits = _credits("get_credits_cc_bound_active.json")
    definition = _definition_from_fixture()
    resolver = StubResolver(
        {"444444444444": "Platform", "555555555555": "DataScience"}
    )
    classifier = SharingClassifier(StubRetriever(definition), resolver)

    result = classifier.classify(credits, [_reference_category()])

    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.sharing_option == COST_CATEGORY_BASED
    # Authoritative: the ARN's presence is the authoritative signal.
    assert group.cost_category_name == "CreditGroup"
    assert group.rule_name == "Credit_Rule"
    assert group.member_accounts_unavailable is False
    assert [m.account_id for m in group.member_accounts] == MEMBER_IDS
    assert [m.account_name for m in group.member_accounts] == [
        "Platform",
        "DataScience",
    ]


def test_bound_active_members_fall_back_to_bare_ids_when_names_unresolved():
    credits = _credits("get_credits_cc_bound_active.json")
    classifier = SharingClassifier(
        StubRetriever(_definition_from_fixture()), StubResolver({})
    )

    group = classifier.classify(credits, [_reference_category()]).groups[0]

    assert [m.account_id for m in group.member_accounts] == MEMBER_IDS
    assert [m.account_name for m in group.member_accounts] == [None, None]


# --------------------------------------------------------------------------
# unbound-active: no ARN, no credit-sharing category -> Shared by default
# (Req 18.4)
# --------------------------------------------------------------------------


def test_unbound_active_is_shared_by_default():
    credits = _credits("get_credits_cc_unbound_active.json")
    classifier = SharingClassifier(
        StubRetriever(_definition_from_fixture()),
        StubResolver({}),
    )

    # The reference category carries no SupportedResourceTypes, so it is not a
    # Phase-5 credit-sharing category; with no ARN on the credit, there is
    # nothing to bind to.
    result = classifier.classify(credits, [_reference_category()])

    group = result.groups[0]
    assert group.sharing_option == SHARED_BY_DEFAULT
    assert group.member_accounts == []


# --------------------------------------------------------------------------
# bound-paused: DISABLED lifecycle, yet still authoritative cost-category-based
# (Req 18.2 - sharing classification is independent of credit status)
# --------------------------------------------------------------------------


def test_bound_paused_is_still_authoritative_cost_category_based():
    credits = _credits("get_credits_cc_bound_paused.json")
    # Sanity: this captured state is the DISABLED (paused) one.
    assert credits[0].creditStatus == "DISABLED"
    classifier = SharingClassifier(
        StubRetriever(_definition_from_fixture()),
        StubResolver({"444444444444": "Platform", "555555555555": "DataScience"}),
    )

    group = classifier.classify(credits, [_reference_category()]).groups[0]

    assert group.sharing_option == COST_CATEGORY_BASED
    assert group.cost_category_name == "CreditGroup"
    assert group.rule_name == "Credit_Rule"
    assert [m.account_id for m in group.member_accounts] == MEMBER_IDS


# --------------------------------------------------------------------------
# ruleName with no matching rule -> rule unresolved + empty members
# (Req 19.3a)
# --------------------------------------------------------------------------


def test_rule_name_with_no_matching_rule_is_unresolved_with_empty_members():
    # The credit's ruleName does not match any rule Value in the definition.
    payload = _load("describe_cost_category_definition.json")["CostCategory"]
    mismatched = CostCategoryDefinition(
        arn=payload["CostCategoryArn"],
        name=payload["Name"],
        rules=[{"Value": "Some_Other_Rule", "Rule": payload["Rules"][0]["Rule"]}],
    )
    credits = _credits("get_credits_cc_bound_active.json")
    classifier = SharingClassifier(
        StubRetriever(mismatched),
        StubResolver({"444444444444": "Platform"}),
    )

    group = classifier.classify(credits, [_reference_category()]).groups[0]

    # Still authoritative cost-category-based; only the rule failed to resolve.
    assert group.sharing_option == COST_CATEGORY_BASED
    assert group.cost_category_name == "CreditGroup"
    assert group.rule_name == UNRESOLVED_NAME
    assert group.member_accounts == []
    assert group.member_accounts_unavailable is False


# --------------------------------------------------------------------------
# A definition with no SupportedResourceTypes still classifies via the ARN
# (Req 18.2) - the captured category genuinely lacks SupportedResourceTypes.
# --------------------------------------------------------------------------


def test_classifies_via_arn_even_though_category_has_no_supported_resource_types():
    reference = _reference_category()
    # The real captured reference carries no credit-sharing resource type.
    assert reference.SupportedResourceTypes == []
    credits = _credits("get_credits_cc_bound_active.json")
    classifier = SharingClassifier(
        StubRetriever(_definition_from_fixture()),
        StubResolver({"444444444444": "Platform", "555555555555": "DataScience"}),
    )

    group = classifier.classify(credits, [reference]).groups[0]

    # The ARN drives the authoritative binding; the absence of a Phase-5
    # credit-sharing category does not downgrade it to Shared by default.
    assert group.sharing_option == COST_CATEGORY_BASED
    assert group.cost_category_arn == reference.CostCategoryArn
    assert group.cost_category_name == "CreditGroup"
