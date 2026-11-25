"""Unit and property tests for the Attention_Builder (Phase 8, Requirement 26).

``build_attention_items`` is a pure function over the run's credits, the
``LifecycleResult`` and the ``SharingResult``. The unit tests pin each
condition (a) to (d) of Requirement 26.1, the Active-only scope of the sharing
conditions, the all-credit scope of the missing-field condition, the rule that
a missing ``exhaustDate`` is never listed, the merge of reasons per creditId,
and the ordering. The property test is Property 22 from design.md.

The sharing groups come from the real ``SharingClassifier`` fed by small
stubs, so the inputs stay internally consistent. No AWS client is built.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from aws_credits_reporter.attention_builder import (
    REASON_DEFINITION_UNAVAILABLE,
    REASON_MISSING_FIELDS_SUFFIX,
    REASON_NAME_UNRESOLVED,
    REASON_NAMES_UNRESOLVED_PREFIX,
    REASON_PARTIAL_BINDING,
    REASON_RULE_UNRESOLVED,
    AttentionItem,
    build_attention_items,
)
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    CostCategory,
    CostCategoryDefinition,
    Credit,
    CurrencyAmount,
)
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    UNRESOLVED_NAME,
    SharingClassifier,
)

AS_OF = date(2026, 5, 15)
ARN = "arn:aws:ce::111111111111:costcategory/11111111-aaaa-bbbb-cccc-000000000010"
RULE = "Credit_Rule"
MEMBERS = ["444444444444", "555555555555"]


# --------------------------------------------------------------------------
# Stubs and builders
# --------------------------------------------------------------------------


class StubRetriever:
    """Returns a fixed definition (or ``None`` to simulate a failed describe)."""

    def __init__(self, definition: CostCategoryDefinition | None):
        self._definition = definition

    def describe(self, cost_category_arn: str):
        return self._definition


class _Resolution:
    def __init__(self, names: dict[str, str | None]):
        self.names = names


class StubResolver:
    def __init__(self, names: dict[str, str | None]):
        self._names = names

    def resolve(self, account_ids: list[str]):
        return _Resolution(self._names)


def _definition(rule_value: str = RULE, members: list[str] | None = None):
    return CostCategoryDefinition(
        arn=ARN,
        name="CreditGroup",
        rules=[
            {
                "Value": rule_value,
                "Rule": {
                    "Dimensions": {
                        "Key": "LINKED_ACCOUNT",
                        "Values": list(MEMBERS if members is None else members),
                    }
                },
            }
        ],
    )


def _credit(
    credit_id: str | None = "c1",
    *,
    remaining: str | None = "10.00",
    end: date | None = date(2027, 1, 1),
    status: str | None = "ENABLED",
    exhaust: date | None = None,
    arn: str | None = None,
    rule: str | None = None,
    shareable: list[str] | None = None,
) -> Credit:
    return Credit(
        creditId=credit_id,
        remainingAmount=(
            CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None
        ),
        endDate=end,
        creditStatus=status,
        exhaustDate=exhaust,
        costCategoryArn=arn,
        ruleName=rule,
        shareableAccounts=shareable or [],
    )


def _items(
    credits: list[Credit],
    *,
    retriever: StubRetriever | None = None,
    resolver: StubResolver | None = None,
    categories: list[CostCategory] | None = None,
) -> list[AttentionItem]:
    lifecycle = LifecycleAnalyzer(AS_OF).partition(credits)
    sharing = SharingClassifier(retriever, resolver).classify(credits, categories or [])
    return build_attention_items(credits, lifecycle, sharing)


def _named_category() -> CostCategory:
    return CostCategory(CostCategoryArn=ARN, Name="CreditGroup")


def _resolved_stubs() -> tuple[StubRetriever, StubResolver, list[CostCategory]]:
    return (
        StubRetriever(_definition()),
        StubResolver({MEMBERS[0]: "Platform", MEMBERS[1]: "DataScience"}),
        [_named_category()],
    )


# --------------------------------------------------------------------------
# No attention
# --------------------------------------------------------------------------


def test_empty_input_yields_no_items():
    assert _items([]) == []


def test_fully_resolved_credits_yield_no_items():
    retriever, resolver, categories = _resolved_stubs()
    credits = [
        _credit("bound", arn=ARN, rule=RULE),
        _credit("plain"),
        _credit("shared", shareable=["222222222222"]),
    ]

    assert _items(credits, retriever=retriever, resolver=resolver, categories=categories) == []


# --------------------------------------------------------------------------
# Condition (a): partial binding
# --------------------------------------------------------------------------


def test_partial_binding_arn_only_is_listed():
    items = _items([_credit("p1", arn=ARN)])

    assert [(i.credit_id, i.reasons) for i in items] == [("p1", [REASON_PARTIAL_BINDING])]


def test_partial_binding_rule_only_is_listed():
    items = _items([_credit("p2", rule=RULE)])

    assert [(i.credit_id, i.reasons) for i in items] == [("p2", [REASON_PARTIAL_BINDING])]


def test_partial_binding_credit_stays_shared_by_default():
    credit = _credit("p3", arn=ARN)
    lifecycle = LifecycleAnalyzer(AS_OF).partition([credit])
    sharing = SharingClassifier().classify([credit], [])
    before = [(g.sharing_option, g.cost_category_name) for g in sharing.groups]

    build_attention_items([credit], lifecycle, sharing)

    assert [(g.sharing_option, g.cost_category_name) for g in sharing.groups] == before
    assert sharing.groups[0].sharing_option != COST_CATEGORY_BASED


# --------------------------------------------------------------------------
# Condition (b): unresolved Name, unresolved rule, unavailable definition
# --------------------------------------------------------------------------


def test_unresolved_category_name_is_listed():
    retriever, resolver, _ = _resolved_stubs()

    # No category reference matches the ARN, so the Name is "unresolved".
    items = _items(
        [_credit("n1", arn=ARN, rule=RULE)],
        retriever=retriever,
        resolver=resolver,
        categories=[],
    )

    assert [(i.credit_id, i.reasons) for i in items] == [("n1", [REASON_NAME_UNRESOLVED])]


def test_unresolved_rule_is_listed():
    _, resolver, categories = _resolved_stubs()

    items = _items(
        [_credit("r1", arn=ARN, rule=RULE)],
        retriever=StubRetriever(_definition(rule_value="Some_Other_Rule")),
        resolver=resolver,
        categories=categories,
    )

    assert [(i.credit_id, i.reasons) for i in items] == [("r1", [REASON_RULE_UNRESOLVED])]


def test_unavailable_definition_is_listed_with_definition_reason_only():
    _, resolver, categories = _resolved_stubs()

    items = _items(
        [_credit("d1", arn=ARN, rule=RULE)],
        retriever=StubRetriever(None),
        resolver=resolver,
        categories=categories,
    )

    # The rule is not separately reported: it was never checked (no definition).
    assert [(i.credit_id, i.reasons) for i in items] == [("d1", [REASON_DEFINITION_UNAVAILABLE])]


def test_no_adapters_injected_lists_definition_unavailable():
    items = _items([_credit("d2", arn=ARN, rule=RULE)], categories=[_named_category()])

    assert [i.reasons for i in items] == [[REASON_DEFINITION_UNAVAILABLE]]


# --------------------------------------------------------------------------
# Condition (c): member names unresolved
# --------------------------------------------------------------------------


def test_unresolved_member_names_are_listed_with_their_ids():
    retriever, _, categories = _resolved_stubs()

    items = _items(
        [_credit("m1", arn=ARN, rule=RULE)],
        retriever=retriever,
        resolver=StubResolver({MEMBERS[0]: "Platform"}),  # second id unnamed
        categories=categories,
    )

    assert len(items) == 1
    (reason,) = items[0].reasons
    assert reason.startswith(REASON_NAMES_UNRESOLVED_PREFIX)
    assert MEMBERS[1] in reason
    assert MEMBERS[0] not in reason


def test_all_member_names_unresolved_lists_every_member_id():
    retriever, _, categories = _resolved_stubs()

    items = _items(
        [_credit("m2", arn=ARN, rule=RULE)],
        retriever=retriever,
        resolver=StubResolver({}),
        categories=categories,
    )

    (reason,) = items[0].reasons
    assert all(member in reason for member in MEMBERS)


# --------------------------------------------------------------------------
# Condition (d): missing Classification_Fields, every credit
# --------------------------------------------------------------------------


def test_each_missing_classification_field_is_listed():
    for kwargs, label in [
        ({"remaining": None}, "remaining amount"),
        ({"end": None}, "end date"),
        ({"status": None}, "credit status"),
    ]:
        items = _items([_credit("f1", **kwargs)])

        assert [i.reasons for i in items] == [[f"{label} {REASON_MISSING_FIELDS_SUFFIX}"]]


def test_several_missing_fields_share_one_reason_in_fixed_order():
    items = _items([_credit("f2", remaining=None, end=None, status=None)])

    assert [i.reasons for i in items] == [
        [f"remaining amount, end date, credit status {REASON_MISSING_FIELDS_SUFFIX}"]
    ]


def test_missing_fields_recorded_by_the_parser_count_even_when_value_present():
    credit = _credit("f3")
    credit.missing_fields = ["endDate"]

    items = _items([credit])

    assert [i.reasons for i in items] == [[f"end date {REASON_MISSING_FIELDS_SUFFIX}"]]


def test_missing_field_condition_covers_inactive_credits():
    # An Expired credit (endDate before the snapshot date) that lacks its
    # creditStatus is still listed: condition (d) is evaluated over every credit.
    expired = _credit("old", end=date(2025, 1, 1), status=None)
    assert expired in LifecycleAnalyzer(AS_OF).partition([expired]).inactive

    items = _items([expired])

    assert [i.credit_id for i in items] == ["old"]


def test_missing_exhaust_date_alone_is_never_listed():
    credits = [
        _credit("e1", exhaust=None),
        _credit("e2", exhaust=None),
    ]
    credits[1].missing_fields = ["exhaustDate"]

    assert _items(credits) == []


# --------------------------------------------------------------------------
# Scope: sharing conditions are Active_Credits only
# --------------------------------------------------------------------------


def test_sharing_conditions_skip_inactive_credits():
    # Each credit is Expired (endDate before the snapshot date), so none is an
    # Active_Credit; their sharing problems are not reported (Req 26.1, 22.7).
    old = date(2025, 1, 1)
    credits = [
        _credit("x-partial", end=old, arn=ARN),
        _credit("x-bound", end=old, arn=ARN, rule=RULE),
    ]
    lifecycle = LifecycleAnalyzer(AS_OF).partition(credits)
    assert lifecycle.active == []

    assert _items(credits, categories=[]) == []


def test_exhausted_and_paused_credits_skip_sharing_conditions():
    credits = [
        _credit("x-exhausted", remaining="0.00", arn=ARN),
        _credit("x-paused", status="DISABLED", rule=RULE),
    ]
    assert LifecycleAnalyzer(AS_OF).partition(credits).active == []

    assert _items(credits) == []


# --------------------------------------------------------------------------
# Merge and ordering
# --------------------------------------------------------------------------


def test_reasons_merge_into_one_item_in_the_fixed_order():
    # Name unresolved, definition unavailable, and a missing end date at once.
    items = _items(
        [_credit("multi", end=None, arn=ARN, rule=RULE)],
        retriever=StubRetriever(None),
        categories=[],
    )

    assert len(items) == 1
    assert items[0].reasons == [
        REASON_NAME_UNRESOLVED,
        REASON_DEFINITION_UNAVAILABLE,
        f"end date {REASON_MISSING_FIELDS_SUFFIX}",
    ]


def test_partial_binding_precedes_missing_fields():
    items = _items([_credit("order", status=None, arn=ARN)])

    assert items[0].reasons == [
        REASON_PARTIAL_BINDING,
        f"credit status {REASON_MISSING_FIELDS_SUFFIX}",
    ]


def test_each_credit_id_appears_once_even_when_the_id_repeats():
    credits = [
        _credit("dup", arn=ARN),
        _credit("dup", status=None),
    ]

    items = _items(credits)

    assert [i.credit_id for i in items] == ["dup"]
    assert items[0].reasons == [
        REASON_PARTIAL_BINDING,
        f"credit status {REASON_MISSING_FIELDS_SUFFIX}",
    ]


def test_items_are_ordered_by_credit_id():
    credits = [
        _credit("30", arn=ARN),
        _credit("10", arn=ARN),
        _credit("20", arn=ARN),
    ]

    assert [i.credit_id for i in _items(credits)] == ["10", "20", "30"]


def test_credit_without_credit_id_is_named_by_position():
    items = _items([_credit("ok"), _credit(None, arn=ARN)])

    assert [i.credit_id for i in items] == ["<credit #2 without creditId>"]


def test_reasons_are_single_line_without_the_join_separator():
    # The notifier joins reasons with "; " on one bullet, so no reason may
    # contain a newline or that separator.
    retriever = StubRetriever(None)
    items = _items(
        [_credit("s1", remaining=None, end=None, status=None, arn=ARN, rule=RULE)],
        retriever=retriever,
        categories=[],
    )

    for reason in items[0].reasons:
        assert "\n" not in reason
        assert "; " not in reason


# ==========================================================================
# Property 22: attention items cover exactly the unresolved or uncategorized
# credits.
#
# Feature: aws-credits-reporter, Property 22.
# Validates: Requirements 26.1, 26.2, 26.3, 26.5
# ==========================================================================

_ARN_POOL = [
    f"arn:aws:ce::111111111111:costcategory/11111111-aaaa-bbbb-cccc-00000000000{n}"
    for n in range(3)
]
_RULE_POOL = ["Rule_A", "Rule_B"]
_ACCOUNT_POOL = ["777777777777", "888888888888", "999999999999"]

_dates = st.one_of(st.none(), st.dates(min_value=date(2025, 1, 1), max_value=date(2028, 1, 1)))


@st.composite
def _p22_credit(draw, index: int) -> Credit:
    remaining = draw(st.one_of(st.none(), st.sampled_from(["0.00", "5.00", "120.50"])))
    return Credit(
        creditId=f"c{index:03d}",
        remainingAmount=(
            CurrencyAmount(Decimal(remaining), "USD") if remaining is not None else None
        ),
        endDate=draw(_dates),
        creditStatus=draw(st.sampled_from([None, "ENABLED", "DISABLED"])),
        exhaustDate=draw(_dates),
        costCategoryArn=draw(st.one_of(st.none(), st.sampled_from(_ARN_POOL))),
        ruleName=draw(st.one_of(st.none(), st.sampled_from(_RULE_POOL))),
        shareableAccounts=draw(st.sampled_from([[], [], ["333333333333"]])),
    )


@st.composite
def _p22_scenario(draw):
    count = draw(st.integers(min_value=0, max_value=8))
    credits = [draw(_p22_credit(i)) for i in range(count)]

    # Per ARN: the Name (or none), and what describe returns (nothing, a
    # definition lacking the rule, or a definition with the rule and members).
    categories = [
        CostCategory(CostCategoryArn=arn, Name="Cat")
        for arn in _ARN_POOL
        if draw(st.booleans())
    ]
    definitions: dict[str, CostCategoryDefinition | None] = {}
    for arn in _ARN_POOL:
        mode = draw(st.sampled_from(["none", "wrong-rule", "ok"]))
        if mode == "none":
            definitions[arn] = None
        else:
            rules = [
                {
                    "Value": "Rule_A" if mode == "ok" else "Other",
                    "Rule": {
                        "Dimensions": {
                            "Key": "LINKED_ACCOUNT",
                            "Values": draw(
                                st.lists(
                                    st.sampled_from(_ACCOUNT_POOL), max_size=3, unique=True
                                )
                            ),
                        }
                    },
                },
                {"Value": "Rule_B", "Rule": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["777777777777"]}}},
            ]
            definitions[arn] = CostCategoryDefinition(arn=arn, name="Cat", rules=rules)
    names = {
        account: draw(st.one_of(st.none(), st.just(f"Name-{account}")))
        for account in _ACCOUNT_POOL
    }
    use_retriever = draw(st.booleans())
    use_resolver = draw(st.booleans())
    return credits, categories, definitions, names, use_retriever, use_resolver


class _MapRetriever:
    def __init__(self, definitions: dict[str, CostCategoryDefinition | None]):
        self._definitions = definitions

    def describe(self, cost_category_arn: str):
        return self._definitions.get(cost_category_arn)


def _expected_has_item(credit: Credit, group, active: bool) -> bool:
    """Independent statement of Property 22's iff condition."""
    if active:
        if bool(credit.costCategoryArn) != bool(credit.ruleName):  # (a)
            return True
        if group.sharing_option == COST_CATEGORY_BASED:
            if UNRESOLVED_NAME in (group.cost_category_name, group.rule_name):  # (b)
                return True
            if group.member_accounts_unavailable:  # (b), (c)
                return True
            if any(not m.account_name for m in group.member_accounts):  # (c)
                return True
    return (
        credit.remainingAmount is None
        or credit.endDate is None
        or not credit.creditStatus
    )  # (d)


