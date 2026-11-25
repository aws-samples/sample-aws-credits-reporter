"""Data models and raw-JSON parsers for AWS Credits Reporter.

The parsers in this module are deliberately tolerant: a raw ``get-credits``
record may omit any field, or carry a value of an unexpected shape, and the
parser must never raise. Every field that is absent or unparsable is recorded
by name in ``Credit.missing_fields`` and treated as ``None`` / empty
.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

# The verified ``get-credits`` field names, in order. ``exhaustDate`` lets
# ``lifecycle_status`` distinguish an Exhausted credit from an Active one.
CREDIT_FIELDS: tuple[str, ...] = (
    "creditId",
    "accountId",
    "creditType",
    "initialAmount",
    "remainingAmount",
    "estimatedAmount",
    "applicableProductNames",
    "description",
    "startDate",
    "endDate",
    "applicationType",
    "shareableAccounts",
    "accountHasCreditSharingEnabled",
    "creditConsoleVisibility",
    "creditSharingType",
    "creditStatus",
    "exhaustDate",
    "costCategoryArn",
    "ruleName",
)

_STRING_FIELDS = (
    "creditId",
    "accountId",
    "creditType",
    "description",
    "applicationType",
    "creditConsoleVisibility",
    "creditSharingType",
    "creditStatus",
    "costCategoryArn",
    "ruleName",
)
_AMOUNT_FIELDS = ("initialAmount", "remainingAmount", "estimatedAmount")
_LIST_FIELDS = ("applicableProductNames", "shareableAccounts")
_DATE_FIELDS = ("startDate", "endDate", "exhaustDate")
_BOOL_FIELDS = ("accountHasCreditSharingEnabled",)

# The eight fields retrieved per cost category, in order.
# Names are PascalCase because that is how the Cost Explorer API returns them.
COST_CATEGORY_FIELDS: tuple[str, ...] = (
    "CostCategoryArn",
    "Name",
    "EffectiveStart",
    "NumberOfRules",
    "ProcessingStatus",
    "Values",
    "DefaultValue",
    "SupportedResourceTypes",
)

_CATEGORY_STRING_FIELDS = ("CostCategoryArn", "Name", "EffectiveStart", "DefaultValue")
_CATEGORY_INT_FIELDS = ("NumberOfRules",)
_CATEGORY_STR_LIST_FIELDS = ("Values", "SupportedResourceTypes")
_CATEGORY_DICT_LIST_FIELDS = ("ProcessingStatus",)


@dataclass
class CurrencyAmount:
    """A monetary amount with its ISO currency code."""

    amount: Decimal
    currency: str


@dataclass
class Credit:
    """A single credit record from ``get-credits``.

    Every API field is optional. Field names that were absent from the raw
    record, or could not be parsed, are listed in ``missing_fields``.
    """

    creditId: str | None = None
    accountId: str | None = None
    creditType: str | None = None
    initialAmount: CurrencyAmount | None = None
    remainingAmount: CurrencyAmount | None = None
    estimatedAmount: CurrencyAmount | None = None
    applicableProductNames: list[str] = field(default_factory=list)
    description: str | None = None
    startDate: date | None = None
    endDate: date | None = None
    applicationType: str | None = None
    shareableAccounts: list[str] = field(default_factory=list)
    accountHasCreditSharingEnabled: bool | None = None
    creditConsoleVisibility: str | None = None
    creditSharingType: str | None = None
    creditStatus: str | None = None
    exhaustDate: date | None = None
    costCategoryArn: str | None = None
    ruleName: str | None = None
    missing_fields: list[str] = field(default_factory=list)


@dataclass
class CostCategory:
    """A single cost category reference from ``list-cost-category-definitions``.

    Every API field is optional. Field names that were
    absent from the raw record, or could not be parsed, are listed in
    ``missing_fields`` and left at ``None`` / empty.
    """

    CostCategoryArn: str | None = None
    Name: str | None = None
    EffectiveStart: str | None = None
    NumberOfRules: int | None = None
    ProcessingStatus: list[dict] = field(default_factory=list)
    Values: list[str] = field(default_factory=list)
    DefaultValue: str | None = None
    SupportedResourceTypes: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemberAccount:
    """One account that a Bound_Rule shares a credit with.

    ``account_id`` is the AWS account identifier taken from the bound rule's
    ``LINKED_ACCOUNT`` dimension ``Values``. ``account_name`` is the
    best-effort Organizations account Name resolved by the
    Account_Name_Resolver, or ``None`` when it could not be resolved (the
    renderer falls back to the bare id in that case).
    """

    account_id: str
    account_name: str | None = None
    # Whether the account has credit sharing turned on in the Billing
    # preferences: True (on), False (off), or None when unknown (the
    # preferences could not be read, or the account is not listed).
    sharing_enabled: bool | None = None


@dataclass
class CostCategoryDefinition:
    """A full cost category definition from ``describe-cost-category-definition``.

    Unlike ``CostCategory`` (a reference from the list API), this
    carries the ``Rules`` list so the Sharing_Classifier can select the rule
    whose ``Value`` matches a credit's ``ruleName`` and derive member accounts
    from its ``LINKED_ACCOUNT`` dimension. ``processing_complete`` is ``False``
    whenever the category's ``ProcessingStatus`` is not yet fully applied; the
    definition is still valid and its rules are read regardless
