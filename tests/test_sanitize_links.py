"""Links and contact addresses in API-sourced text are removed before they reach the report."""

from __future__ import annotations

import pytest

from aws_credits_reporter.models import LINK_REMOVED_MARKER, sanitize_text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Pay at https://evil.example/login now", f"Pay at {LINK_REMOVED_MARKER} now"),
        ("see HTTP://x.test/a?b=c&d=e", f"see {LINK_REMOVED_MARKER}"),
        ("ftp://files.test/x", LINK_REMOVED_MARKER),
        ("go to www.evil.example/path today", f"go to {LINK_REMOVED_MARKER} today"),
        ("two https://a.test and https://b.test", f"two {LINK_REMOVED_MARKER} and {LINK_REMOVED_MARKER}"),
        # Forms that an earlier version of the pattern did not remove.
        ("Renew at aws-credits-renew.com/verify", f"Renew at {LINK_REMOVED_MARKER}"),
        ("Renew at aws-credits-renew.com", f"Renew at {LINK_REMOVED_MARKER}"),
        ("write to pay@aws-credit-desk.com now", f"write to {LINK_REMOVED_MARKER} now"),
        ("xhttps://evil.example/a", LINK_REMOVED_MARKER),
        ("_https://evil.example", f"_{LINK_REMOVED_MARKER}"),
        ("1https://evil.example", f"1{LINK_REMOVED_MARKER}"),
        ("mailto:a@evil.example", LINK_REMOVED_MARKER),
        ("javascript:alert(1)", LINK_REMOVED_MARKER),
        ("https:/\\evil.example", LINK_REMOVED_MARKER),
        ("Www.evil.example", LINK_REMOVED_MARKER),
        ("evil\u3002example", LINK_REMOVED_MARKER),
        ("pay at evil\uff0ecom/x today", f"pay at {LINK_REMOVED_MARKER} today"),
    ],
)
def test_urls_are_replaced(text, expected):
    assert sanitize_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Promotional credit for Q4",
        "Team Platform (prod)",
        "arn:aws:ce::111111111111:costcategory/abc-123",
        "wwwroot team",
        "ratio 1/2 and a:b",
        "Version 1.2.3 of the plan",
        "e.g. the first one, i.e. this one",
        "Migration Acceleration Program: Q4 2026",
    ],
)
def test_ordinary_text_is_unchanged(text):
    assert sanitize_text(text) == text


def test_control_characters_are_still_escaped():
    assert sanitize_text("a\nb") == "a\\x0ab"
    assert sanitize_text("x\r\nhttps://e.test") == f"x\\x0d\\x0a{LINK_REMOVED_MARKER}"


def test_extra_control_and_format_characters_are_escaped():
    assert sanitize_text("a\x7fb") == "a\\x7fb"
    assert sanitize_text("a\x85b") == "a\\x85b"
    assert sanitize_text("a\u2028b") == "a\\u2028b"
    assert sanitize_text("a\u202eb") == "a\\u202eb"
    assert sanitize_text("a\u200bb") == "a\\u200bb"
    assert sanitize_text("a\ufeffb") == "a\\ufeffb"
    # Ordinary non-ASCII text is kept.
    assert sanitize_text("Caf\u00e9 Zürich") == "Caf\u00e9 Zürich"


@pytest.mark.parametrize(
    "text",
    [
        "a" * 100_000,
        "a." * 50_000,
        "a." * 50_000 + "1",
        "x" * 100_000 + "@",
        "a+" * 50_000,
        "-" * 100_000,
        "a1-b.c" * 17_000,
    ],
)
def test_long_inputs_are_processed_in_linear_time(text):
    # Open-ended repetitions made the search quadratic (100,000 characters took
    # about 90 seconds). The bounds keep it to well under a second; 5 seconds
    # leaves room for a slow machine and still fails on the old pattern.
    import time

    started = time.perf_counter()
    sanitize_text(text)
    assert time.perf_counter() - started < 5


def test_links_next_to_long_runs_are_still_removed():
    padding = "a" * 500
    assert sanitize_text(f"{padding} https://evil.example/x") == f"{padding} {LINK_REMOVED_MARKER}"
    assert sanitize_text(f"{padding} evil.example/pay") == f"{padding} {LINK_REMOVED_MARKER}"
    assert sanitize_text(f"{padding} pay@evil.example") == f"{padding} {LINK_REMOVED_MARKER}"

