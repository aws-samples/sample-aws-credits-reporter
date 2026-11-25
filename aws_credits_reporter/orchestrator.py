"""Orchestrator: sequences a single AWS Credits Reporter run.

Steps:

1. Retrieve all organization credits via ``CreditRetriever.retrieve()`` and
   summarize them (totals, per-credit missing-field notes).
2. Retrieve the cost category definitions (``CategoryRetriever``, optional;
   a failure is recorded and the run continues with none).
3. Build a ``Snapshot`` from the unmodified raw pages (credits and cost
   categories) and write it via ``SnapshotStore.write()``. A write failure is
   fatal.
4. Partition the credits into Active and Inactive at the snapshot date
   (``LifecycleAnalyzer``).
5. Classify how each credit is shared (``SharingClassifier``, with the optional
   definition retriever and account-name resolver) and collect the credits
   that need attention (``build_attention_items``).
6. Read the most recent prior snapshot with
   ``SnapshotStore.read_latest_prior(before_key=<current key>)``. Any
   exception raised by that read is captured as ``prior_read_ok=False`` and
   recorded in ``messages``; a ``None`` result with no error is the
   initial-baseline path. Then run ``NewCreditDetector.detect(current, prior,
   prior_read_ok)``.
7. Classify expiration tiers over the Active credits only
   (``ExpirationAnalyzer(now).classify``).
8. Build the ``Dashboard`` from the lifecycle partition, the expiration tiers,
   the new-credit result, and the sharing classification.
9. When a ``notifier`` was injected, build the report body
   (``report_body.build_dashboard_body``) and call ``notifier.send(body)``. The
   ``NotifyResult`` is stored in ``RunResult.notify_result``, its messages
   (configuration error, delivery failure, dry run) are appended to
   ``RunResult.messages``, and ``RunResult.delivery_failed`` mirrors the
   delivery-failure indication. Notification problems never fail the run: the
   snapshot already exists, so ``success`` stays True and the CLI exits 0 while
   printing the recorded indication. With ``notifier=None`` the step is
   skipped.

Error policy
------------
Three fatal error types are captured into a
``RunResult`` with ``success=False`` and the recorded message, so that the
CLI can print it to stderr and exit non-zero:

- ``AuthorizationError``,
- ``RetrievalError``,
- ``SnapshotWriteError``.

Any other exception type is *not* swallowed: it propagates to the caller
unchanged. Those are programming errors or environment problems, and hiding them behind a generic "run failed"
message would make them harder to diagnose. The one deliberate exception is
the prior-snapshot read (step 4): *every* failure there is a comparison
error by definition, so it is captured regardless of type.

The orchestrator owns no AWS clients. Fully constructed components are
injected so tests can pass in-memory stubs; building real boto3 clients is
``wiring.build_orchestrator``'s job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Protocol

from aws_credits_reporter.aggregation import TierSummary, count_label
from aws_credits_reporter.attention_builder import AttentionItem, build_attention_items
from aws_credits_reporter.category_retriever import CategoryRetrievalOutcome
from aws_credits_reporter.credit_retriever import RetrievalError, RetrievalOutcome
from aws_credits_reporter.dashboard import Dashboard, build_dashboard, format_money
from aws_credits_reporter.expiration_analyzer import ExpirationAnalyzer, ExpirationResult
from aws_credits_reporter.lifecycle_analyzer import LifecycleAnalyzer
from aws_credits_reporter.models import (
    SNAPSHOT_SOURCE_MANUAL,
    Credit,
    Snapshot,
    parse_cost_categories,
)
from aws_credits_reporter.new_credit_detector import NewCreditDetector, NewCreditResult
from aws_credits_reporter.notifier import NotifyResult
from aws_credits_reporter.report_body import build_dashboard_body
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    UNRESOLVED_NAME,
    SharingClassifier,
    SharingResult,
)
from aws_credits_reporter.snapshot_store import SnapshotWriteError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _CreditRetrieverLike(Protocol):
    def retrieve(self) -> RetrievalOutcome: ...


class _CategoryRetrieverLike(Protocol):
    def retrieve(self) -> CategoryRetrievalOutcome: ...


class _SnapshotStoreLike(Protocol):
    def write(self, snapshot: Snapshot) -> str: ...

    def read_latest_prior(self, before_key: str | None = None) -> Snapshot | None: ...


class _NotifierLike(Protocol):
    def send(self, body: str) -> NotifyResult: ...


class _DetectorLike(Protocol):
    def detect(
        self, current: Snapshot, prior: Snapshot | None, prior_read_ok: bool = True
    ) -> NewCreditResult: ...


class _AnalyzerLike(Protocol):
    def classify(self, credits: list[Credit]) -> ExpirationResult: ...


class _DefinitionRetrieverLike(Protocol):
    # The Sharing_Classifier calls ``describe``; the orchestrator reads the
    # accumulated ``messages`` after classification to surface definition
    # fetch failures.
    messages: list[str]

    def describe(self, cost_category_arn: str): ...


class _AccountNameResolverLike(Protocol):
    # The Sharing_Classifier calls ``resolve``; the orchestrator reads
    # ``consulted``/``unavailable`` after classification to surface the single
    # name-resolution-unavailable message.
    consulted: bool
    unavailable: bool

    def resolve(self, account_ids: list[str]): ...


class _SharingPreferencesLike(Protocol):
    # The Sharing_Classifier calls ``is_enabled`` / ``account_name``; the
    # orchestrator reads ``consulted``/``unavailable`` after classification to
    # surface the single preferences-unavailable message.
    consulted: bool
    unavailable: bool

    def is_enabled(self, account_id: str) -> bool | None: ...

    def account_name(self, account_id: str) -> str | None: ...


# Message texts recorded by the orchestrator (exported so tests and the CLI
# can match on them without duplicating the wording).
PRIOR_READ_FAILED_PREFIX = "prior snapshot read failed"
BASELINE_REJECTED_PREFIX = "snapshot not used as baseline"
CATEGORY_RETRIEVAL_FAILED_PREFIX = "cost category retrieval failed"
# Sharing-resolution degrading messages. None
# of these fails the run; they are recorded for the CLI and SNS notice.
DEFINITION_FETCH_FAILED_PREFIX = "cost category definition fetch failed"
RULE_UNRESOLVED_PREFIX = "cost category rule unresolved"
ACCOUNT_NAME_RESOLUTION_UNAVAILABLE_PREFIX = "account name resolution unavailable"
SHARING_PREFERENCES_UNAVAILABLE_PREFIX = "credit sharing preferences unavailable"


@dataclass
class RunResult:
    """Outcome of one ``Orchestrator.run()``.

    ``success`` is False when a fatal error was recorded in ``error_message``.
    On a failed run the fields below hold whatever was produced before the
    failure (for example ``credits`` is populated when retrieval succeeded but
    the snapshot write failed), so the CLI can still show useful context.

    ``total_remaining`` is the ``Decimal`` sum of ``remainingAmount.amount``
    over the credits that carry one. ``remaining_currency`` is the shared
    currency code when every counted amount uses the same currency; it is
    ``None`` when there are no amounts or when currencies are mixed (a
    warning is then appended to ``messages``).

    ``messages`` collects human-readable notes recorded during the run: one
    line per credit with missing fields, the baseline /
    comparison-error / new-credit outcome, the expiration
    tier summaries, plus warnings.

    ``notify_result`` is the ``NotifyResult`` of the notification step, or ``None`` when no notifier was configured or the run failed before
    reaching it. ``delivery_failed`` is the delivery-failure flag copied from it
    so callers can check for a delivery failure without inspecting the
    nested result; it is always False when nothing was attempted.

    Analysis: ``new_credits`` is the detector output, ``expiration`` the
    analyzer output, and ``dashboard`` the metrics reported in the email body.
    ``prior_snapshot_key`` names the prior snapshot that was compared against
    (``None`` on the baseline and comparison-error paths). All of these stay
    ``None`` when the run failed before the analysis step.

    ``sharing`` is the Sharing_Classifier output; it stays ``None`` on a run
    that failed before the analysis step.

    ``payer_account_id`` and ``payer_account_name`` identify the payer account
    for the report title; the name is ``None`` when it could not be resolved.
    """

    success: bool
    error_message: str | None = None
    credits: list[Credit] = field(default_factory=list)
    snapshot_key: str | None = None
    total_count: int = 0
    total_remaining: Decimal = Decimal("0")
    remaining_currency: str | None = None
    messages: list[str] = field(default_factory=list)
    notify_result: NotifyResult | None = None
    delivery_failed: bool = False
    # Analysis results.
    new_credits: NewCreditResult | None = None
    expiration: ExpirationResult | None = None
    dashboard: Dashboard | None = None
    prior_snapshot_key: str | None = None
    # UTC calendar date of the current snapshot (Snapshot_Date);
    # ``None`` on a run that failed before the snapshot write.
    snapshot_date: date | None = None
    # The Inactive_Credits partition from the Lifecycle_Analyzer,
    # passed to ``build_dashboard_body`` for the INACTIVE CREDITS section;
    # empty until the analysis step runs.
    inactive_credits: list[Credit] = field(default_factory=list)
    # The Sharing_Classifier output for the run's credits. It
    # feeds the sharing-option figures in the dashboard and the sharing lines
    # in the email body. ``None`` on a
    # run that failed before the analysis step.
    sharing: SharingResult | None = None
    # The Attention_Items: credits the run found but could not
    # categorize or resolve, one item per creditId. Rendered as the
    # "NEEDS ATTENTION" block of the email body; informational only, never
    # changes ``success`` or the exit status. Empty when nothing
    # needs attention and on a run that failed before the analysis step.
    attention: list[AttentionItem] = field(default_factory=list)
    payer_account_id: str | None = None
    payer_account_name: str | None = None


def summarize_remaining(credits: list[Credit]) -> tuple[Decimal, str | None, list[str]]:
    """Sum ``remainingAmount`` over ``credits``.

    Returns ``(total, currency, warnings)``. Credits without a parsable
    ``remainingAmount`` contribute nothing. ``currency`` is the common code
    when uniform, otherwise ``None`` with a warning explaining the mix.
    """
    total = Decimal("0")
    currencies: list[str] = []
    for credit in credits:
        amount = credit.remainingAmount
        if amount is None:
            continue
        total += amount.amount
        if amount.currency not in currencies:
            currencies.append(amount.currency)

    warnings: list[str] = []
    if not currencies:
        return total, None, warnings
    if len(currencies) == 1:
        return total, currencies[0], warnings
    warnings.append(
        "warning: remaining amounts use mixed currencies "
        f"({', '.join(currencies)}); the total sums raw amounts without conversion"
    )
    return total, None, warnings


def missing_field_messages(credits: list[Credit]) -> list[str]:
    """One message per credit that has fields recorded as missing."""
    messages: list[str] = []
    for index, credit in enumerate(credits):
        if not credit.missing_fields:
            continue
        identity = credit.creditId if credit.creditId else f"<credit #{index + 1} without creditId>"
        messages.append(
            f"credit {identity}: missing fields {', '.join(credit.missing_fields)}"
        )
    return messages


def new_credit_messages(new_credits: NewCreditResult) -> list[str]:
    """The detector's own messages plus a listing line when credits are new."""
    messages = list(new_credits.messages)
    if new_credits.new_credit_ids:
        total = format_money(new_credits.total_new_remaining, new_credits.new_remaining_currency)
        messages.append(
            f"{count_label(len(new_credits.new_credit_ids))} newly issued "
            f"(total remaining {total}): {', '.join(sorted(new_credits.new_credit_ids))}"
        )
    return messages