.
    """

    arn: str
    name: str | None
    rules: list[dict] = field(default_factory=list)
    processing_complete: bool = False


# Where a run came from. Only scheduled snapshots advance the
# baseline that new-credit detection compares against; a manual or duplicate run
# is stored for history but is skipped as baseline while a scheduled one exists.
SNAPSHOT_SOURCE_SCHEDULED = "scheduled"
SNAPSHOT_SOURCE_MANUAL = "manual"
SNAPSHOT_SOURCES = (SNAPSHOT_SOURCE_SCHEDULED, SNAPSHOT_SOURCE_MANUAL)


@dataclass
class Snapshot:
    """A timestamped raw snapshot of one run.

    ``raw_get_credits`` holds the unmodified ``get-credits`` response pages in
    the order they were received. ``raw_cost_categories`` holds the
    unmodified ``list-cost-category-definitions`` pages (empty when none were
    retrieved).
    """

    key: str
    taken_at: datetime
    raw_get_credits: list[dict] = field(default_factory=list)
    raw_cost_categories: list[dict] = field(default_factory=list)
    source: str = SNAPSHOT_SOURCE_SCHEDULED
    """Where the run came from; snapshots written before this field existed are scheduled."""


# --------------------------------------------------------------------------
# Value parsers: each returns the parsed value or None when unparsable.
# --------------------------------------------------------------------------


# Control characters that enable log/notification injection (CWE-117) when an
# API-sourced string is written into a log line, CLI output, or the plain-text
# SNS email body. CR and LF can forge new lines; other C0 controls can corrupt
# or spoof output. We neutralize them at the single parse boundary so every
# downstream display consumer is covered. The raw snapshot stored in S3 is
# unaffected: snapshot serialization uses the raw response pages, not the
# parsed Credit/CostCategory objects produced here.
# CR (0x0D) and LF (0x0A) are the line-forging characters; the remaining C0
# controls (except TAB, which is legitimate whitespace) are escaped too so they
# cannot corrupt or spoof log/CLI/email output. TAB (0x09) is preserved.
# Also escaped as defense in depth: DEL and the C1 controls, zero-width and
# bidirectional formatting characters (they can reorder or hide text), the
# Unicode line and paragraph separators and the byte order mark.
_ESCAPED_CODE_POINTS = (
    [cp for cp in range(0x20) if cp != 0x09]
    + list(range(0x7F, 0xA0))
    + list(range(0x200B, 0x2010))
    + list(range(0x2028, 0x202F))
    + list(range(0x2066, 0x206A))
    + [0xFEFF]
)
_CONTROL_CHAR_TRANSLATION = {
    cp: (f"\\x{cp:02x}" if cp < 0x100 else f"\\u{cp:04x}") for cp in _ESCAPED_CODE_POINTS
}


# Names
# and descriptions set by other account owners are printed in an email that
# finance reads, so a link or a contact address in them is a phishing vector.
# Replaced with a marker, with no word boundary in front so ``xhttps://`` and
# ``_https://`` do not slip through:
#   * anything shaped like ``scheme://``, ``scheme:/`` or ``scheme:\``
#   * ``mailto:``, ``tel:``, ``sms:``, ``javascript:``, ``data:`` and ``file:``
#   * email addresses
#   * ``www.`` hosts
#   * bare domains such as ``example.com/pay`` (any alphabetic top-level
#     domain, including dots written as full-width or ideographic full stops)
# Ordinary text, ARNs, version numbers and ``a:b`` stay unchanged. A bare
# ``word.Word`` with no space (for example ``Inc.Corp``) is also treated as a
# domain; the marker is a harmless cost for that false positive.
_DOT = r"[.\u3002\uff0e\uff61]"
# Every repetition in front of a match is bounded (a scheme of up to 32
# characters, a mailbox of up to 64, DNS labels of up to 63, up to 8 labels), so
# the cost grows linearly with the text length. With open-ended repetitions the
# search was quadratic: 20,000 characters took about 4 seconds, 100,000 about
# 90 seconds (found by static analysis for regular expression denial of service).
_URL_PATTERN = re.compile(
    rf"""(?ix)
      [a-z][a-z0-9+.-]{{0,31}}:[/\\]+\S*
    | (?:mailto|tel|sms|javascript|data|file):\S+
    | [^\s@<>()\[\]]{{1,64}}@[a-z0-9-]{{1,63}}(?:{_DOT}[a-z0-9-]{{1,63}}){{1,8}}
    | www{_DOT}\S+
    | (?:[a-z0-9-]{{1,63}}{_DOT}){{1,8}}[a-z]{{2,24}}\b(?:[/?\#]\S*)?
    """
)
LINK_REMOVED_MARKER = "[link removed]"


def sanitize_text(value: str) -> str:
    """Make API-sourced text safe to print.

    Escapes CR/LF and other C0 control characters (log and notification
    injection) and replaces URLs with ``[link removed]`` (phishing text in
    account names, cost category values or credit descriptions).
    """
    # URLs first: once a control character is escaped to ``\x0a`` the letter
    # after it would hide the word boundary in front of a link.
    return _URL_PATTERN.sub(LINK_REMOVED_MARKER, value).translate(_CONTROL_CHAR_TRANSLATION)


def _parse_str(value: Any) -> str | None:
    return sanitize_text(value) if isinstance(value, str) else None


def _parse_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _parse_int(value: Any) -> int | None:
    # ``bool`` is a subclass of ``int``; a boolean is not a valid count.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _parse_dict_list(value: Any) -> list[dict] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, dict) for item in value):
        return None
    return [dict(item) for item in value]


def _calendar_date_utc(value: datetime) -> date:
    """Return the UTC calendar date of a datetime.

    boto3 deserializes API timestamps into ``tzlocal()`` datetimes, so taking
    ``.date()`` directly would make the parsed date depend on the machine's
    timezone. Expiration windows are defined in UTC, so
    tz-aware values are converted to UTC first. Naive values are used as-is.
    """
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(timezone.utc)
    return value.date()


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return _calendar_date_utc(value)
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        # Accept full ISO 8601 timestamps such as "2024-01-15T00:00:00Z" or
        # "2026-03-11T17:00:00+00:00" (the shape seen in live responses).
        return _calendar_date_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, (str, int, float)):
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    return parsed if parsed.is_finite() else None


def _parse_amount(value: Any) -> CurrencyAmount | None:
    if not isinstance(value, dict):
        return None
    # The Billing service model names these ``currencyAmount`` and
    # ``currencyCode``; accept the shorter ``amount``/``currency`` as well.
    amount = _parse_decimal(value.get("currencyAmount", value.get("amount")))
    currency = _parse_str(value.get("currencyCode", value.get("currency")))
    if amount is None or currency is None:
        return None
    return CurrencyAmount(amount=amount, currency=currency)


_PARSERS = {}
_PARSERS.update({name: _parse_str for name in _STRING_FIELDS})
_PARSERS.update({name: _parse_amount for name in _AMOUNT_FIELDS})
_PARSERS.update({name: _parse_str_list for name in _LIST_FIELDS})
_PARSERS.update({name: _parse_date for name in _DATE_FIELDS})
_PARSERS.update({name: _parse_bool for name in _BOOL_FIELDS})


def parse_credit(raw: Any) -> Credit:
    """Parse one raw ``get-credits`` credit record into a ``Credit``.

    Never raises. Any field that is absent, ``None``, or fails to parse is
    treated as absent: it is left at its default (``None`` or an empty list)
    and its name is appended to ``missing_fields`` in ``CREDIT_FIELDS`` order.
    """
    credit = Credit()
    if not isinstance(raw, dict):
        credit.missing_fields = list(CREDIT_FIELDS)
        return credit

    for name in CREDIT_FIELDS:
        value = raw.get(name)
        parsed = _PARSERS[name](value) if value is not None else None
        if parsed is None:
            credit.missing_fields.append(name)
        else:
            setattr(credit, name, parsed)
    return credit


def parse_credits(raw_pages: list[dict]) -> list[Credit]:
    """Flatten a list of raw ``get-credits`` response pages into credits.

    Each page is expected to be a dict with a ``credits`` list. Pages or
    entries of an unexpected shape are skipped rather than raising.
    """
    credits: list[Credit] = []
    for page in raw_pages or []:
        if not isinstance(page, dict):
            continue
        entries = page.get("credits")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            credits.append(parse_credit(entry))
    return credits


# --------------------------------------------------------------------------
# Lifecycle_Status
# --------------------------------------------------------------------------


class LifecycleStatus(str, Enum):
    """Derived per-Credit status evaluated at the Snapshot_Date.

    The Billing API's ``creditStatus`` only carries ENABLED or DISABLED and
    does not express expiration; the Billing console derives its "Expired"
    label from ``endDate``. ``lifecycle_status`` mirrors that derivation and
    returns exactly one of these four values.
    """

    EXPIRED = "Expired"
    EXHAUSTED = "Exhausted"
    DISABLED = "Disabled"
    ACTIVE = "Active"


DISABLED_STATUS = "DISABLED"


def lifecycle_status(credit: Credit, as_of: date) -> LifecycleStatus:
    """Return the Lifecycle_Status of ``credit`` at ``as_of``.

    Rules, first match wins:

    (a) ``endDate`` is present and strictly before ``as_of`` -> Expired.
    (b) ``exhaustDate`` is present, or ``remainingAmount`` is present and its
        amount equals zero -> Exhausted.
    (c) ``creditStatus`` equals ``"DISABLED"`` -> Disabled.
    (d) otherwise Active.

    A missing ``endDate`` never yields Expired. A missing ``remainingAmount``
    never yields Exhausted on its own (the credit falls through to the
    creditStatus check and typically ends up Active).
    """
    if credit.endDate is not None and credit.endDate < as_of:
        return LifecycleStatus.EXPIRED
    if credit.exhaustDate is not None or (
        credit.remainingAmount is not None and credit.remainingAmount.amount == 0
    ):
        return LifecycleStatus.EXHAUSTED
    if credit.creditStatus == DISABLED_STATUS:
        return LifecycleStatus.DISABLED
    return LifecycleStatus.ACTIVE


# --------------------------------------------------------------------------
# Cost category parsing
# --------------------------------------------------------------------------

_CATEGORY_PARSERS = {}
_CATEGORY_PARSERS.update({name: _parse_str for name in _CATEGORY_STRING_FIELDS})
_CATEGORY_PARSERS.update({name: _parse_int for name in _CATEGORY_INT_FIELDS})
_CATEGORY_PARSERS.update({name: _parse_str_list for name in _CATEGORY_STR_LIST_FIELDS})
_CATEGORY_PARSERS.update({name: _parse_dict_list for name in _CATEGORY_DICT_LIST_FIELDS})


def parse_cost_category(raw: Any) -> CostCategory:
    """Parse one raw ``CostCategoryReferences`` entry into a ``CostCategory``.

    Never raises. Any field that is absent, ``None``, or fails to parse is
    treated as absent: it is left at its default (``None`` or an empty list)
    and its name is appended to ``missing_fields`` in ``COST_CATEGORY_FIELDS``
    order.
    """
    category = CostCategory()
    if not isinstance(raw, dict):
        category.missing_fields = list(COST_CATEGORY_FIELDS)
        return category

    for name in COST_CATEGORY_FIELDS:
        value = raw.get(name)
        parsed = _CATEGORY_PARSERS[name](value) if value is not None else None
        if parsed is None:
            category.missing_fields.append(name)
        else:
            setattr(category, name, parsed)
    return category


def parse_cost_categories(raw_pages: list[dict]) -> list[CostCategory]:
    """Flatten raw ``list-cost-category-definitions`` pages into categories.

    Each page is expected to be a dict with a ``CostCategoryReferences`` list.
    Pages or entries of an unexpected shape are skipped rather than raising.
    """
    categories: list[CostCategory] = []
    for page in raw_pages or []:
        if not isinstance(page, dict):
            continue
        entries = page.get("CostCategoryReferences")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            categories.append(parse_cost_category(entry))
    return categories
