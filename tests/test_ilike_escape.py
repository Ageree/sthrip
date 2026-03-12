"""Tests for ILIKE wildcard escaping."""


def test_escape_ilike_wildcards():
    from sthrip.utils import escape_ilike
    assert escape_ilike("normal") == "normal"
    assert escape_ilike("100%") == r"100\%"
    assert escape_ilike("under_score") == r"under\_score"
    assert escape_ilike("%_%") == r"\%\_\%"
    assert escape_ilike(r"back\slash") == r"back\\slash"


def test_escape_ilike_empty_string():
    from sthrip.utils import escape_ilike
    assert escape_ilike("") == ""