def _tier_message(label: str, tier: TierSummary) -> str:
    total = format_money(tier.total_remaining, tier.currency)
    return f"{label}: {count_label(tier.count)}, total remaining {total}"


def expiration_messages(expiration: ExpirationResult) -> list[str]:
    """One summary line per expiration tier."""
    return [
        _tier_message("expiring this month", expiration.this_month),
        _tier_message("expiring in the next three months", expiration.next_three_months),
    ]


def sharing_resolution_messages(
    sharing: SharingResult,
    definition_retriever: _DefinitionRetrieverLike | None,
    account_name_resolver: _AccountNameResolverLike | None,
    sharing_preferences: _SharingPreferencesLike | None = None,
) -> list[str]:
    """Degrading notes from the authoritative sharing resolution.

    None of these fails the run; they describe where the authoritative binding
    could only be resolved partially:

    * ``DEFINITION_FETCH_FAILED_PREFIX`` - one line per
      ``Category_Definition_Retriever`` message (a describe-cost-category-
      definition call that failed after its retries, so that ARN's member
      accounts are unavailable). Read from the retriever's own
      ``messages`` list.
    * ``RULE_UNRESOLVED_PREFIX`` - one line naming each authoritatively bound
      credit whose ``ruleName`` matched no rule in the fetched definition,
      derived from the ``SharingResult`` groups.
    * ``ACCOUNT_NAME_RESOLUTION_UNAVAILABLE_PREFIX`` - a single line when the
      ``Account_Name_Resolver`` was consulted and reported the Organizations
      sweep unavailable, so member accounts fell back to bare ids.
    * ``SHARING_PREFERENCES_UNAVAILABLE_PREFIX`` - a single line when the
      credit sharing preferences were consulted and could not be read, so
      member accounts are not marked with their sharing state.

    Best-effort throughout: missing adapters contribute nothing.
    """
    messages: list[str] = []

    if definition_retriever is not None:
        for message in getattr(definition_retriever, "messages", []) or []:
            messages.append(f"{DEFINITION_FETCH_FAILED_PREFIX}: {message}")

    for group in sharing.groups:
        # This note is specifically "the definition was fetched but no rule
        # matched ``ruleName``". When ``member_accounts_unavailable`` is True
        # the definition itself was never available (no adapter, or a failed
        # fetch already reported above), so the rule was not checked and this
        # is not an unmatched-rule degrade; skip it to avoid a misleading double report.
        if (
            group.sharing_option == COST_CATEGORY_BASED
            and group.cost_category_arn is not None
            and group.rule_name == UNRESOLVED_NAME
            and not group.member_accounts_unavailable
        ):
            identity = group.credit.creditId or "<credit without creditId>"
            messages.append(
                f"{RULE_UNRESOLVED_PREFIX}: credit {identity} ruleName matched no rule "
                f"in cost category {group.cost_category_arn}; member accounts empty"
            )

    if account_name_resolver is not None and getattr(
        account_name_resolver, "consulted", False
    ) and getattr(account_name_resolver, "unavailable", False):
        detail = getattr(account_name_resolver, "error_message", None)
        suffix = f": {detail}" if detail else ""
        messages.append(
            f"{ACCOUNT_NAME_RESOLUTION_UNAVAILABLE_PREFIX}{suffix}; "
            f"member accounts shown by bare id"
        )

    if sharing_preferences is not None and getattr(
        sharing_preferences, "consulted", False
    ) and getattr(sharing_preferences, "unavailable", False):
        detail = getattr(sharing_preferences, "error_message", None)
        suffix = f": {detail}" if detail else ""
        messages.append(
            f"{SHARING_PREFERENCES_UNAVAILABLE_PREFIX}{suffix}; "
            f"member accounts are not marked with their credit sharing state"
        )

    return messages


