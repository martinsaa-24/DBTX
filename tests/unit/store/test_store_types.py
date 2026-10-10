"""Column types (`dbtx.store.types`): what they must do on every backend.

* `UTCDateTime` stores every timestamp as UTC and always returns an aware UTC datetime,
  whatever offset it was given and whatever the database session's time zone is.
* `UTCDateTime` rejects a naive datetime, and nothing is written when it does.
* On SQLite, stored timestamps sort in time order (fixed-width UTC text).
* `JSONType` round-trips JSON values, stores Python `None` as SQL `NULL` (never the JSON
  literal `null`), and is `JSONB` on Postgres.
* `in_check` renders the CHECK expression for a closed set of values.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import StatementError

from dbtx.store.types import JSONType, UTCDateTime, in_check

PLUS_0530 = timezone(timedelta(hours=5, minutes=30))
MINUS_0800 = timezone(timedelta(hours=-8))

# A table that uses only the types under test, so these tests do not depend on the models.
probe_metadata = sa.MetaData()
probe_table = sa.Table(
    "type_probe",
    probe_metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("at", UTCDateTime(), nullable=True),
    sa.Column("doc", JSONType, nullable=True),
)


@pytest.fixture
def probe(engine):
    """The engine, with an empty `type_probe` table."""
    probe_metadata.create_all(engine)
    return engine


def insert(engine, row_id, at=None, doc=None):
    with engine.begin() as conn:
        conn.execute(sa.insert(probe_table).values(id=row_id, at=at, doc=doc))


def select_at(engine, row_id):
    with engine.connect() as conn:
        return conn.execute(sa.select(probe_table.c.at).where(probe_table.c.id == row_id)).scalar_one()


def select_doc(engine, row_id):
    with engine.connect() as conn:
        return conn.execute(sa.select(probe_table.c.doc).where(probe_table.c.id == row_id)).scalar_one()


def count_rows(engine):
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(probe_table)).scalar_one()


# --------------------------------------------------------------------------------------
# UTCDateTime: binding and reading
# --------------------------------------------------------------------------------------


def test_utc_value_round_trips_as_aware_utc(probe):
    value = datetime(2026, 3, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
    insert(probe, 1, at=value)

    result = select_at(probe, 1)

    assert result == value
    assert result.tzinfo == timezone.utc
    assert result.utcoffset() == timedelta(0)


@pytest.mark.parametrize("offset", [PLUS_0530, MINUS_0800], ids=["plus_0530", "minus_0800"])
def test_non_utc_value_comes_back_as_the_same_instant_in_utc(probe, offset):
    value = datetime(2026, 3, 1, 12, 30, 0, tzinfo=offset)
    insert(probe, 1, at=value)

    result = select_at(probe, 1)

    assert result == value  # the same instant
    assert result.tzinfo == timezone.utc
    assert result.replace(tzinfo=None) == value.astimezone(timezone.utc).replace(tzinfo=None)


def test_naive_value_is_rejected_and_nothing_is_written(probe):
    with pytest.raises(StatementError) as excinfo:
        insert(probe, 1, at=datetime(2026, 3, 1, 12, 30))

    assert isinstance(excinfo.value.orig, ValueError)
    assert "naive" in str(excinfo.value.orig)
    assert count_rows(probe) == 0


def test_none_timestamp_is_stored_as_null(probe):
    insert(probe, 1, at=None)

    assert select_at(probe, 1) is None


# --------------------------------------------------------------------------------------
# UTCDateTime: ordering of stored values
# --------------------------------------------------------------------------------------


def test_order_by_timestamp_is_time_order(probe):
    # Wall-clock order differs from time order: 10:00+05:30 is 04:30 UTC, the earliest.
    values = {
        1: datetime(2026, 3, 1, 6, 0, 0, tzinfo=timezone.utc),
        2: datetime(2026, 3, 1, 10, 0, 0, tzinfo=PLUS_0530),
        3: datetime(2026, 3, 1, 5, 0, 0, 1, tzinfo=timezone.utc),
        4: datetime(2026, 2, 28, 23, 0, 0, tzinfo=MINUS_0800),
        5: datetime(2026, 3, 1, 5, 0, 0, tzinfo=timezone.utc),
    }
    for row_id, value in values.items():
        insert(probe, row_id, at=value)

    with probe.connect() as conn:
        stored_order = conn.execute(sa.select(probe_table.c.id).order_by(probe_table.c.at)).scalars().all()

    expected_order = sorted(values, key=lambda row_id: values[row_id])
    assert stored_order == expected_order == [2, 5, 3, 1, 4]


def test_sqlite_stores_fixed_width_utc_text(probe):
    if probe.dialect.name != "sqlite":
        pytest.skip("storage format is SQLite-specific")
    insert(probe, 1, at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=PLUS_0530))
    insert(probe, 2, at=datetime(2026, 3, 1, 4, 30, 0, 1, tzinfo=timezone.utc))

    with probe.connect() as conn:
        raw = conn.execute(sa.text("SELECT at FROM type_probe ORDER BY id")).scalars().all()

    assert raw == ["2026-03-01 04:30:00.000000", "2026-03-01 04:30:00.000001"]


# --------------------------------------------------------------------------------------
# JSONType
# --------------------------------------------------------------------------------------


def test_json_document_round_trips(probe):
    document = {"vars": {"job_id": "j1"}, "select": ["a", "b"], "full_refresh": False, "threads": 4}
    insert(probe, 1, doc=document)

    assert select_doc(probe, 1) == document


def test_json_none_is_stored_as_sql_null(probe):
    insert(probe, 1, doc=None)

    with probe.connect() as conn:
        is_null = conn.execute(
            sa.select(probe_table.c.doc.is_(None)).where(probe_table.c.id == 1)
        ).scalar_one()

    assert bool(is_null) is True
    assert select_doc(probe, 1) is None


# --------------------------------------------------------------------------------------
# Storage types per dialect (DDL only, no database)
# --------------------------------------------------------------------------------------


def test_postgres_storage_types_are_timestamptz_and_jsonb():
    ddl = str(sa.schema.CreateTable(probe_table).compile(dialect=postgresql.dialect()))

    assert "at TIMESTAMP WITH TIME ZONE" in ddl
    assert "doc JSONB" in ddl


def test_sqlite_storage_types_are_datetime_and_json():
    ddl = str(sa.schema.CreateTable(probe_table).compile(dialect=sqlite.dialect()))

    assert "at DATETIME" in ddl
    assert "doc JSON" in ddl


# --------------------------------------------------------------------------------------
# in_check
# --------------------------------------------------------------------------------------


def test_in_check_for_a_required_column():
    assert in_check("status", ["pending", "running"], nullable=False) == "status IN ('pending', 'running')"


def test_in_check_for_a_nullable_column_allows_null_explicitly():
    assert (
        in_check("result", ["success", "failure"], nullable=True)
        == "result IS NULL OR result IN ('success', 'failure')"
    )


def test_in_check_keeps_spaces_and_escapes_quotes():
    assert (
        in_check("command", ["source freshness", "it's"], nullable=False)
        == "command IN ('source freshness', 'it''s')"
    )
