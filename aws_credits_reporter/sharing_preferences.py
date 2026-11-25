"""Credit_Sharing_Preferences: which accounts have credit sharing turned on.

Reads the Billing preferences feature ``CREDIT_SHARING`` through
``billing:GetBillingPreferences``. Each account has an ``account/<id>`` entry whose
value is ``ENABLED`` or ``DISABLED``; the same call also returns the account name.

A credit shared through a cost category only reaches the member accounts that
have credit sharing turned on, so the report marks members whose sharing is off.

The adapter is best-effort and **never raises**: a denied, throttled, or
unsupported call (a boto3 without the operation) makes the preferences
unavailable, in which case ``is_enabled`` answers ``None`` and the report simply
leaves the members unmarked. The call is made at most once per instance and its
result is cached.
"""

from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError

from .aws_calls import client_error_details
from .models import sanitize_text

FEATURE_CREDIT_SHARING = "CREDIT_SHARING"

PARAM_FEATURES = "features"
PARAM_NEXT_TOKEN = "nextToken"
RESPONSE_PREFERENCES = "billingPreferences"
RESPONSE_NEXT_TOKEN = "nextToken"

KEY_ACCOUNT_ID = "accountId"
KEY_ACCOUNT_NAME = "accountName"
KEY_VALUE = "value"
VALUE_ENABLED = "ENABLED"
VALUE_DISABLED = "DISABLED"

# Upper bound on pages followed, so a misbehaving token can never loop forever.
MAX_PAGES = 50


class CreditSharingPreferences:
    """Best-effort lookup of per-account credit sharing state."""

    def __init__(self, billing_client=None):
        self._client = billing_client
        self._loaded = False
        self._enabled: dict[str, bool] = {}
        self._names: dict[str, str] = {}
        self._unavailable = False
        self._error: str | None = None

    # -- state ---------------------------------------------------------------

    @property
    def consulted(self) -> bool:
        """True once a lookup was attempted."""
        return self._loaded

    @property
    def unavailable(self) -> bool:
        """True when the preferences could not be read (meaningful after a lookup)."""
        return self._unavailable

    @property
    def error_message(self) -> str | None:
        return self._error

    # -- lookups -------------------------------------------------------------

    def is_enabled(self, account_id: str) -> bool | None:
        """True / False when the account's sharing state is known, else ``None``."""
        self._ensure_loaded()
        return self._enabled.get(account_id)

    def account_name(self, account_id: str) -> str | None:
        """The account name returned with the preferences, when available."""
        self._ensure_loaded()
        return self._names.get(account_id)

    # -- loading -------------------------------------------------------------

    def _fail(self, message: str) -> None:
        self._unavailable = True
        self._error = message
        self._enabled = {}
        self._names = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._client is None:
            self._fail("Billing client not configured")
            return

        enabled: dict[str, bool] = {}
        names: dict[str, str] = {}
        token: str | None = None
        try:
            for _ in range(MAX_PAGES):
                params: dict = {PARAM_FEATURES: [FEATURE_CREDIT_SHARING]}
                if token:
                    params[PARAM_NEXT_TOKEN] = token
                response = self._client.get_billing_preferences(**params)
                for entry in response.get(RESPONSE_PREFERENCES) or []:
                    if not isinstance(entry, dict):
                        continue
                    account_id = entry.get(KEY_ACCOUNT_ID)
                    value = entry.get(KEY_VALUE)
                    if not isinstance(account_id, str) or value not in (
                        VALUE_ENABLED,
                        VALUE_DISABLED,
                    ):
                        continue
                    enabled[account_id] = value == VALUE_ENABLED
                    name = entry.get(KEY_ACCOUNT_NAME)
                    if isinstance(name, str) and name:
                        names[account_id] = sanitize_text(name)
                token = response.get(RESPONSE_NEXT_TOKEN)
                if not token:
                    break
        except ClientError as exc:
            code, _, message = client_error_details(exc)
            self._fail(f"GetBillingPreferences failed ({code}): {message}")
            return
        except (BotoCoreError, AttributeError) as exc:
            # AttributeError: a boto3 release without the operation.
            self._fail(f"GetBillingPreferences failed: {exc}")
            return

        self._enabled = enabled
        self._names = names
