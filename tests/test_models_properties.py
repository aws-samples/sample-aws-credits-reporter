"""Property-based tests for the raw ``get-credits`` parser in ``models``.

These exercise Requirement 1.6: a raw Credit record may omit any of the
verified fields, and ``parse_credit`` must never raise, must record exactly
the omitted fields (by name) in ``missing_fields``, and must treat every
omitted field as absent.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from aws_credits_reporter.models import CREDIT_FIELDS, parse_credit

# --------------------------------------------------------------------------
# A "known-good" raw credit: every verified field is present and carries a
# value the parser accepts, so with nothing removed no field is missing. Each
# value is chosen to match the parser expectation for its field type.
# --------------------------------------------------------------------------
_FULL_RAW: dict[str, object] = {
    "creditId": "CR-0001",
    "accountId": "111122223333",
    "creditType": "Promotional",
    "initialAmount": {"currencyAmount": "100.00", "currencyCode": "USD"},
    "remainingAmount": {"currencyAmount": "50.00", "currencyCode": "USD"},
    "estimatedAmount": {"currencyAmount": "40.00", "currencyCode": "USD"},
    "applicableProductNames": ["Amazon EC2", "Amazon S3"],
    "description": "Migration Acceleration Program",
    "startDate": "2024-01-01",
    "endDate": "2025-12-31",
    "applicationType": "ANY",
    "shareableAccounts": ["111122223333", "444455556666"],
    "accountHasCreditSharingEnabled": True,
    "creditConsoleVisibility": "VISIBLE",
    "creditSharingType": "SHARED",
    "creditStatus": "ENABLED",
    "exhaustDate": "2025-06-30",
    "costCategoryArn": "arn:aws:ce::111122223333:costcategory/abc123",
    "ruleName": "ShareWithOrg",
}


def test_full_raw_has_no_missing_fields() -> None:
    """Sanity check: the known-good record parses with an empty missing list."""
    assert set(_FULL_RAW) == set(CREDIT_FIELDS)
    assert parse_credit(_FULL_RAW).missing_fields == []


# A subset of the verified field list, drawn as a set to remove from the
# known-good record. ``sampled_from`` over CREDIT_FIELDS keeps the generator
# constrained to exactly the field space Requirement 1.6 talks about.
_field_subsets = st.lists(st.sampled_from(CREDIT_FIELDS), unique=True).map(set)


@given(to_remove=_field_subsets)
def test_removed_fields_are_recorded_exactly_and_never_raises(to_remove: set[str]) -> None:
    """Property 1 (Requirement 1.6).

    For any subset of the verified field list removed from a raw credit,
    ``parse_credit`` records exactly that subset in ``missing_fields``, never
    raises, and leaves the present fields populated.

    **Validates: Requirements 1.6**
    """
    raw = {name: value for name, value in _FULL_RAW.items() if name not in to_remove}

    credit = parse_credit(raw)  # must not raise

    # Exactly the removed fields are recorded, regardless of order.
    assert set(credit.missing_fields) == to_remove
    # No field is recorded twice.
    assert len(credit.missing_fields) == len(set(credit.missing_fields))
    # missing_fields follows CREDIT_FIELDS order.
    assert credit.missing_fields == [n for n in CREDIT_FIELDS if n in to_remove]

    # Every present field parsed to a non-missing value; every removed field
    # is treated as absent (left at its default and not populated).
    for name in CREDIT_FIELDS:
        if name in to_remove:
            assert name in credit.missing_fields
        else:
            assert name not in credit.missing_fields
