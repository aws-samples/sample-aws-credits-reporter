"""Attention_Builder: collect what could not be categorized or resolved.

``build_attention_items(credits, lifecycle, sharing)`` is a pure function that
returns one ``AttentionItem`` per affected creditId. The email body renders the
items as the "NEEDS ATTENTION" block. Nothing is guessed: building items never
changes a credit's classification (a partial binding stays "Shared by default",
a credit missing a field is not given a value for it).

A credit gets an item when at least one of these holds:

* sharing conditions, evaluated over Active_Credits only:

  a. partial binding: exactly one of ``costCategoryArn`` / ``ruleName`` is
     non-empty;
  b. cost-category-based and the category Name is ``unresolved``, the rule is
     ``unresolved``, or the definition could not be retrieved;
  c. cost-category-based and its member accounts are unavailable, or any member
     account name is unresolved (Organizations or Cost Explorer denied or
     unavailable);

* d. the credit is missing ``remainingAmount``, ``endDate``, or
  ``creditStatus``, evaluated over every credit.

A missing ``exhaustDate`` never produces an item: it is normal for a
credit that is not exhausted.

Reasons are plain-language, one string per reason, and appear in a fixed order:
partial binding, unresolved name / rule / definition, unavailable members or
names, missing classification fields. A credit with several reasons yields a
single item; credits that share a creditId are merged into one item.
The module performs no I/O and never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lifecycle_analyzer import LifecycleResult
from .models import Credit
from .sharing_classifier import (
    COST_CATEGORY_BASED,
    UNRESOLVED_NAME,
    SharingGroup,
    SharingResult,
)

# Classification fields whose absence is reported, with the
# plain-language label used in the reason. ``exhaustDate`` is deliberately not
# listed.
CLASSIFICATION_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("remainingAmount", "remaining amount"),
    ("endDate", "end date"),
    ("creditStatus", "credit status"),
)

# Reason texts. Exported so the tests can match on them without duplicating
# the wording. None contains a newline or a semicolon, because the notifier
# joins the reasons of one credit with "; " on a single bullet line.
REASON_PARTIAL_BINDING = (
    "only one of cost category and rule is set, so the credit is treated as "
    "Shared by default"
)
REASON_NAME_UNRESOLVED = "cost category name could not be resolved"
REASON_RULE_UNRESOLVED = (
    "rule could not be found in the cost category definition, so member "
    "accounts are empty"
)
REASON_DEFINITION_UNAVAILABLE = (
    "cost category definition could not be retrieved, so member accounts are "
    "unavailable (Cost Explorer access denied or unavailable)"
)
REASON_NAMES_UNRESOLVED_PREFIX = "member account names could not be resolved for"
REASON_NAMES_UNRESOLVED_SUFFIX = "(AWS Organizations access denied or unavailable)"
REASON_MISSING_FIELDS_SUFFIX = "missing, so the credit cannot be classified"


@dataclass
class AttentionItem:
    """One credit that needs attention, with every reason.

    ``credit_id`` is the creditId; each affected creditId appears in exactly
    one item. A credit that has no creditId is named by its position, for
    example ``<credit #3 without creditId>``. ``reasons`` is non-empty and in
    the fixed order described in the module docstring; it never includes a
    missing ``exhaustDate``.
    """

    credit_id: str
    reasons: list[str] = field(default_factory=list)


def _is_missing(credit: Credit, name: str) -> bool:
    """True when ``name`` is absent on ``credit`` (recorded or empty)."""
    if name in credit.missing_fields:
        return True
    value = getattr(credit, name, None)
    return value is None or value == ""


def _missing_field_reason(credit: Credit) -> str | None:
    labels = [
        label for name, label in CLASSIFICATION_FIELD_LABELS if _is_missing(credit, name)
    ]
    if not labels:
        return None
    return f"{', '.join(labels)} {REASON_MISSING_FIELDS_SUFFIX}"


def _partial_binding(credit: Credit) -> bool:
    """Exactly one of ``costCategoryArn`` and ``ruleName`` is non-empty."""
    return bool(credit.costCategoryArn) != bool(credit.ruleName)


def _sharing_reasons(credit: Credit, group: SharingGroup | None) -> list[str]:
    """Reasons for conditions (a) to (c), in the fixed order (Active_Credits only)."""
    reasons: list[str] = []
    if _partial_binding(credit):
        reasons.append(REASON_PARTIAL_BINDING)

    if group is None or group.sharing_option != COST_CATEGORY_BASED:
        return reasons

    if group.cost_category_name in (None, "", UNRESOLVED_NAME):
        reasons.append(REASON_NAME_UNRESOLVED)
    if group.member_accounts_unavailable:
        # The definition was never available, so the rule was not checked.
        reasons.append(REASON_DEFINITION_UNAVAILABLE)
    elif group.rule_name in (None, "", UNRESOLVED_NAME):
        reasons.append(REASON_RULE_UNRESOLVED)

    unnamed = [m.account_id for m in group.member_accounts if not m.account_name]
    if unnamed:
        reasons.append(
            f"{REASON_NAMES_UNRESOLVED_PREFIX} {', '.join(unnamed)} "
            f"{REASON_NAMES_UNRESOLVED_SUFFIX}"
        )
    return reasons


def build_attention_items(
    credits: list[Credit], lifecycle: LifecycleResult, sharing: SharingResult
) -> list[AttentionItem]:
    """Return one ``AttentionItem`` per affected creditId, ordered by creditId.

    ``lifecycle`` supplies the Active_Credits to which the sharing conditions
    are limited; ``sharing`` supplies each credit's ``SharingGroup`` (matched
    by object identity, since the same credit instances flow through every
    analysis step). The classification of the listed credits is unchanged.
    Never raises.
    """
    active_identities = {id(credit) for credit in lifecycle.active}
    groups = {id(group.credit): group for group in sharing.groups}

    reasons_by_id: dict[str, list[str]] = {}
    for index, credit in enumerate(credits, start=1):
        reasons: list[str] = []
        if id(credit) in active_identities:
            reasons.extend(_sharing_reasons(credit, groups.get(id(credit))))
        missing = _missing_field_reason(credit)
        if missing is not None:
            reasons.append(missing)
        if not reasons:
            continue

        credit_id = credit.creditId or f"<credit #{index} without creditId>"
        merged = reasons_by_id.setdefault(credit_id, [])
        for reason in reasons:
            if reason not in merged:
                merged.append(reason)

    return [
        AttentionItem(credit_id=credit_id, reasons=reasons_by_id[credit_id])
        for credit_id in sorted(reasons_by_id)
    ]
