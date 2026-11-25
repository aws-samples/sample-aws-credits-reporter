"""Unit tests for Account_Name_Resolver (Requirements 20.2, 20.3).

The resolver performs a single paginated ``ListAccounts`` sweep over AWS
Organizations and degrades gracefully to bare identifiers whenever the sweep
is unavailable, denied, or missing an id. These tests use a small fake
Organizations client (pagination via ``NextToken``, scripted ``ClientError``s)
in the same style as ``test_category_retriever.py``'s ``FakeCeClient``; no real
AWS account is touched.
"""

from __future__ import annotations

from botocore.exceptions import ClientError, EndpointConnectionError

from aws_credits_reporter.account_name_resolver import (
    AccountNameResolution,
    AccountNameResolver,
)


class FakeOrganizationsClient:
    """Fake Organizations client returning scripted ``list_accounts`` pages.

    Each element of ``script`` is either a response dict (optionally carrying a
    ``NextToken`` to drive pagination) or a ``BaseException`` to raise. Every
    call is recorded in ``calls`` so tests can assert the sweep ran exactly
    once regardless of how many ids are requested.
    """

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def list_accounts(self, **params):
        self.calls.append(params)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _access_denied() -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "not authorized to organizations:ListAccounts",
            },
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "ListAccounts",
    )


# --------------------------------------------------------------------------
# Resolved names (Req 20.1)
# --------------------------------------------------------------------------


def test_resolved_names_returned_for_known_ids():
    client = FakeOrganizationsClient(
        [
            {
                "Accounts": [
                    {"Id": "111111111111", "Name": "Platform"},
                    {"Id": "222222222222", "Name": "DataScience"},
                ]
            }
        ]
    )

    result = AccountNameResolver(client).resolve(["111111111111", "222222222222"])

    assert isinstance(result, AccountNameResolution)
    assert result.names == {"111111111111": "Platform", "222222222222": "DataScience"}
    assert result.unavailable is False
    assert result.error_message is None


def test_sweep_paginates_via_next_token_and_unions_pages():
    client = FakeOrganizationsClient(
        [
            {
                "Accounts": [{"Id": "111111111111", "Name": "Platform"}],
                "NextToken": "page-2",
            },
            {"Accounts": [{"Id": "222222222222", "Name": "DataScience"}]},
        ]
    )

    result = AccountNameResolver(client).resolve(["111111111111", "222222222222"])

    assert result.names == {"111111111111": "Platform", "222222222222": "DataScience"}
    assert result.unavailable is False
    # Two pages: one call with no token, one with the carried token.
    assert client.calls == [{}, {"NextToken": "page-2"}]


# --------------------------------------------------------------------------
# AccessDenied -> all bare ids with the unavailable flag (Req 20.3)
# --------------------------------------------------------------------------


def test_access_denied_yields_all_bare_ids_with_unavailable_flag():
    client = FakeOrganizationsClient([_access_denied()])

    result = AccountNameResolver(client).resolve(["111111111111", "222222222222"])

    assert result.names == {"111111111111": None, "222222222222": None}
    assert result.unavailable is True
    assert "AccessDeniedException" in result.error_message
    assert "organizations:ListAccounts" in result.error_message


def test_none_client_yields_all_bare_ids_with_unavailable_flag():
    result = AccountNameResolver(None).resolve(["111111111111", "222222222222"])

    assert result.names == {"111111111111": None, "222222222222": None}
    assert result.unavailable is True
    assert result.error_message == "AWS Organizations client not configured"


def test_connection_failure_yields_all_bare_ids_with_unavailable_flag():
    client = FakeOrganizationsClient(
        [EndpointConnectionError(endpoint_url="https://organizations.us-east-1.amazonaws.com")]
    )

    result = AccountNameResolver(client).resolve(["111111111111"])

    assert result.names == {"111111111111": None}
    assert result.unavailable is True
    assert "ListAccounts failed" in result.error_message


# --------------------------------------------------------------------------
# Partial resolution: name what you can, fall back for the rest (Req 20.3)
# --------------------------------------------------------------------------


def test_partially_resolvable_set_names_known_and_falls_back_for_unknown():
    client = FakeOrganizationsClient(
        [{"Accounts": [{"Id": "111111111111", "Name": "Platform"}]}]
    )

    result = AccountNameResolver(client).resolve(["111111111111", "999999999999"])

    assert result.names == {"111111111111": "Platform", "999999999999": None}
    # The sweep succeeded, but at least one id was unresolved -> degrade.
    assert result.unavailable is True
    # No sweep-level error: the sweep itself worked, the id was simply absent.
    assert result.error_message is None


# --------------------------------------------------------------------------
# Each id resolved once: single ListAccounts sweep, memoized (Req 20.2)
# --------------------------------------------------------------------------


def test_single_sweep_serves_many_requested_ids():
    client = FakeOrganizationsClient(
        [
            {
                "Accounts": [
                    {"Id": "111111111111", "Name": "Platform"},
                    {"Id": "222222222222", "Name": "DataScience"},
                    {"Id": "333333333333", "Name": "Sandbox"},
                ]
            }
        ]
    )
    resolver = AccountNameResolver(client)

    result = resolver.resolve(
        ["111111111111", "222222222222", "333333333333"]
    )

    assert result.names == {
        "111111111111": "Platform",
        "222222222222": "DataScience",
        "333333333333": "Sandbox",
    }
    # Exactly one single-page sweep for all three ids.
    assert client.calls == [{}]


def test_duplicate_ids_in_one_call_are_resolved_once():
    client = FakeOrganizationsClient(
        [{"Accounts": [{"Id": "111111111111", "Name": "Platform"}]}]
    )

    result = AccountNameResolver(client).resolve(
        ["111111111111", "111111111111", "111111111111"]
    )

    # De-duplicated internally: one entry in the map.
    assert result.names == {"111111111111": "Platform"}
    assert result.unavailable is False


def test_sweep_is_memoized_across_multiple_resolve_calls():
    client = FakeOrganizationsClient(
        [
            {
                "Accounts": [
                    {"Id": "111111111111", "Name": "Platform"},
                    {"Id": "222222222222", "Name": "DataScience"},
                ]
            }
        ]
    )
    resolver = AccountNameResolver(client)

    first = resolver.resolve(["111111111111"])
    second = resolver.resolve(["222222222222"])

    assert first.names == {"111111111111": "Platform"}
    assert second.names == {"222222222222": "DataScience"}
    # Only the first resolve swept; the second reused the cached map.
    assert client.calls == [{}]


def test_denied_sweep_is_not_retried_on_later_resolve_calls():
    client = FakeOrganizationsClient([_access_denied()])
    resolver = AccountNameResolver(client)

    first = resolver.resolve(["111111111111"])
    second = resolver.resolve(["222222222222"])

    assert first.unavailable is True
    assert second.unavailable is True
    assert second.names == {"222222222222": None}
    # The failed sweep is cached as unavailable; ListAccounts is called once.
    assert client.calls == [{}]


def test_account_names_have_control_characters_neutralized():
    """Organizations names flow into the plain-text email body (CWE-117)."""
    client = FakeOrganizationsClient(
        [{"Accounts": [{"Id": "111111111111", "Name": "Prod\nFAKE ALERT\r"}]}]
    )
    resolution = AccountNameResolver(client).resolve(["111111111111"])
    name = resolution.names["111111111111"]
    assert name is not None
    assert "\n" not in name and "\r" not in name
    assert name == "Prod\\x0aFAKE ALERT\\x0d"
