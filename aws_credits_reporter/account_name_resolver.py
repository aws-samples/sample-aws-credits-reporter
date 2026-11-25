"""Account_Name_Resolver: best-effort AWS account id -> account name.

Resolves AWS account identifiers to their human-readable account names using
AWS Organizations, degrading gracefully to the bare identifier whenever the
lookup is unavailable, denied, or simply has no entry for an id. The resolver
is best-effort by design and **never raises**: a failure records that name
resolution was unavailable and every affected id falls back to ``None`` (the
renderer shows the bare id in that case).

Resolution strategy:

* A single paginated ``ListAccounts`` sweep builds an id -> Name map for the
  whole organization, which is cheaper and far less likely to be throttled
  than one ``DescribeAccount`` call per id. The sweep runs at most once per
  resolver instance and its result is cached, so each distinct requested id is
  resolved at most once per run.
* When the Organizations client is ``None`` (no access configured) the sweep is
  skipped entirely and every id falls back to the bare identifier with the
  unavailable flag set.

Client injection mirrors the other adapters (``category_retriever.py``): the
client is injected so tests can pass a stub, and ``us-east-1`` is used for the
real client. Any ``ClientError`` or ``BotoCoreError`` during the sweep (denied,
throttled, not in use, connection) makes name resolution unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .aws_calls import client_error_details
from .models import sanitize_text

ORGANIZATIONS_REGION = "us-east-1"

# Exact member names from the ``organizations`` service model.
RESPONSE_ACCOUNTS = "Accounts"
RESPONSE_NEXT_TOKEN = "NextToken"
PARAM_NEXT_TOKEN = "NextToken"
ACCOUNT_ID_FIELD = "Id"
ACCOUNT_NAME_FIELD = "Name"

@dataclass
class AccountNameResolution:
    """Outcome of resolving a set of account identifiers.

    ``names`` maps every requested id to its resolved account name, or ``None``
    when the name could not be resolved (the renderer falls back to the bare
    id in that case). ``unavailable`` is ``True`` when the Organizations lookup
    as a whole could not be performed (no client, or a denied/failed sweep),
    which lets callers surface a single "account name resolution unavailable"
    message rather than inferring it from the ``None`` values alone
.
    """

    names: dict[str, str | None] = field(default_factory=dict)
    unavailable: bool = False
    error_message: str | None = None


def make_organizations_client(session: boto3.Session | None = None):
    """Return an Organizations client pinned to ``us-east-1``.

    Organizations is a global service with its endpoint in ``us-east-1``;
    callers that cannot or do not want Organizations access should pass
    ``None`` to the resolver instead of a client.
    """
    if session is None:
        return boto3.client("organizations", region_name=ORGANIZATIONS_REGION)
    return session.client("organizations", region_name=ORGANIZATIONS_REGION)


class AccountNameResolver:
    """Best-effort resolver of account ids to names via AWS Organizations.

    Accepts an injected Organizations client or ``None``. ``None`` means no
    access is configured, so every requested id resolves to ``None`` with the
    unavailable flag set. A single ``ListAccounts`` sweep is performed lazily on
    the first ``resolve()`` call and cached, so distinct ids are resolved at
    most once per instance.
    """

    def __init__(self, organizations_client=None):
        self._client = organizations_client
        # Cached id -> Name map from the single ListAccounts sweep. ``None``
        # until the sweep has been attempted.
        self._account_map: dict[str, str] | None = None
        # Whether the sweep itself could not be performed (no client, or a
        # denied/failed ListAccounts). Distinct from "id not in the map".
        self._sweep_unavailable: bool = False
        self._sweep_error: str | None = None
        # Whether any ``resolve()`` call has been made this run, so a caller
        # that holds the resolver (the Orchestrator) can tell "never consulted"
        # from "consulted and reported unavailable" when surfacing the single
        # degrading message.
        self._consulted: bool = False

    @property
    def consulted(self) -> bool:
        """True once ``resolve()`` has been called at least once this run."""
        return self._consulted

    @property
    def unavailable(self) -> bool:
        """True when the Organizations sweep could not be performed.

        This reflects only the sweep itself (no client, or a denied/failed
        ``ListAccounts``), not the per-id "not in the map" case, so a caller
        can surface a single "name resolution unavailable" message without
        inspecting every resolution's ``names``. Meaningful only after
        ``consulted`` is True; it stays False on an unconsulted resolver.
        """
        return self._sweep_unavailable

    @property
    def error_message(self) -> str | None:
        """The sweep-level error recorded when ``unavailable`` is True, if any."""
        return self._sweep_error

    def _ensure_swept(self) -> None:
        """Run the ListAccounts sweep once, caching its result or failure.

        Never raises: a denied or failed sweep leaves an empty map with
        ``_sweep_unavailable`` set, so every id then falls back to the bare
        identifier.
        """
        if self._account_map is not None or self._sweep_unavailable:
            return

        if self._client is None:
            self._account_map = {}
            self._sweep_unavailable = True
            self._sweep_error = "AWS Organizations client not configured"
            return

        account_map: dict[str, str] = {}
        next_token: str | None = None
        while True:
            params: dict[str, str] = {}
            if next_token:
                params[PARAM_NEXT_TOKEN] = next_token
            try:
                response = self._client.list_accounts(**params)
            except (ClientError, BotoCoreError) as exc:
                # Any failure (denied, throttled, connection) makes the whole
                # sweep unavailable; fall back to bare ids for every account.
                self._account_map = {}
                self._sweep_unavailable = True
                if isinstance(exc, ClientError):
                    code, _, message = client_error_details(exc)
                    self._sweep_error = f"ListAccounts failed ({code}): {message}"
                else:
                    self._sweep_error = f"ListAccounts failed: {exc}"
                return

            for account in response.get(RESPONSE_ACCOUNTS, []) or []:
                if not isinstance(account, dict):
                    continue
                account_id = account.get(ACCOUNT_ID_FIELD)
                name = account.get(ACCOUNT_NAME_FIELD)
                if account_id is not None and name is not None:
                    # Names reach the plain-text email body; neutralize control
                    # characters as the other API-sourced strings are in models.
                    account_map[str(account_id)] = sanitize_text(str(name))

            next_token = response.get(RESPONSE_NEXT_TOKEN)
            if not next_token:
                break

        self._account_map = account_map

    def resolve(self, account_ids: list[str]) -> AccountNameResolution:
        """Resolve each distinct account id to its name, best-effort.

        Returns an ``AccountNameResolution`` whose ``names`` maps every
        requested id (de-duplicated internally, so each is resolved at most
        once) to its account name or ``None``. ``unavailable`` is set when the
        Organizations sweep as a whole could not be performed, or when at least
        one requested id was not present in a successful sweep; in both cases
        the affected ids resolve to ``None`` and the caller surfaces a
        degrading message. Never raises.
        """
        self._consulted = True
        self._ensure_swept()
        account_map = self._account_map or {}

        names: dict[str, str | None] = {}
        any_missing = False
        for account_id in account_ids:
            if account_id in names:
                continue
            name = account_map.get(account_id)
            names[account_id] = name
            if name is None:
                any_missing = True

        unavailable = self._sweep_unavailable or any_missing
        return AccountNameResolution(
            names=names,
            unavailable=unavailable,
            error_message=self._sweep_error,
        )
