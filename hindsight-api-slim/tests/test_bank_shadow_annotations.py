"""Unit tests for the list_banks shadow-annotation grouping.

``_annotate_bank_shadows`` is the pure, in-memory grouping used by
``MemoryEngine.list_banks`` to mark banks whose BARE id is shadowed in another
accessible schema. It groups the merged bank list (private bare ids + qualified
``schema/bank`` shared ids) by bare id and, for any bare id present in >=2
entries, annotates each entry with the OTHER entries' fully-qualified ids.
Single-occurrence banks get ``shadowed_by=None``.
"""

from hindsight_api.engine.memory_engine import _annotate_bank_shadows, _bare_bank_id


def _bank(bank_id: str) -> dict:
    return {"bank_id": bank_id}


def test_bare_bank_id_strips_prefix():
    assert _bare_bank_id("docs") == "docs"
    assert _bare_bank_id("shared_dev/docs") == "docs"
    # Only the first slash is a schema separator.
    assert _bare_bank_id("shared_dev/a/b") == "a/b"


def test_no_shadows_single_entries():
    banks = [_bank("docs"), _bank("shared_a/notes")]
    _annotate_bank_shadows(banks)
    assert banks[0]["shadowed_by"] is None
    assert banks[1]["shadowed_by"] is None


def test_private_and_shared_shadow_each_other():
    banks = [_bank("docs"), _bank("shared_dev/docs")]
    _annotate_bank_shadows(banks)
    by_id = {b["bank_id"]: b["shadowed_by"] for b in banks}
    assert by_id["docs"] == ["shared_dev/docs"]
    assert by_id["shared_dev/docs"] == ["docs"]


def test_three_way_shadow():
    banks = [_bank("docs"), _bank("shared_a/docs"), _bank("shared_b/docs")]
    _annotate_bank_shadows(banks)
    by_id = {b["bank_id"]: b["shadowed_by"] for b in banks}
    assert by_id["docs"] == ["shared_a/docs", "shared_b/docs"]
    assert by_id["shared_a/docs"] == ["docs", "shared_b/docs"]
    assert by_id["shared_b/docs"] == ["docs", "shared_a/docs"]


def test_mixed_shadowed_and_unshadowed():
    banks = [
        _bank("docs"),
        _bank("shared_a/docs"),
        _bank("notes"),
        _bank("shared_a/other"),
    ]
    _annotate_bank_shadows(banks)
    by_id = {b["bank_id"]: b["shadowed_by"] for b in banks}
    assert by_id["docs"] == ["shared_a/docs"]
    assert by_id["shared_a/docs"] == ["docs"]
    assert by_id["notes"] is None
    assert by_id["shared_a/other"] is None


def test_empty_list_is_noop():
    banks: list[dict] = []
    _annotate_bank_shadows(banks)
    assert banks == []
