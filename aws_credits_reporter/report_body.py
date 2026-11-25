"""Report body: render the plain-text email body for AWS Credits Reporter.

``build_dashboard_body(dashboard, expiration, new_credits, inactive, sharing,
attention, snapshot_date)`` renders the body in this order:

1. The title ``AWS CREDITS OVERVIEW FOR PAYER <id> (<name>)``, always present,
   followed by the alert blocks that apply: newly issued credits, paused
   credits, then one expiration alert per non-empty tier, each credit with its
   remaining amount and end date.
2. ``CREDITS DASHBOARD SUMMARY``.
3. ``Credits by sharing option``: cost-category-based, Shared by default, and
   Sharing disabled.
4. ``NEEDS ATTENTION`` (only when credits could not be categorized or resolved).
5. A trailing listing of recently Expired and Exhausted credits, present only
   when there is something to list. Paused credits are shown in the alerts.

This module is pure: it performs no I/O. Delivery lives in ``notifier``.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from aws_credits_reporter.aggregation import TierSummary, count_label, sum_remaining
from aws_credits_reporter.attention_builder import AttentionItem
from aws_credits_reporter.dashboard import (
    INACTIVE_STATUSES,
    NO_ACTIVE_CREDITS_MESSAGE,
    NO_NEW_CREDITS_MESSAGE,
    Dashboard,
    format_money,
)
from aws_credits_reporter.expiration_analyzer import ExpirationResult
from aws_credits_reporter.models import Credit, LifecycleStatus, MemberAccount
from aws_credits_reporter.new_credit_detector import NewCreditResult
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SHARING_DISABLED,
    SHARING_TYPE_DISABLED,
    SharingGroup,
    SharingResult,
)

# --------------------------------------------------------------------------
# Body text
# --------------------------------------------------------------------------


# Dashboard_Summary body. Section headings are exported so the
# tests and the orchestrator can locate the blocks without duplicating text.
OVERVIEW_HEADING = "AWS CREDITS OVERVIEW"
NEW_CREDITS_ALERT_HEADING = "New credits"
PAUSED_CREDITS_ALERT_HEADING = "Paused credits"
EXPIRING_THIS_MONTH_HEADING = "Expiring this month"
EXPIRING_NEXT_THREE_HEADING = "Expiring in the next three months"
DASHBOARD_SUMMARY_HEADING = "CREDITS DASHBOARD SUMMARY"
SHARING_SUMMARY_HEADING = "Credits by sharing option"
INACTIVE_CREDITS_HEADING = "INACTIVE CREDITS"
# Markers in the credit lines about credit sharing being off.
SHARING_OFF_MARKER = "(sharing off)"
SHARING_DEACTIVATED_TEMPLATE = "[credit sharing DEACTIVATED for the owning account {account_id}]"
# Heading of the block listing what the run found but could not categorize or
# resolve. Present only when at least one Attention_Item exists.
NEEDS_ATTENTION_HEADING = "NEEDS ATTENTION"
# Joins the reasons of one credit on its single bullet.
ATTENTION_REASON_SEPARATOR = "; "
# When every listed Inactive_Credit is Expired the trailing listing is titled
# "EXPIRED CREDITS" instead.
EXPIRED_CREDITS_HEADING = "EXPIRED CREDITS IN THE LAST 3 MONTHS"

# The trailing EXPIRED listing and the DASHBOARD SUMMARY "Expired" figure are
# scoped to credits that expired within this many calendar months before the
# Snapshot_Date; older expirations are omitted so the section stays current.
EXPIRED_WINDOW_MONTHS = 3
NOT_AVAILABLE = "not available"

# The email body labels the Disabled Lifecycle_Status group "Paused"
# everywhere it appears. This is a display-only relabelling: the
# LifecycleStatus enum value keeps "Disabled".
# ``get-credits`` cannot distinguish a user-paused credit from a genuinely
# disabled one, so the single label covers both.
PAUSED_LABEL = "Paused"


def _status_label(status: LifecycleStatus) -> str:
    """The email-body display label for ``status`` (Paused for Disabled)."""
    return PAUSED_LABEL if status is LifecycleStatus.DISABLED else status.value


# Marker rendered in a cost-category alert bullet when the member-account
# fetch failed.
MEMBER_ACCOUNTS_UNAVAILABLE = "member accounts unavailable"


def _credit_id_display(credit: Credit) -> str:
    return credit.creditId if credit.creditId is not None else NOT_AVAILABLE


def _remaining_display(credit: Credit) -> str:
    amount = credit.remainingAmount
    if amount is None:
        return NOT_AVAILABLE
    return format_money(amount.amount, amount.currency)


def _end_date_display(credit: Credit) -> str:
    return credit.endDate.isoformat() if credit.endDate is not None else NOT_AVAILABLE


def _member_account_display(member: MemberAccount) -> str:
    """``<name> <id>`` when the name resolved, else the bare id.

    A member whose credit sharing is known to be off gets a trailing
    ``(sharing off)`` marker. An unknown state (``None``) is left unmarked.
    """
    label = f"{member.account_name} {member.account_id}" if member.account_name else member.account_id
    if member.sharing_enabled is False:
        label = f"{label} {SHARING_OFF_MARKER}"
    return label


def _has_binding(group: SharingGroup) -> bool:
    """True when the group carries a cost category binding (any sharing option)."""
    return bool(group.cost_category_arn)


def _cost_category_annotation(group: SharingGroup) -> str | None:
    """The cost-category annotation suffix for a credit line.

    Names the Bound_Cost_Category and its Bound_Rule, with the
    Rule_Member_Accounts (account name where resolved, else the bare id, and a
    ``(sharing off)`` marker where known). When the member-account fetch failed
    the account list is replaced by an explicit "member accounts unavailable"
    marker. Returns ``None`` for a credit without a cost category binding, so no
    annotation is appended. A credit whose sharing is disabled keeps its
    annotation when it is bound.
    """
    if not _has_binding(group):
        return None
    name = group.cost_category_name if group.cost_category_name else NOT_AVAILABLE
    rule = group.rule_name if group.rule_name else NOT_AVAILABLE
    if group.member_accounts_unavailable:
        return f'(cost category "{name}" on rule "{rule}": {MEMBER_ACCOUNTS_UNAVAILABLE})'
    if group.member_accounts:
        accounts = ", ".join(_member_account_display(m) for m in group.member_accounts)
        return f'(cost category "{name}" on rule "{rule}" with accounts: {accounts})'
    return f'(cost category "{name}" on rule "{rule}")'


def format_credit_alert_bullet(credit: Credit, group: SharingGroup | None = None) -> str:
    """``- <creditId>  remaining <amount>  ends <endDate>`` with "not available" fallbacks.

    When ``group`` is a cost-category-based ``SharingGroup`` the bullet carries
    a trailing ``(cost category "<Name>" on rule "<Rule>" with accounts: ...)``
    annotation naming who can consume the credit.
    """
    bullet = (
        f"- {_credit_id_display(credit)}  "
        f"remaining {_remaining_display(credit)}  "
        f"ends {_end_date_display(credit)}"
    )
    annotation = _cost_category_annotation(group) if group is not None else None
    if annotation is not None:
        bullet = f"{bullet}  {annotation}"
    return bullet


def _sharing_lookup(sharing: SharingResult | None) -> dict[int, SharingGroup]:
    """Map a credit's identity (``id()``) to its ``SharingGroup``.

    Keyed by object identity because ``Credit`` is unhashable and the same
    credit instances flow from the retriever through every analysis step, so
    the alert tiers and the ``SharingResult`` hold the same objects.
    """
    if sharing is None:
        return {}
    return {id(group.credit): group for group in sharing.groups}


def _tier_alert_lines(
    heading: str, tier: TierSummary, sharing: dict[int, SharingGroup]
) -> list[str]:
    """One expiration alert subsection; empty when the tier has no credits."""
    if not tier.credits:
        return []
    total = format_money(tier.total_remaining, tier.currency)
    lines = [f"{heading} ({count_label(tier.count)}, total remaining {total}):"]
    lines.extend(
        format_credit_alert_bullet(credit, sharing.get(id(credit)))
        for credit in tier.credits
    )
    return lines


def _new_credit_alert_lines(
    new_credits: NewCreditResult, sharing: dict[int, SharingGroup]
) -> list[str]:
    """The new-credit alert listing each newly issued creditId."""
    if not new_credits.new_credits:
        return []
    listed = sorted(new_credits.new_credits, key=lambda credit: credit.creditId or "")
    total = format_money(new_credits.total_new_remaining, new_credits.new_remaining_currency)
    lines = [f"{NEW_CREDITS_ALERT_HEADING} ({count_label(len(listed))}, total remaining {total}):"]
    lines.extend(
        format_credit_alert_bullet(credit, sharing.get(id(credit))) for credit in listed
    )
    return lines


def _paused_alert_lines(
    paused: list[Credit], sharing: dict[int, SharingGroup]
) -> list[str]:
    """The paused-credits alert; empty when no credit is paused.

    One bullet per paused credit, ordered by end date. A bound credit also
    carries its cost category annotation (members whose sharing is off are
    marked), and a credit whose sharing type is ``DISABLED`` ends with a
    ``[credit sharing DEACTIVATED for the owning account <id>]`` note.
    """
    if not paused:
        return []
    ordered = sorted(paused, key=_inactive_sort_key)
    total, currency = sum_remaining(ordered)
    lines = [
        f"{PAUSED_CREDITS_ALERT_HEADING} ({count_label(len(ordered))}, "
        f"total remaining {format_money(total, currency)}):"
    ]
    for credit in ordered:
        bullet = format_inactive_credit_line(credit, LifecycleStatus.DISABLED)
        group = sharing.get(id(credit))
        annotation = _cost_category_annotation(group) if group is not None else None
        if annotation is not None:
            bullet = f"{bullet}  {annotation}"
        if credit.creditSharingType == SHARING_TYPE_DISABLED:
            account = credit.accountId if credit.accountId else NOT_AVAILABLE
            bullet = f"{bullet}  {SHARING_DEACTIVATED_TEMPLATE.format(account_id=account)}"
        lines.append(bullet)
    return lines


def _alert_lines(
    expiration: ExpirationResult,
    new_credits: NewCreditResult,
    paused: list[Credit],
    sharing: dict[int, SharingGroup],
) -> list[str]:
    """The alert blocks (new, paused, expiring), each followed by a blank line."""
    sections = [
        _new_credit_alert_lines(new_credits, sharing),
        _paused_alert_lines(paused, sharing),
        _tier_alert_lines(EXPIRING_THIS_MONTH_HEADING, expiration.this_month, sharing),
        _tier_alert_lines(EXPIRING_NEXT_THREE_HEADING, expiration.next_three_months, sharing),
    ]
    lines: list[str] = []
    for section in (s for s in sections if s):
        lines.extend(section)
        lines.append("")
    return lines


def overview_heading(
    payer_account_id: str | None = None, payer_account_name: str | None = None
) -> str:
    """The title line: ``AWS CREDITS OVERVIEW FOR PAYER <id> (<name>)``.

    The name is omitted when it could not be resolved, and the whole
    ``FOR PAYER`` part when the payer account id is unknown.
    """
    if not payer_account_id:
        return OVERVIEW_HEADING
    heading = f"{OVERVIEW_HEADING} FOR PAYER {payer_account_id}"
    if payer_account_name:
        heading = f"{heading} ({payer_account_name})"
    return heading


def _cost_category_sub_bullets(
    groups: list[SharingGroup],
) -> list[str]:
    """One sub-bullet per distinct (Name, Rule) among active cost-category groups.

    Groups sharing the same resolved category Name and rule are aggregated
    into a single sub-bullet reporting their combined count and remaining
    total.
    """
    order: list[tuple[str, str]] = []
    members: dict[tuple[str, str], list[SharingGroup]] = {}
    for group in groups:
        name = group.cost_category_name if group.cost_category_name else NOT_AVAILABLE
        rule = group.rule_name if group.rule_name else NOT_AVAILABLE
        key = (name, rule)
        if key not in members:
            members[key] = []
            order.append(key)
        members[key].append(group)

    lines = []
    for name, rule in order:
        member_groups = members[(name, rule)]
        total, currency = sum_remaining([g.credit for g in member_groups])
        lines.append(
            f"   - {name} (rule {rule}) - {count_label(len(member_groups))}, "
            f"total remaining {format_money(total, currency)}"
        )
        # The rule's member accounts, with a marker on those whose credit
        # sharing is off. Groups of one (Name, rule) share the same members.
        first = member_groups[0]
        if first.member_accounts_unavailable:
            lines.append(f"       accounts: {MEMBER_ACCOUNTS_UNAVAILABLE}")
        elif first.member_accounts:
            accounts = ", ".join(_member_account_display(m) for m in first.member_accounts)
            lines.append(f"       accounts: {accounts}")
    return lines


def _disabled_sharing_sub_bullets(groups: list[SharingGroup]) -> list[str]:
    """One sub-bullet per credit whose sharing is disabled, naming its account."""
    lines = []
    for group in groups:
        credit = group.credit
        account_id = credit.accountId if credit.accountId else NOT_AVAILABLE
        if group.owner_account_name:
            account = f"{group.owner_account_name} {account_id}"
        else:
            account = account_id
        lines.append(
            f"   - {_credit_id_display(credit)}: sharing is off for its account {account}"
        )
    return lines


def _sharing_lines(
    dashboard: Dashboard, sharing: SharingResult | None, inactive: list[Credit]
) -> list[str]:
    """The "Credits by sharing option" block for the email body.

    Shows three rows, cost-category-based, "Shared by default" and "Sharing
    disabled", which together reconcile to the Active_Credit total. The
    cost-category-based row expands into one sub-bullet per distinct
    (category Name, rule) among active cost-category credits, followed by the
    rule's member accounts (a member whose credit sharing is off is marked
    ``(sharing off)``). The "Sharing disabled" row lists each such credit with
    its owning account.
    Figures are Active_Credits-only: a group's credit is Active when it is not a
    member of ``inactive`` (identity-based, matching ``dashboard.py``'s own fold).

    When ``sharing`` is ``None`` (no ``SharingResult`` available), degrades to
    the aggregate figures already in ``dashboard.sharing_by_option`` with no
    sub-bullets, preserving backward compatibility
    for callers that omit ``sharing``.
    """
    lines = [SHARING_SUMMARY_HEADING]

    if sharing is None:
        cc_count, _ = dashboard.sharing_option_display(COST_CATEGORY_BASED)
        if cc_count:
            lines.append(f"- {COST_CATEGORY_BASED}:")
        else:
            lines.append(f"- {COST_CATEGORY_BASED}: {count_label(0)}, total remaining 0.00")
        for option in (SHARED_BY_DEFAULT, SHARING_DISABLED):
            tier = dashboard.sharing_by_option.get(option, TierSummary())
            lines.append(
                f"- {option}: {count_label(tier.count)}, "
                f"total remaining {format_money(tier.total_remaining, tier.currency)}"
            )
        return lines

    inactive_identities = {id(c) for c in inactive}
    active_groups = [g for g in sharing.groups if id(g.credit) not in inactive_identities]

    cost_category_based = [g for g in active_groups if g.sharing_option == COST_CATEGORY_BASED]
    shared_by_default = [g for g in active_groups if g.sharing_option == SHARED_BY_DEFAULT]
    sharing_disabled = [g for g in active_groups if g.sharing_option == SHARING_DISABLED]

    if cost_category_based:
        lines.append(f"- {COST_CATEGORY_BASED}:")
        lines.extend(_cost_category_sub_bullets(cost_category_based))
    else:
        lines.append(f"- {COST_CATEGORY_BASED}: {count_label(0)}, total remaining 0.00")

    default_total, default_currency = sum_remaining(
        [g.credit for g in shared_by_default]
    )
    lines.append(
        f"- {SHARED_BY_DEFAULT}: {count_label(len(shared_by_default))}, "
        f"total remaining {format_money(default_total, default_currency)}"
    )

    disabled_total, disabled_currency = sum_remaining(
        [g.credit for g in sharing_disabled]
    )
    lines.append(
        f"- {SHARING_DISABLED}: {count_label(len(sharing_disabled))}, "
        f"total remaining {format_money(disabled_total, disabled_currency)}"
    )
    lines.extend(_disabled_sharing_sub_bullets(sharing_disabled))
    return lines


def _dashboard_lines(dashboard: Dashboard, snapshot_date: date | None = None) -> list[str]:
    """The "CREDITS DASHBOARD SUMMARY" block as one continuous bullet list.

    Fixed order: Newly issued credits, Active credits, Paused, Exhausted (last
    3 months), Expiring this month, Expiring in the next three months, Expired
    (last 3 months). There is no overall "Inactive credits" line: it overlapped
    the Paused, Exhausted, and Expired lines. No blank-line sub-grouping between
    the bullets. The Active line carries no inline inactive-count suffix; the
    Disabled group is labelled "Paused".
    """
    lines = [DASHBOARD_SUMMARY_HEADING]

    if dashboard.has_new_credits:
        lines.append(
            f"- Newly issued credits: {count_label(dashboard.new_count)}, "
            f"total remaining {dashboard.new_total_display()}"
        )
    else:
        lines.append(f"- Newly issued credits: {NO_NEW_CREDITS_MESSAGE}")

    if dashboard.has_active_credits:
        lines.append(
            f"- Active credits: {count_label(dashboard.active_count)}, "
            f"total remaining {dashboard.active_total_display()}"
        )
    else:
        lines.append(f"- Active credits: {NO_ACTIVE_CREDITS_MESSAGE}")

    paused_count, paused_total_display = dashboard.inactive_status_display(
        LifecycleStatus.DISABLED
    )
    lines.append(
        f"- {_status_label(LifecycleStatus.DISABLED)}: {count_label(paused_count)}, "
        f"total remaining {paused_total_display}"
    )

    # Exhausted is limited to the last EXPIRED_WINDOW_MONTHS months; its total is
    # the amount fully used (initialAmount minus remainingAmount).
    exhausted_tier = dashboard.inactive_by_status.get(LifecycleStatus.EXHAUSTED)
    exhausted_members = list(exhausted_tier.credits) if exhausted_tier is not None else []
    windowed_exhausted = _exhausted_within_window(exhausted_members, snapshot_date)
    exhausted_count, exhausted_used_display = _count_and_used_display(windowed_exhausted)
    lines.append(
        f"- {_status_label(LifecycleStatus.EXHAUSTED)} in the last {EXPIRED_WINDOW_MONTHS} "
        f"months: {count_label(exhausted_count)}, "
        f"total fully used {exhausted_used_display}"
    )

    lines.append(
        f"- {EXPIRING_THIS_MONTH_HEADING}: {count_label(dashboard.this_month_count)}, "
        f"total remaining {dashboard.this_month_total_display()}"
    )
    lines.append(
        f"- {EXPIRING_NEXT_THREE_HEADING}: {count_label(dashboard.next_three_count)}, "
        f"total remaining {dashboard.next_three_total_display()}"
    )

    expired_tier = dashboard.inactive_by_status.get(LifecycleStatus.EXPIRED)
    expired_members = list(expired_tier.credits) if expired_tier is not None else []
    windowed_expired = _expired_within_window(expired_members, snapshot_date)
    expired_count, expired_total_display = _count_and_total_display(windowed_expired)
    lines.append(
        f"- {_status_label(LifecycleStatus.EXPIRED)} in the last {EXPIRED_WINDOW_MONTHS} "
        f"months: {count_label(expired_count)}, "
        f"total expired {expired_total_display}"
    )

    for notice in dashboard.notices:
        lines.append(f"Note: {notice}")
    return lines


def _attention_lines(attention: list[AttentionItem] | None) -> list[str]:
    """The "NEEDS ATTENTION" block: one bullet per creditId.

    Each bullet reads ``- <creditId>: <reason>[; <reason>...]`` with every
    reason of that credit in the order the Attention_Builder fixed. Returns an
    empty list when there are no items, so the block is omitted entirely.
    """
    if not attention:
        return []
    lines = [NEEDS_ATTENTION_HEADING]
    lines.extend(
        f"- {item.credit_id}: {ATTENTION_REASON_SEPARATOR.join(item.reasons)}"
        for item in attention
    )
    return lines


def _description_display(credit: Credit) -> str:
    return credit.description if credit.description else NOT_AVAILABLE


def format_inactive_credit_line(credit: Credit, status: LifecycleStatus) -> str:
    """``- <creditId>  <description>  <Lifecycle_Status>  remaining <amount>  ends <endDate>``.

    One bullet per credit. Every field renders as "not available"
    when absent; the Disabled status is labelled "Paused".
    """
    return (
        f"- {_credit_id_display(credit)}  "
        f"{_description_display(credit)}  "
        f"{_status_label(status)}  "
        f"remaining {_remaining_display(credit)}  "
        f"ends {_end_date_display(credit)}"
    )


def format_expired_credit_line(credit: Credit) -> str:
    """``- <endDate> - <creditId> "<description>" - <amount>``.

    The compact one-line form used when the trailing listing is all-Expired:
    end date first, then creditId, the quoted description, and the remaining
    amount. Every field renders as "not available" when absent.
    """
    return (
        f"- {_end_date_display(credit)} - "
        f'{_credit_id_display(credit)} "{_description_display(credit)}" - '
        f"{_remaining_display(credit)}"
    )


def _window_start(snapshot_date: date | None, months: int = EXPIRED_WINDOW_MONTHS) -> date | None:
    """Return the inclusive cutoff ``snapshot_date - months`` (calendar months).

    Credits whose ``endDate`` is on or after this date are inside the window.
    Returns ``None`` when ``snapshot_date`` is ``None`` (no date to anchor to),
    in which case callers do not filter and fall back to showing all expired
    credits, preserving the prior behaviour.
    """
    if snapshot_date is None:
        return None
    month_index = (snapshot_date.month - 1) - months
    year = snapshot_date.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp the day to the target month's length (e.g. Mar 31 - 1mo -> Feb 28/29).
    day = min(snapshot_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _expired_within_window(
    credits: list[Credit], snapshot_date: date | None
) -> list[Credit]:
    """Expired credits whose ``endDate`` falls within the last window.

    A credit with no ``endDate`` cannot be placed in the window and is omitted.
    When ``snapshot_date`` is ``None`` the list is returned unfiltered.
    """
    start = _window_start(snapshot_date)
    if start is None:
        return list(credits)
    return [c for c in credits if c.endDate is not None and c.endDate >= start]


def _count_and_total_display(credits: list[Credit]) -> tuple[int, str]:
    """``(count, formatted total remaining)`` over ``credits``."""
    total, currency = sum_remaining(credits)
    return len(credits), format_money(total, currency)


def _exhausted_within_window(
    credits: list[Credit], snapshot_date: date | None
) -> list[Credit]:
    """Exhausted credits that were exhausted within the last window.

    The exhaustion date is ``exhaustDate`` when present, otherwise ``endDate``
    (a credit with zero remaining and no ``exhaustDate``). A credit with
    neither date cannot be placed in the window and is omitted. When
    ``snapshot_date`` is ``None`` the list is returned unfiltered.
    """
    start = _window_start(snapshot_date)
    if start is None:
        return list(credits)
    selected: list[Credit] = []
    for credit in credits:
        when = credit.exhaustDate if credit.exhaustDate is not None else credit.endDate
        if when is not None and when >= start:
            selected.append(credit)
    return selected


def _count_and_used_display(credits: list[Credit]) -> tuple[int, str]:
    """``(count, formatted total fully used)`` over ``credits``.

    The amount used is ``initialAmount - remainingAmount`` per credit (a missing
    remainingAmount counts as zero because the credit is exhausted). Credits
    without an ``initialAmount`` contribute nothing to the total.
    """
    total = Decimal("0")
    currency: str | None = None
    for c in credits:
        if c.initialAmount is None:
            continue
        remaining = c.remainingAmount.amount if c.remainingAmount is not None else Decimal("0")
        total += c.initialAmount.amount - remaining
        if currency is None:
            currency = c.initialAmount.currency
    return len(credits), format_money(total, currency)


def _inactive_sort_key(credit: Credit) -> tuple[int, date]:
    """Order by endDate ascending with credits lacking an endDate last."""
    if credit.endDate is None:
        return (1, date.max)
    return (0, credit.endDate)


def _inactive_lines(dashboard: Dashboard, inactive: list[Credit], snapshot_date: date | None = None) -> list[str]:
    """The trailing listing of recently Expired and Exhausted credits.

    The heading is "EXPIRED CREDITS" when every listed credit is Expired,
    otherwise "INACTIVE CREDITS". Grouping reuses
    ``dashboard.inactive_by_status``, which the dashboard already populated
    from the run's ``LifecycleResult`` at the true Snapshot_Date, so the block
    matches the dashboard breakdown exactly and no status is re-derived against
    a different date. Each credit is one bullet; within each group
    the bullets are ordered by endDate ascending, with credits lacking an
    endDate last.

    Paused (Disabled) credits are not listed here: they appear in the "Paused
    credits" alert at the top. When there is nothing to list (no inactive
    credits, or only old expirations and paused credits) the block is omitted
    entirely and this returns an empty list.
    """
    if not inactive:
        return []

    grouped: list[tuple[LifecycleStatus, list[Credit]]] = []
    for status in INACTIVE_STATUSES:
        if status is LifecycleStatus.DISABLED:
            continue
        tier = dashboard.inactive_by_status.get(status)
        members = list(tier.credits) if tier is not None else []
        if status is LifecycleStatus.EXPIRED:
            # Scope the Expired listing to credits that expired within the last
            # EXPIRED_WINDOW_MONTHS calendar months.
            members = _expired_within_window(members, snapshot_date)
        if not members:
            continue
        members.sort(key=_inactive_sort_key)
        grouped.append((status, members))

    if not grouped:
        return []

    all_expired = all(status is LifecycleStatus.EXPIRED for status, _ in grouped)
    heading = EXPIRED_CREDITS_HEADING if all_expired else INACTIVE_CREDITS_HEADING

    lines = [heading]
    if all_expired:
        # Compact single-section format: no status
        # sub-heading, each credit as "- <endDate> - <creditId> "<desc>" - <amount>".
        for _status, members in grouped:
            lines.extend(format_expired_credit_line(credit) for credit in members)
        return lines
    for status, members in grouped:
        lines.append(_status_label(status))
        lines.extend(format_inactive_credit_line(credit, status) for credit in members)
    return lines


def build_dashboard_body(
    dashboard: Dashboard,
    expiration: ExpirationResult,
    new_credits: NewCreditResult,
    inactive: list[Credit],
    sharing: SharingResult | None = None,
    attention: list[AttentionItem] | None = None,
    snapshot_date: date | None = None,
    payer_account_id: str | None = None,
    payer_account_name: str | None = None,
) -> str:
    """Render the Dashboard_Summary email body.

    The body opens with the title ``AWS CREDITS OVERVIEW FOR PAYER <id>
    (<name>)`` (see ``overview_heading``; the name and the payer part are
    dropped when unknown). It is always present and is followed by a blank line. The alert blocks follow
    directly under it: the new-credit alert naming every newly issued
    creditId, the paused-credits alert, then one expiration alert per non-empty
    tier listing each affected credit with its remaining amount and endDate.
    Each listed credit is one bullet; for a credit with a cost category
    binding the bullet also names the Bound_Cost_Category and its Bound_Rule,
    with the member accounts (names where resolved, bare ids otherwise, a
    ``(sharing off)`` marker where a member's credit sharing is off, or a
    "member accounts unavailable" marker when the fetch failed), read from
    ``sharing``. With no alerts the blank line after the title is followed by the
    dashboard summary.

    The "CREDITS DASHBOARD SUMMARY" block is one continuous bullet list in a
    fixed order: Newly issued credits, Active credits, Paused, Exhausted,
    Expiring this month, Expiring in the next three months, Expired.
    Exhausted and Expired cover the last 3 months; there is no overall
    Inactive line. The Active line carries no inline inactive-count suffix. The
    Disabled group is labelled "Paused" throughout the body. A blank line
    precedes the "Credits by sharing option" block, which shows three rows,
    cost-category-based, "Shared by default" and "Sharing disabled": the
    cost-category-based row expands into a sub-bullet per distinct category
    and rule with its accounts, the "Sharing disabled" row lists each credit
    and its owning account, and every figure is Active_Credits-only.

    When ``attention`` is non-empty, a "NEEDS ATTENTION" block follows the
    sharing block and precedes the trailing listing, with one bullet per
    creditId naming every reason. With ``None`` or an empty
    list the block is omitted entirely and the body is byte-for-byte what it
    would be without the parameter.

    The trailing listing follows the sharing block (and the attention block
    when present). It lists recently Expired and Exhausted credits (from
    ``inactive``) as bullets grouped by Lifecycle_Status, each ordered by
    endDate ascending with missing endDate last. The heading is
    "EXPIRED CREDITS" when every listed credit is Expired, otherwise
    "INACTIVE CREDITS". Paused credits are not listed here (they are in the
    alerts), and when there is nothing to list the block is omitted.

    ``sharing`` is the run's ``SharingResult`` (optional, default ``None``);
    when provided it drives both the per-credit cost-category annotation and
    the per-category sharing sub-bullets. When omitted, credit lines carry no
    cost-category annotation and the sharing block degrades to the aggregate
    figures already on ``dashboard``.

    The body is built once and returned as a single string, which
    ``Notifier.send`` publishes unchanged.
    """
    sharing_lookup = _sharing_lookup(sharing)
    paused_tier = dashboard.inactive_by_status.get(LifecycleStatus.DISABLED)
    paused = list(paused_tier.credits) if paused_tier is not None else []
    # The title is always followed by a blank line, whether or not alerts follow.
    lines = [overview_heading(payer_account_id, payer_account_name), ""]
    lines.extend(_alert_lines(expiration, new_credits, paused, sharing_lookup))
    lines.extend(_dashboard_lines(dashboard, snapshot_date))
    lines.append("")
    lines.extend(_sharing_lines(dashboard, sharing, inactive))
    lines.append("")
    attention_lines = _attention_lines(attention)
    if attention_lines:
        lines.extend(attention_lines)
        lines.append("")
    lines.extend(_inactive_lines(dashboard, inactive, snapshot_date))
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"
