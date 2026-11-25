"""Shared helpers for calling AWS APIs: error inspection and retry with backoff.

The retrievers and the snapshot store each own their policy (what counts as
retryable, how many attempts, how a final failure is reported). This module owns
the mechanics they share: reading a ``ClientError``, spotting authorization
failures, and the exponential-backoff loop. ``sleep`` is injected so tests never
wait.
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from botocore.exceptions import ClientError

T = TypeVar("T")

BACKOFF_BASE_SECONDS = 0.5

# Codes that mean the caller is not allowed to make the call.
AUTHORIZATION_ERROR_CODES = frozenset({"AccessDeniedException", "UnauthorizedOperation"})


def client_error_details(exc: ClientError) -> tuple[str, int | None, str]:
    """``(error code, HTTP status or None, message)`` from a ``ClientError``."""
    response = exc.response if isinstance(exc.response, dict) else {}
    error = response.get("Error", {})
    error = error if isinstance(error, dict) else {}
    code = str(error.get("Code", ""))
    message = str(error.get("Message", "")) or str(exc)
    metadata = response.get("ResponseMetadata", {})
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return code, status, message


def is_authorization_error(
    exc: BaseException, extra_codes: frozenset[str] = frozenset()
) -> bool:
    """True for an access-denied style ``ClientError`` (code match or HTTP 403)."""
    if not isinstance(exc, ClientError):
        return False
    code, status, _ = client_error_details(exc)
    return code in AUTHORIZATION_ERROR_CODES or code in extra_codes or status == 403


def call_with_backoff(
    func: Callable[[], T],
    *,
    attempts: int,
    is_retryable: Callable[[BaseException], bool],
    sleep: Callable[[float], Any] = time.sleep,
) -> T:
    """Call ``func`` up to ``attempts`` times, sleeping ``0.5 * 2**n`` seconds between tries.

    An exception for which ``is_retryable`` is False is raised immediately. When
    every attempt fails, the last exception is raised. The caller decides how to
    report either outcome.
    """
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - classification is the caller's predicate
            if attempt >= attempts or not is_retryable(exc):
                raise
        sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")  # pragma: no cover
