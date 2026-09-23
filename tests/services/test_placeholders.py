"""The one place two SQL dialects meet.

Services write SQLite's placeholder style — `?` and `:name` — and the
PostgreSQL store rewrites it. That rewrite is a text substitution, so it is
only correct while it leaves string literals and comments alone: `'what?'` is
a question mark somebody meant to store, and `'HH:mm'` is a time format, not a
parameter called `mm`.

Neither would fail loudly. The first makes psycopg look for an argument that
was never passed; the second invents a named one. Both are the kind of SQL
somebody adds long after the person who wrote `translate` has stopped thinking
about it, which is why this file exists.
"""

from __future__ import annotations

import pytest

from api.models.postgres_store import translate
from tests.conftest import TEST_DB_URL, postgres_only


@pytest.mark.parametrize(
    "sql, expected",
    [
        # ordinary code is translated
        ("WHERE a = ? AND b = :name", "WHERE a = %s AND b = %(name)s"),
        # a question mark inside a literal is data
        ("WHERE b = 'what?'", "WHERE b = 'what?'"),
        ("WHERE a = ? AND b = 'what?'", "WHERE a = %s AND b = 'what?'"),
        # so is a colon that looks like a name
        ("WHERE fmt = 'HH:mm:ss' AND at = :now", "WHERE fmt = 'HH:mm:ss' AND at = %(now)s"),
        # per cent is doubled everywhere, because psycopg unescapes everywhere
        ("WHERE label LIKE 'a%b'", "WHERE label LIKE 'a%%b'"),
        ("WHERE pct = 50 % 7 AND id = ?", "WHERE pct = 50 %% 7 AND id = %s"),
        # a doubled quote does not end the literal
        ("SELECT 'it''s ok?', ?", "SELECT 'it''s ok?', %s"),
        # comments are prose, not code
        ("-- is this ? a comment\nWHERE id = ?", "-- is this ? a comment\nWHERE id = %s"),
        ("/* ? and :thing */ WHERE id = ?", "/* ? and :thing */ WHERE id = %s"),
        # a cast is not a named parameter
        ("SELECT x::text WHERE id = ?", "SELECT x::text WHERE id = %s"),
    ],
)
def test_only_the_code_is_rewritten(sql, expected):
    assert translate(sql) == expected


@postgres_only
def test_a_literal_survives_the_round_trip():
    """The translation is only half of it: psycopg then formats the result.

    So the proof is what comes back out of the database, not what the regex
    produced — a `%%` that is never unescaped, or a `%s` with no argument,
    both show up here and nowhere else.
    """
    import psycopg

    from api.models import database

    awkward = "50% done? at 09:30"
    with database.transaction() as conn:
        row = conn.execute(
            "SELECT ? AS bound, 'literal: 50% done? at 09:30' AS inline", (awkward,)
        ).fetchone()

    assert row["bound"] == awkward, "a value carrying % and ? did not survive binding"
    assert row["inline"] == "literal: 50% done? at 09:30", "a literal was rewritten"


@postgres_only
def test_a_literal_question_mark_would_break_without_the_guard():
    """Shows the failure the guard prevents, so the guard cannot be quietly
    deleted as unnecessary."""
    import psycopg

    from api.models import postgres_store

    naive = "SELECT 'what?' AS x"
    # what the old blunt substitution produced
    broken = naive.replace("%", "%%").replace("?", "%s")
    assert broken == "SELECT 'what%s' AS x"

    with postgres_store.pool().connection() as raw:
        with pytest.raises(Exception):
            raw.execute(broken, ())
        assert raw.rollback() is None
        assert raw.execute(translate(naive), ()).fetchone()["x"] == "what?"