def _snapshot_date(snapshot: Snapshot) -> date:
    """Return the UTC calendar date of ``snapshot.taken_at`` (Snapshot_Date)."""
    taken_at = snapshot.taken_at
    if taken_at.tzinfo is not None and taken_at.utcoffset() is not None:
        taken_at = taken_at.astimezone(timezone.utc)
    return taken_at.date()


class Orchestrator:
    """Runs retrieval, snapshot persistence, analysis, then notification into a ``RunResult``."""

    def __init__(
        self,
        credit_retriever: _CreditRetrieverLike,
        snapshot_store: _SnapshotStoreLike,
        now: Callable[[], datetime] = _utc_now,
        notifier: _NotifierLike | None = None,
        detector: _DetectorLike | None = None,
        analyzer_factory: Callable[[datetime], _AnalyzerLike] = ExpirationAnalyzer,
        category_retriever: _CategoryRetrieverLike | None = None,
        definition_retriever: _DefinitionRetrieverLike | None = None,
        account_name_resolver: _AccountNameResolverLike | None = None,
        sharing_preferences: _SharingPreferencesLike | None = None,
        payer_account_id: str | None = None,
        run_source: str = SNAPSHOT_SOURCE_MANUAL,
    ) -> None:
        # Only a "scheduled" run advances the baseline. The
        # default is "manual" so an ad-hoc run can never consume the new-credit
        # alert of the next scheduled run.
        self._run_source = run_source
        self._retriever = credit_retriever
        self._store = snapshot_store
        self._now = now
        self._notifier = notifier
        self._detector = detector if detector is not None else NewCreditDetector()
        self._analyzer_factory = analyzer_factory
        self._category_retriever = category_retriever
        # Authoritative-sharing adapters. Both are optional:
        # the CLI/lambda wiring builds the real ones; tests pass
        # stubs or leave them None. When None, the Sharing_Classifier is built
        # without them and the authoritative path degrades gracefully (member
        # accounts unavailable) while still classifying ARN-bound credits
        # cost-category-based.
        self._definition_retriever = definition_retriever
        self._account_name_resolver = account_name_resolver
        # Optional credit sharing preferences (billing:GetBillingPreferences),
        # used to mark cost category members whose sharing is off and to name
        # an owning account. Best-effort: when absent or denied the report is
        # unchanged apart from the missing markers.
        self._sharing_preferences = sharing_preferences
        # The payer (management) account the credits belong to, shown in the
        # report title.
        self._payer_account_id = payer_account_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _payer_account_name(self) -> str | None:
        """Best-effort name of the payer account (Organizations, then preferences)."""
        account_id = self._payer_account_id
        if not account_id:
            return None
        if self._account_name_resolver is not None:
            resolution = self._account_name_resolver.resolve([account_id])
            names = getattr(resolution, "names", None) or {}
            if names.get(account_id):
                return names[account_id]
        if self._sharing_preferences is not None:
            return self._sharing_preferences.account_name(account_id)
        return None

    def _read_prior(
        self, current_key: str, result: RunResult
    ) -> tuple[Snapshot | None, bool]:
        """Read the latest prior snapshot; any failure is a comparison error."""
        try:
            prior = self._store.read_latest_prior(before_key=current_key)
        except Exception as error:  # noqa: BLE001 - every prior-read failure is a comparison error, never fatal
            detail = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
            result.messages.append(f"{PRIOR_READ_FAILED_PREFIX}: {detail}")
            return None, False
        # Snapshots the store refused to use as baseline.
        for rejected_key, reason in getattr(self._store, "rejected", None) or []:
            result.messages.append(
                f"{BASELINE_REJECTED_PREFIX}: {rejected_key} ({reason})"
            )
        if prior is not None:
            result.prior_snapshot_key = prior.key
        return prior, True

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> RunResult:
        """Execute one run. Never raises for the fatal errors listed in the module docstring."""
        result = RunResult(success=False)

        # Step 1: retrieve credits.
        try:
            outcome = self._retriever.retrieve()
        except RetrievalError as error:  # includes AuthorizationError
            result.error_message = str(error)
            return result

        result.credits = list(outcome.credits)
        result.total_count = len(result.credits)
        result.messages.extend(missing_field_messages(result.credits))
        total, currency, warnings = summarize_remaining(result.credits)
        result.total_remaining = total
        result.remaining_currency = currency
        result.messages.extend(warnings)

        # Step 1b: retrieve cost category definitions before the snapshot
        # write so their unmodified raw pages ride in the same S3 object
        # Retrieval never raises: a persistent failure
        # returns an empty page set plus an error message, which is recorded
        # here and does not fail the run. When no category retriever is
        # injected the step is skipped and the section stays empty, keeping
        # earlier behavior unchanged.
        raw_cost_categories: list[dict] = []
        if self._category_retriever is not None:
            category_outcome = self._category_retriever.retrieve()
            raw_cost_categories = list(category_outcome.raw_pages)
            if category_outcome.error_message:
                result.messages.append(
                    f"{CATEGORY_RETRIEVAL_FAILED_PREFIX}: {category_outcome.error_message}"
                )

        # Step 2: persist the unmodified raw pages.
        taken_at = self._now()
        snapshot = Snapshot(
            key="",
            taken_at=taken_at,
            raw_get_credits=list(outcome.raw_pages),
            raw_cost_categories=raw_cost_categories,
            source=self._run_source,
        )
        try:
            result.snapshot_key = self._store.write(snapshot)
        except SnapshotWriteError as error:
            result.error_message = str(error)
            return result
        if not snapshot.key:
            snapshot.key = result.snapshot_key

        # Step 3: derive Lifecycle_Status for every credit at the Snapshot_Date,
        # splitting the parsed credits into Active_Credits and Inactive_Credits. This runs before detection and expiration so
        # that only Active_Credits are ever considered for expiration tiers,
        # and the same partition feeds the dashboard and the
        # email body. The Snapshot_Date is the UTC calendar date of the
        # snapshot timestamp.
        snapshot_date = _snapshot_date(snapshot)
        result.snapshot_date = snapshot_date
        lifecycle = LifecycleAnalyzer(snapshot_date).partition(result.credits)
        result.inactive_credits = list(lifecycle.inactive)

        # Step 3b: classify how each credit is shared. The classifier
        # runs over every credit and the cost category definitions retrieved in
        # step 1b (parsed from the same raw pages that ride in the snapshot).
        # When no category retriever was injected, ``raw_cost_categories`` is
        # empty, so the Name of a bound credit stays ``unresolved`` (reported
        # by the attention builder). The result feeds the dashboard
        # sharing figures and the email body sharing lines.
        # The two authoritative-sharing adapters (``definition_retriever``,
        # ``account_name_resolver``) are injected into the classifier so the
        # authoritative path can describe each Bound_Cost_Category once and
        # resolve member account names. When either is
        # None the classifier is built without it and degrades: ARN-bound
        # credits still classify cost-category-based, with member accounts
        # reported unavailable.
        categories = parse_cost_categories(raw_cost_categories)
        sharing = SharingClassifier(
            definition_retriever=self._definition_retriever,
            account_name_resolver=self._account_name_resolver,
            sharing_preferences=self._sharing_preferences,
        ).classify(result.credits, categories)
        result.sharing = sharing

        # The report title names the payer account.
        result.payer_account_id = self._payer_account_id
        result.payer_account_name = self._payer_account_name()

        # Surface the authoritative-resolution degrading notes (definition
        # fetch failures, unresolved rules, name-resolution unavailability,
        # unavailable credit sharing preferences). None of these fails the run.
        result.messages.extend(
            sharing_resolution_messages(
                sharing,
                self._definition_retriever,
                self._account_name_resolver,
                self._sharing_preferences,
            )
        )

        # Step 3c: collect the credits that could not be categorized or
        # resolved. Pure and informational: it never alters the
        # classification above or the exit status.
        result.attention = build_attention_items(result.credits, lifecycle, sharing)

        # Step 4: new-credit detection, immediately after the write.
        prior, prior_read_ok = self._read_prior(snapshot.key, result)
        new_credits = self._detector.detect(snapshot, prior, prior_read_ok)
        result.new_credits = new_credits
        result.messages.extend(new_credit_messages(new_credits))

        # Step 5: expiration tiers over the Active_Credits only, relative to
        # the snapshot time. Passing ``lifecycle.active`` guarantees no
        # Inactive_Credit can generate an expiration alert.
        expiration = self._analyzer_factory(taken_at).classify(lifecycle.active)
        result.expiration = expiration
        result.messages.extend(expiration_messages(expiration))

        # Step 6: dashboard metrics. The dashboard reads the Active /
        # Inactive partition and ``status_by_id`` from the ``LifecycleResult``
        # built in step 3 rather than re-deriving statuses.
        dashboard = build_dashboard(lifecycle, expiration, new_credits, sharing)
        result.dashboard = dashboard

        # Step 7: notify. ``Notifier.send`` records
        # configuration errors and delivery failures in its result instead of
        # raising, and none of them makes the run fail: the data is persisted.
        # They are surfaced through ``messages`` and ``delivery_failed`` for
        # the CLI to print.
        if self._notifier is not None:
            body = build_dashboard_body(
                dashboard,
                expiration,
                new_credits,
                lifecycle.inactive,
                sharing,
                result.attention,
                snapshot_date=result.snapshot_date,
                payer_account_id=result.payer_account_id,
                payer_account_name=result.payer_account_name,
            )
            notify_result = self._notifier.send(body)
            result.notify_result = notify_result
            result.delivery_failed = bool(notify_result.delivery_failed)
            result.messages.extend(notify_result.messages)

        result.success = True
        return result
