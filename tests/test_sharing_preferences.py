"""Credit_Sharing_Preferences: per-account credit sharing state, best-effort."""

from __future__ import annotations

from botocore.exceptions import ClientError, EndpointConnectionError

from aws_credits_reporter.sharing_preferences import CreditSharingPreferences


def _entry(account_id: str, value: str, name: str | None = None) -> dict:
    entry = {
        "feature": "CREDIT_SHARING",
        "key": f"account/{account_id}",
        "value": value,
        "accountId": account_id,
    }
    if name is not None:
        entry["accountName"] = name
    return entry


class FakeBilling:
    def __init__(self, pages=None, error=None):
        self._pages = list(pages or [])
        self._error = error
        self.calls: list[dict] = []

    def get_billing_preferences(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._pages.pop(0)


def _denied() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "GetBillingPreferences",
    )


def test_reads_enabled_state_and_names_per_account():
    client = FakeBilling(
        [
            {
                "billingPreferences": [
                    {"feature": "CREDIT_SHARING", "key": "default", "value": "ENABLED"},
                    _entry("111111111111", "ENABLED", "Operations"),
                    _entry("222222222222", "DISABLED", "Log Archive"),
                ]
            }
        ]
    )
    prefs = CreditSharingPreferences(client)

    assert prefs.is_enabled("111111111111") is True
    assert prefs.is_enabled("222222222222") is False
    assert prefs.account_name("222222222222") == "Log Archive"
    # An account that is not listed (and the non-account "default" key) is unknown.
    assert prefs.is_enabled("333333333333") is None
    assert prefs.account_name("333333333333") is None
    assert prefs.consulted is True and prefs.unavailable is False
    assert client.calls == [{"features": ["CREDIT_SHARING"]}]  # one call, cached


def test_follows_pagination():
    client = FakeBilling(
        [
            {"billingPreferences": [_entry("111111111111", "ENABLED")], "nextToken": "t1"},
            {"billingPreferences": [_entry("222222222222", "DISABLED")]},
        ]
    )
    prefs = CreditSharingPreferences(client)

    assert prefs.is_enabled("111111111111") is True
    assert prefs.is_enabled("222222222222") is False
    assert client.calls == [
        {"features": ["CREDIT_SHARING"]},
        {"features": ["CREDIT_SHARING"], "nextToken": "t1"},
    ]


def test_ignores_entries_with_unexpected_values_or_shapes():
    client = FakeBilling(
        [
            {
                "billingPreferences": [
                    "not-a-dict",
                    {"accountId": "111111111111", "value": "MAYBE"},
                    {"value": "ENABLED"},
                    _entry("222222222222", "ENABLED"),
                ]
            }
        ]
    )
    prefs = CreditSharingPreferences(client)

    assert prefs.is_enabled("111111111111") is None
    assert prefs.is_enabled("222222222222") is True
    assert prefs.unavailable is False


def test_denied_call_makes_the_preferences_unavailable_and_never_raises():
    prefs = CreditSharingPreferences(FakeBilling(error=_denied()))

    assert prefs.is_enabled("111111111111") is None
    assert prefs.account_name("111111111111") is None
    assert prefs.consulted is True and prefs.unavailable is True
    assert "AccessDeniedException" in (prefs.error_message or "")


def test_connection_errors_and_a_boto3_without_the_operation_degrade_too():
    for error in (EndpointConnectionError(endpoint_url="https://billing"), AttributeError("no op")):
        prefs = CreditSharingPreferences(FakeBilling(error=error))
        assert prefs.is_enabled("111111111111") is None
        assert prefs.unavailable is True
        assert prefs.error_message


def test_no_client_is_unavailable():
    prefs = CreditSharingPreferences(None)

    assert prefs.is_enabled("111111111111") is None
    assert prefs.unavailable is True


def test_failure_is_remembered_and_the_call_is_not_repeated():
    client = FakeBilling(error=_denied())
    prefs = CreditSharingPreferences(client)

    prefs.is_enabled("111111111111")
    prefs.is_enabled("222222222222")

    assert len(client.calls) == 1


def test_nothing_is_called_until_a_lookup_is_made():
    client = FakeBilling([{"billingPreferences": []}])
    prefs = CreditSharingPreferences(client)

    assert prefs.consulted is False
    assert client.calls == []
