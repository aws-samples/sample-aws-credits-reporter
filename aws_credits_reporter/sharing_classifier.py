"""Sharing_Classifier: classify how each credit is shared.

Each credit is assigned to exactly one of two sharing options:

* ``cost-category-based`` - the credit has no ``shareableAccounts`` AND carries
  BOTH a non-empty ``costCategoryArn`` AND a non-empty ``ruleName``.
  Live payloads showed ``get-credits`` exposes both fields for a
  cost-category-shared credit, so the binding is authoritative, never inferred.
  The Bound_Cost_Category Name is resolved by matching the ARN against the
  retrieved ``CostCategory`` references; the Bound_Rule and its
  member accounts are resolved by describing the category definition and
  selecting the rule whose ``Value == ruleName``.
* ``Shared by default``   - every other credit,
  including a credit with non-empty ``shareableAccounts`` (which takes
  precedence over the cost-category pair) and a credit that carries only one of
  ``costCategoryArn`` / ``ruleName`` (a partial binding, which the attention
  builder reports; nothing is guessed).

A third option sits above both: ``Sharing disabled`` is assigned to every credit
whose ``creditSharingType`` is ``DISABLED`` (credit sharing is off for the
owning account), whatever its binding. A disabled credit that has a full cost
category binding still carries the binding details (category, rule, members), so
the report can show them.

There is no fallback or heuristic path: nothing is inferred.

The classifier never raises. The cost-category path depends on two injected
AWS adapters, a ``CategoryDefinitionRetriever`` and an ``AccountNameResolver``,
but both are optional: when either is absent the bound credit is still
classified cost-category-based (Name where resolvable), degrading the member
accounts to unavailable rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from .models import CostCategory, Credit, MemberAccount, sanitize_text

# The sharing options a credit can be assigned to.
COST_CATEGORY_BASED = "cost-category-based"
SHARED_BY_DEFAULT = "Shared by default"
SHARING_DISABLED = "Sharing disabled"

# ``creditSharingType`` value meaning credit sharing is off for the owning account.
SHARING_TYPE_DISABLED = "DISABLED"

# Marker used when no definition matches a bound ARN or the match carries no
# Name, or when no rule matches the credit's ``ruleName``.
UNRESOLVED_NAME = "unresolved"

# Exact member names from the ``ce`` cost-category rule / dimension shape.
# A rule looks like ``{"Value": <rule name>, "Rule": {"Dimensions":
# {"Key": "LINKED_ACCOUNT", "Values": [...]}}}``.
RULE_VALUE_KEY = "Value"
RULE_BODY_KEY = "Rule"
RULE_DIMENSIONS_KEY = "Dimensions"
DIMENSION_KEY_KEY = "Key"
DIMENSION_VALUES_KEY = "Values"
LINKED_ACCOUNT_DIMENSION = "LINKED_ACCOUNT"


class _Retriever(Protocol):
    """The subset of ``CategoryDefinitionRetriever`` the classifier needs."""

    def describe(self, cost_category_arn: str): ...


class _Resolver(Protocol):
    """The subset of ``AccountNameResolver`` the classifier needs."""

    def resolve(self, account_ids: list[str]): ...


class _Preferences(Protocol):
    """The subset of ``CreditSharingPreferences`` the classifier needs."""

    def is_enabled(self, account_id: str) -> bool | None: ...

    def account_name(self, account_id: str) -> str | None: ...


@dataclass(frozen=True)
class SharingGroup:
    """The sharing classification of a single credit.

    Exactly one ``sharing_option`` is assigned per credit. The binding fields
    are populated for any credit with a full cost category binding, whether it
    is ``cost-category-based`` or ``Sharing disabled``:
    ``cost_category_name`` names the resolved category (or the ``unresolved``
    marker), ``cost_category_arn`` and ``rule_name`` carry the bound binding
    (``rule_name`` is ``unresolved`` when no rule matched or the definition was
    unavailable), ``member_accounts`` lists the rule's member accounts (each
    with its credit sharing state when known), and
    ``member_accounts_unavailable`` is ``True`` when the definition could not
    be retrieved. A ``Shared by default`` group carries the defaults.
    ``owner_account_name`` is the name of the credit's owning account, set for a
    ``Sharing disabled`` group when it could be resolved.
    """

    credit: Credit
    sharing_option: str
    cost_category_name: str | None = None
    cost_category_arn: str | None = None
    rule_name: str | None = None
    member_accounts: list[MemberAccount] = field(default_factory=list)
    member_accounts_unavailable: bool = False
    owner_account_name: str | None = None


@dataclass(frozen=True)
class SharingResult:
    """The outcome of classifying one credit list against the account's categories.

    ``groups`` holds one ``SharingGroup`` per input credit, in input order, so
    ``len(groups) == len(credits)`` always holds. A category
    retrieval failure is not carried here: an unresolved category Name is
    reported through the "NEEDS ATTENTION" block instead.
    """

    groups: list[SharingGroup] = field(default_factory=list)


def _rule_member_account_ids(rule: dict) -> list[str]:
    """Extract LINKED_ACCOUNT member ids from one cost-category rule.

    The rule shape is ``{"Value": <name>, "Rule": {"Dimensions":
    {"Key": "LINKED_ACCOUNT", "Values": [...]}}}``. Any unexpected shape yields
    an empty list rather than raising. Only string values are kept.
    """
    body = rule.get(RULE_BODY_KEY)
    if not isinstance(body, dict):
        return []
    dimensions = body.get(RULE_DIMENSIONS_KEY)
    if not isinstance(dimensions, dict):
        return []
    if dimensions.get(DIMENSION_KEY_KEY) != LINKED_ACCOUNT_DIMENSION:
        return []
    values = dimensions.get(DIMENSION_VALUES_KEY)
    if not isinstance(values, list):
        return []
    return [sanitize_text(v) for v in values if isinstance(v, str)]


class SharingClassifier:
    """Classifies each credit into exactly one sharing option.

    The two AWS adapters are injected via the constructor so tests can pass
    stubs; both default to ``None``, in which case the cost-category path
    degrades gracefully (member accounts unavailable) while still classifying
    the credit cost-category-based with the Name resolved from the retrieved
    references. Each distinct bound ARN is described at most once per
    ``classify`` call by the injected retriever's own memoization.
    """

    def __init__(
        self,
        definition_retriever: _Retriever | None = None,
        account_name_resolver: _Resolver | None = None,
        sharing_preferences: _Preferences | None = None,
    ):
        self._retriever = definition_retriever
        self._resolver = account_name_resolver
        self._preferences = sharing_preferences

    def classify(
        self, credits: list[Credit], categories: list[CostCategory]
    ) -> SharingResult:
        """Assign every credit exactly one ``SharingGroup``.

        The sharing option is chosen first; first match wins:

        1. ``creditSharingType`` is ``DISABLED`` -> ``Sharing disabled``.
        2. ``shareableAccounts`` non-empty -> ``Shared by default``.
        3. empty ``shareableAccounts`` AND non-empty ``costCategoryArn`` AND
           non-empty ``ruleName`` -> ``cost-category-based``: the ARN's Name is
           resolved from the references, and the bound rule and member accounts
           from the described definition.
        4. anything else -> ``Shared by default``, including a
           credit with only one of the two binding fields.

        A ``Sharing disabled`` credit that has a full binding (and no
        ``shareableAccounts``) still gets the binding details resolved, because
        the report shows them. Returns one group per credit in input order.
        Never raises.
        """
        # Map ARN -> resolved Name from the references. Later
        # references win on a duplicate ARN, which is harmless since the API
        # does not duplicate ARNs.
        arn_to_name = {
            cat.CostCategoryArn: cat.Name
            for cat in categories
            if cat.CostCategoryArn is not None
        }

        groups: list[SharingGroup] = []
        for credit in credits:
            has_binding = bool(
                not credit.shareableAccounts
                and credit.costCategoryArn
                and credit.ruleName
            )
            disabled = credit.creditSharingType == SHARING_TYPE_DISABLED

            if has_binding:
                group = self._cost_category_group(credit, arn_to_name)
            else:
                group = SharingGroup(credit=credit, sharing_option=SHARED_BY_DEFAULT)

            if disabled:
                group = replace(
                    group,
                    sharing_option=SHARING_DISABLED,
                    owner_account_name=self._owner_name(credit.accountId),
                )
            groups.append(group)
        return SharingResult(groups=groups)

    def _owner_name(self, account_id: str | None) -> str | None:
        """Best-effort name of the credit's owning account (Organizations first)."""
        if not account_id:
            return None
        if self._resolver is not None:
            resolution = self._resolver.resolve([account_id])
            names = getattr(resolution, "names", None) or {}
            if names.get(account_id):
                return names[account_id]
        if self._preferences is not None:
            return self._preferences.account_name(account_id)
        return None

    def _cost_category_group(
        self, credit: Credit, arn_to_name: dict[str, str | None]
    ) -> SharingGroup:
        """Build the cost-category-based group for a credit with a full binding.

        The credit is cost-category-based regardless of whether the Name, rule,
        definition, or member names resolve: the ARN and rule pair is the
        authoritative signal. Resolution of the Name, the
        bound rule and member accounts, and member account
        names each degrades to ``unresolved`` / empty / unavailable
        without reclassifying the credit.
        """
        arn = credit.costCategoryArn
        name = arn_to_name.get(arn)
        cost_category_name = name if name else UNRESOLVED_NAME

        rule_name = UNRESOLVED_NAME
        member_accounts: list[MemberAccount] = []
        member_accounts_unavailable = False

        definition = self._retriever.describe(arn) if self._retriever is not None else None
        if definition is None:
            # No adapter injected, or describe-cost-category-definition failed
            #: the definition is unavailable.
            member_accounts_unavailable = True
        else:
            rule = self._select_rule(definition.rules, credit.ruleName)
            if rule is not None:
                rule_name = credit.ruleName
                member_accounts = self._resolve_members(
                    _rule_member_account_ids(rule)
                )

        return SharingGroup(
            credit=credit,
            sharing_option=COST_CATEGORY_BASED,
            cost_category_name=cost_category_name,
            cost_category_arn=arn,
            rule_name=rule_name,
            member_accounts=member_accounts,
            member_accounts_unavailable=member_accounts_unavailable,
        )

    @staticmethod
    def _select_rule(rules: list[dict], rule_name: str | None) -> dict | None:
        """Return the rule whose ``Value`` equals ``rule_name``.

        Returns ``None`` when ``rule_name`` is absent or no rule matches, which
        the caller treats as the rule being ``unresolved``.
        """
        if rule_name is None:
            return None
        for rule in rules:
            if isinstance(rule, dict) and rule.get(RULE_VALUE_KEY) == rule_name:
                return rule
        return None

    def _resolve_members(self, account_ids: list[str]) -> list[MemberAccount]:
        """Resolve member account ids to names, in order.

        Each id becomes exactly one ``MemberAccount`` in the rule's declared
        order. When no resolver is injected, or the resolver reports a name
        unavailable, the ``account_name`` is ``None`` and the renderer falls
        back to the bare id. Never raises.
        """
        if not account_ids:
            return []
        names: dict[str, str | None] = {}
        if self._resolver is not None:
            resolution = self._resolver.resolve(account_ids)
            names = getattr(resolution, "names", None) or {}
        return [
            MemberAccount(
                account_id=account_id,
                account_name=names.get(account_id),
                sharing_enabled=(
                    self._preferences.is_enabled(account_id)
                    if self._preferences is not None
                    else None
                ),
            )
            for account_id in account_ids
        ]
