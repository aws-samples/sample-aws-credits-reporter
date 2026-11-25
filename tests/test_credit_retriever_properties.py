"""Property tests for ``aws_credits_reporter.credit_retriever`` (Task 2.3).

Property 2 (Validates: Requirements 1.3):

    For any sequence of stubbed pages linked by tokens, ``retrieve()`` returns
    exactly the concatenation of all pages' credits in order and makes exactly
    one call per page.

The retriever follows the ``nextToken`` on each ``get_credits`` response and
stops when a page returns no token (Requirement 1.3). This test drives it with
a fake Billing client that serves a hypothesis-generated list of pages linked
by tokens, then asserts that:

* the parsed credits equal the in-order concatenation of every page's credits;
* exactly one ``get_credits`` call was made per page (no page skipped, none
  re-fetched); and
* the pagination token from each page (except the last) was passed on the
  following call, in order.

No mocking library is used: the fake client is a small hand-written stub that
records the parameters of each call and returns the next scripted page.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings, strategies as st

from aws_credits_reporter.credit_retriever import (
    PARAM_NEXT_TOKEN,
    RESPONSE_NEXT_TOKEN,
    CreditRetriever,
)


class FakeBillingClient:
    """A scripted ``get_credits`` backend that links pages by ``nextToken``.

    ``pages`` is the list of response dicts to return, in order, one per call.
    Each call's keyword parameters are recorded in ``calls`` so the test can
    assert exactly one call per page and that tokens were threaded correctly.
    """

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    def get_credits(self, **params: Any) -> dict:
        self.calls.append(params)
        if self._index >= len(self._pages):
            raise AssertionError("get_credits called more times than there are pages")
        page = self._pages[self._index]
        self._index += 1
        return page


def _credit_entry(page_idx: int, credit_idx: int) -> dict:
    """A minimal but well-formed raw credit record with a unique id."""
    return {
        "creditId": f"credit-{page_idx:03d}-{credit_idx:03d}",
        "accountId": "111111111111",
        "remainingAmount": {"currencyAmount": "1.00", "currencyCode": "USD"},
        "creditStatus": "ENABLED",
    }


# A strategy for one page's credit-count. Pages may be empty (a valid response
# that still carries a token to the next page).
_page_sizes = st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=6)


def _build_pages(page_sizes: list[int]) -> list[dict]:
    """Build response pages whose credit ids are globally unique and ordered.

    Every page but the last carries a ``nextToken``; the last carries none, so
    the retriever stops after it (Requirement 1.3).
    """
    pages: list[dict] = []
    last = len(page_sizes) - 1
    for page_idx, size in enumerate(page_sizes):
        page: dict[str, Any] = {
            "credits": [_credit_entry(page_idx, c) for c in range(size)],
        }
        if page_idx != last:
            page[RESPONSE_NEXT_TOKEN] = f"token-{page_idx}"
        pages.append(page)
    return pages


@settings(max_examples=200)
@given(page_sizes=_page_sizes)
def test_retrieve_concatenates_all_pages_and_calls_once_per_page(
    page_sizes: list[int],
) -> None:
    pages = _build_pages(page_sizes)
    client = FakeBillingClient(pages)
    # ``sleep`` is never hit on the happy path, but stub it so a stray retry
    # can never actually block.
    retriever = CreditRetriever(client, sleep=lambda _s: None)

    outcome = retriever.retrieve()

    # Exactly one call per page: no page skipped, none re-fetched.
    assert len(client.calls) == len(pages)

    # The tokens thread through: every page except the last hands its token to
    # the next call, in order. The first call carries no token.
    assert PARAM_NEXT_TOKEN not in client.calls[0]
    for i in range(1, len(pages)):
        assert client.calls[i].get(PARAM_NEXT_TOKEN) == pages[i - 1][RESPONSE_NEXT_TOKEN]

    # raw_pages is the unmodified pages in order.
    assert outcome.raw_pages == pages

    # The parsed credits are exactly the in-order concatenation of every page's
    # credits (Requirement 1.3).
    expected_ids = [
        entry["creditId"] for page in pages for entry in page["credits"]
    ]
    actual_ids = [credit.creditId for credit in outcome.credits]
    assert actual_ids == expected_ids


@given(page_sizes=_page_sizes)
def test_retrieve_stops_at_first_page_without_a_token(
    page_sizes: list[int],
) -> None:
    """The loop stops on the first tokenless page and never over-reads.

    ``FakeBillingClient`` raises if called more often than there are pages, so
    a passing run proves the retriever stopped exactly at the last page.
    """
    pages = _build_pages(page_sizes)
    client = FakeBillingClient(pages)
    retriever = CreditRetriever(client, sleep=lambda _s: None)

    retriever.retrieve()

    assert client._index == len(pages)
