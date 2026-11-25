"""Dashboard summary for the AWS Credits Reporter email body.

- ``build_dashboard(lifecycle, expiration, new_credits, sharing=None)`` folds the
  ``LifecycleResult`` from the Lifecycle_Analyzer, the ``ExpirationResult``
  from the Expiration_Analyzer, and the ``NewCreditResult`` from the
  New_Credit_Detector into a ``Dashboard`` value holding the metrics the
  email body reports at a glance:

  * Active_Credit count and total remaining, plus the Inactive_Credit count
    (Active + Inactive equals the snapshot total);
  * one row per Inactive Lifecycle_Status Expired, Exhausted, Disabled with
    its count and total remaining;
  * Expiring_This_Month and Expiring_Next_Three_Months count and total;
  * newly issued credit count, total remaining, and the sorted creditIds;
  * baseline / comparison-error flags and human-readable notices.

- ``format_money(amount, currency)`` renders a ``Decimal`` with a currency
  symbol and exactly two decimals (ROUND_HALF_UP), falling back to an ISO
  code prefix for currencies without a well-known symbol.

- ``build_dashboard`` optionally accepts the ``SharingResult`` from the
  Sharing_Classifier and folds it into ``Dashboard.sharing_by_option`` (one
  ``TierSummary`` per sharing option, always present). The fold counts only
  Active_Credits; Expired, Exhausted, and Disabled credits never
  contribute to a sharing group count or total.

This module is pure: it performs no I/O and depends only on in-memory data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from aws_credits_reporter.aggregation import TierSummary, sum_remaining, summarize
from aws_credits_reporter.expiration_analyzer import ExpirationResult
from aws_credits_reporter.lifecycle_analyzer import LifecycleResult
from aws_credits_reporter.models import Credit, LifecycleStatus, lifecycle_status
from aws_credits_reporter.new_credit_detector import NewCreditResult
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SHARING_DISABLED,
    SharingResult,
)

# The Inactive Lifecycle_Status values shown in the dashboard breakdown, in
# the fixed display order Expired, Exhausted, Disabled.
INACTIVE_STATUSES: tuple[LifecycleStatus, ...] = (
    LifecycleStatus.EXPIRED,
    LifecycleStatus.EXHAUSTED,
    LifecycleStatus.DISABLED,
)

# The sharing options tracked by the dashboard, in the fixed order
# cost-category-based, "Shared by default", "Sharing disabled".
SHARING_OPTIONS: tuple[str, ...] = (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SHARING_DISABLED,
)

# Empty-state and notice texts.
NO_ACTIVE_CREDITS_MESSAGE = "No active credits were found."
NO_NEW_CREDITS_MESSAGE = "No newly issued credits were found."
BASELINE_NOTICE = (
    "No prior run available for comparison; the current credits were recorded "
    "as the initial baseline."
)
COMPARISON_ERROR_NOTICE = (
    "Comparison error: the most recent prior snapshot could not be retrieved or "
    "read, so new-credit detection was halted."
)

# ISO 4217 code to display symbol. Unknown codes render as ``<CODE> <amount>``.
CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "\u20ac",
    "GBP": "\u00a3",
    "JPY": "\u00a5",
}

_TWO_PLACES = Decimal("0.01")


def format_money(amount: Decimal, currency: str | None) -> str:
    """Format ``amount`` with a currency symbol and two decimals.

    Known currencies use their symbol as a prefix (``$1,234.56`` style without
    thousands separators: ``$1234.56``). Unknown or missing currencies fall
    back to the ISO code as a prefix (``CHF 12.00``); a ``None`` currency
    renders the bare number so a zero total is still meaningful (``0.00``).
    Negative amounts keep the sign after the symbol (``$-5.00``).
    """
    quantized = amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    number = f"{quantized:.2f}"
    if currency is None:
        return number
    symbol = CURRENCY_SYMBOLS.get(currency.upper())
    if symbol is not None:
        return f"{symbol}{number}"
    return f"{currency} {number}"


@dataclass
class Dashboard:
    """The metrics reported in the email body.

    Totals are ``Decimal`` sums of ``remainingAmount``; the paired currency is
    the code of the first contributing amount, or ``None`` when no credit in
    that group carried a remaining amount. ``new_credit_ids`` is sorted so the
    rendering is deterministic. ``notices`` carries the baseline and
    comparison-error texts the dashboard must display.
    """

    active_count: int = 0
    active_total: Decimal = Decimal("0")
    active_currency: str | None = None
    inactive_count: int = 0
    inactive_by_status: dict[LifecycleStatus, TierSummary] = field(
        default_factory=lambda: {status: TierSummary() for status in INACTIVE_STATUSES}
    )
    this_month_count: int = 0
    this_month_total: Decimal = Decimal("0")
    this_month_currency: str | None = None
    next_three_count: int = 0
    next_three_total: Decimal = Decimal("0")
    next_three_currency: str | None = None
    new_count: int = 0
    new_total: Decimal = Decimal("0")
    new_currency: str | None = None
    new_credit_ids: list[str] = field(default_factory=list)
    is_initial_baseline: bool = False
    comparison_error: bool = False
    notices: list[str] = field(default_factory=list)
    # Sharing-option figures over Active_Credits. One
    # ``TierSummary`` per sharing option cost-category-based and "Shared by
    # default", always present (0 / 0.00 when a group has no credits).
    sharing_by_option: dict[str, TierSummary] = field(
        default_factory=lambda: {option: TierSummary() for option in SHARING_OPTIONS}
    )

    # -- convenience accessors used by the email body --------------------------

    @property
    def has_active_credits(self) -> bool:
        return self.active_count > 0

    @property
    def has_new_credits(self) -> bool:
        return self.new_count > 0

    def active_total_display(self) -> str:
        return format_money(self.active_total, self.active_currency)

    def this_month_total_display(self) -> str:
        return format_money(self.this_month_total, self.this_month_currency)

    def next_three_total_display(self) -> str:
        return format_money(self.next_three_total, self.next_three_currency)

    def new_total_display(self) -> str:
        return format_money(self.new_total, self.new_currency)

    def inactive_status_display(self, status: LifecycleStatus) -> tuple[int, str]:
        """The ``(count, formatted total)`` for one Inactive Lifecycle_Status."""
        tier = self.inactive_by_status.get(status, TierSummary())
        return tier.count, format_money(tier.total_remaining, tier.currency)

    def sharing_option_display(self, option: str) -> tuple[int, str]:
        """The ``(count, formatted total)`` for one sharing option."""
        tier = self.sharing_by_option.get(option, TierSummary())
        return tier.count, format_money(tier.total_remaining, tier.currency)


def _inactive_by_status(
    lifecycle: LifecycleResult,
) -> dict[LifecycleStatus, TierSummary]:
    """Group Inactive_Credits by Lifecycle_Status into ``TierSummary`` values.

    Every one of Expired, Exhausted, and Disabled is always present as a key
    (0 / 0.00 when a status has no credits). Each credit's status is derived
    from the credit itself at the Snapshot_Date rather than looked up by
    ``creditId``, so two records that share a ``creditId`` (or a credit with no
    ``creditId``) are never mixed up. The Exhausted total is always zero by
    definition of rule (b).
    """
    grouped: dict[LifecycleStatus, list[Credit]] = {
        status: [] for status in INACTIVE_STATUSES
    }
    for credit in lifecycle.inactive:
        status = lifecycle_status(credit, lifecycle.snapshot_date)
        if status in grouped:
            grouped[status].append(credit)
    return {status: summarize(members) for status, members in grouped.items()}


def _sharing_by_option(
    sharing: SharingResult, lifecycle: LifecycleResult
) -> dict[str, TierSummary]:
    """Group the Active_Credits by sharing option into ``TierSummary`` values.

    Each of cost-category-based and "Shared by default" is always present as a
    key (0 / 0.00 when a group has no credits). The ``SharingResult`` carries
    one ``SharingGroup`` per credit; each group's ``sharing_option`` is exactly
    one of the two options, so grouping by it partitions the credits.

    Only groups whose credit is one of ``lifecycle.active`` (matched by object
    identity, since the same credit instances flow through every analysis step)
    contribute. Expired, Exhausted, and Disabled credits never contribute to any
    sharing group count or total, so the sharing totals reflect only credits
    that can still be consumed and always reconcile to the Active_Credit count.
    """
    active_identities = {id(credit) for credit in lifecycle.active}
    grouped: dict[str, list[Credit]] = {option: [] for option in SHARING_OPTIONS}
    for group in sharing.groups:
        if group.sharing_option in grouped and id(group.credit) in active_identities:
            grouped[group.sharing_option].append(group.credit)
    return {option: summarize(members) for option, members in grouped.items()}


def build_dashboard(
    lifecycle: LifecycleResult,
    expiration: ExpirationResult,
    new_credits: NewCreditResult,
    sharing: SharingResult | None = None,
) -> Dashboard:
    """Compute the dashboard metrics from the analysis results.

    ``lifecycle`` is the ``LifecycleResult`` produced by the Lifecycle_Analyzer
    at the Snapshot_Date; the dashboard reads the Active / Inactive partitions
    and the per-credit statuses from ``lifecycle.status_by_id`` instead of
    re-deriving them. Expiration tiers are taken verbatim
    from ``expiration`` and the new-credit figures from
    ``new_credits``. ``inactive_by_status`` carries one
    ``TierSummary`` per Inactive Lifecycle_Status Expired, Exhausted, Disabled,
    always present. When the detector reported an initial baseline or
    a comparison error, the matching notice is added and the new-credit figures
    stay at zero.

    ``sharing`` is the ``SharingResult`` produced by the Sharing_Classifier for
    the same credits. When provided, ``sharing_by_option`` carries one
    ``TierSummary`` per sharing option cost-category-based and "Shared by
    default", always present, computed over Active_Credits only so that no
    Inactive_Credit contributes to a sharing group count or total.
    When omitted, the groups stay empty, keeping the dashboard
    backward-compatible.
    """
    active = lifecycle.active
    inactive_count = len(lifecycle.inactive)
    active_total, active_currency = sum_remaining(active)
    inactive_by_status = _inactive_by_status(lifecycle)

    this_tier = expiration.this_month
    next_tier = expiration.next_three_months

    notices: list[str] = []
    if new_credits.comparison_error:
        notices.append(COMPARISON_ERROR_NOTICE)
    if new_credits.is_initial_baseline:
        notices.append(BASELINE_NOTICE)

    new_ids = sorted(new_credits.new_credit_ids)

    if sharing is not None:
        sharing_by_option = _sharing_by_option(sharing, lifecycle)
    else:
        sharing_by_option = {option: TierSummary() for option in SHARING_OPTIONS}

    return Dashboard(
        active_count=len(active),
        active_total=active_total,
        active_currency=active_currency,
        inactive_count=inactive_count,
        inactive_by_status=inactive_by_status,
        this_month_count=this_tier.count,
        this_month_total=this_tier.total_remaining,
        this_month_currency=this_tier.currency,
        next_three_count=next_tier.count,
        next_three_total=next_tier.total_remaining,
        next_three_currency=next_tier.currency,
        new_count=len(new_ids),
        new_total=new_credits.total_new_remaining,
        new_currency=new_credits.new_remaining_currency,
        new_credit_ids=new_ids,
        is_initial_baseline=new_credits.is_initial_baseline,
        comparison_error=new_credits.comparison_error,
        notices=notices,
        sharing_by_option=sharing_by_option,
    )
