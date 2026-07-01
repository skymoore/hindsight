"""Unit tests for the AmbiguousBankIdError exception shape.

A bare bank id that resolves to more than one accessible schema raises
``AmbiguousBankIdError``. These tests pin its public shape: ``bank_id``,
``schemas``, the derived ``conflicts`` property, and the message — all relied on
by the HTTP 409 handler that surfaces AMBIGUOUS_BANK_ID responses.
"""

from hindsight_api.extensions import AmbiguousBankIdError


def test_fields_are_stored():
    err = AmbiguousBankIdError("docs", ["user_alice", "shared_dev"])
    assert err.bank_id == "docs"
    assert err.schemas == ["user_alice", "shared_dev"]


def test_conflicts_property_qualifies_each_schema():
    err = AmbiguousBankIdError("docs", ["user_alice", "shared_dev"])
    assert err.conflicts == ["user_alice/docs", "shared_dev/docs"]


def test_conflicts_preserves_order():
    # Order is significant (primary first, then sorted) and must round-trip.
    err = AmbiguousBankIdError("notes", ["shared_a", "shared_b", "shared_c"])
    assert err.conflicts == ["shared_a/notes", "shared_b/notes", "shared_c/notes"]


def test_conflicts_empty_when_no_schemas():
    err = AmbiguousBankIdError("docs", [])
    assert err.conflicts == []


def test_message_mentions_bank_id():
    err = AmbiguousBankIdError("docs", ["user_alice", "shared_dev"])
    assert str(err) == "Bank id 'docs' is ambiguous across schemas you can access"


def test_is_exception():
    assert isinstance(AmbiguousBankIdError("x", []), Exception)