@settings(max_examples=200, deadline=None)
@given(_p22_scenario())
def test_property_22_attention_items_cover_exactly_the_unresolved_credits(scenario):
    credits, categories, definitions, names, use_retriever, use_resolver = scenario
    classifier = SharingClassifier(
        _MapRetriever(definitions) if use_retriever else None,
        StubResolver(names) if use_resolver else None,
    )
    lifecycle = LifecycleAnalyzer(AS_OF).partition(credits)
    sharing = classifier.classify(credits, categories)
    groups_before = [
        (g.sharing_option, g.cost_category_name, g.rule_name, g.member_accounts,
         g.member_accounts_unavailable)
        for g in sharing.groups
    ]

    items = build_attention_items(credits, lifecycle, sharing)

    # One item per creditId, ordered by creditId.
    ids = [item.credit_id for item in items]
    assert ids == sorted(set(ids))

    # A credit has an item iff Property 22's condition holds.
    active_identities = {id(c) for c in lifecycle.active}
    expected_ids = {
        credit.creditId
        for credit, group in zip(credits, sharing.groups)
        if _expected_has_item(credit, group, id(credit) in active_identities)
    }
    assert set(ids) == expected_ids

    # Every item carries at least one non-empty reason, and a missing
    # exhaustDate is never the reason.
    for item in items:
        assert item.reasons
        assert all(reason.strip() for reason in item.reasons)
        assert not any("exhaust" in reason.lower() for reason in item.reasons)

    # Nothing is guessed: the sharing groups are what they were before.
    assert [
        (g.sharing_option, g.cost_category_name, g.rule_name, g.member_accounts,
         g.member_accounts_unavailable)
        for g in sharing.groups
    ] == groups_before
