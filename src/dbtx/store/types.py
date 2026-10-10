"""Column types shared by every store model (plan section 3.1).

* `UTCDateTime`: the spec's `timestamp`. Stored as UTC, returned as an aware UTC
  datetime. A naive datetime is rejected, because its time zone is unknown.
* `JSONType`: the spec's `json`. `JSONB` on Postgres, JSON text on SQLite. Python
  `None` is stored as SQL `NULL`, not as the JSON literal `null`.
* `in_check()`: the CHECK expression for a column whose values the database enforces.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A timezone-aware timestamp, always stored and returned in UTC.

    SQLite has no timestamp type: SQLAlchemy stores fixed-width text without an offset,
    so string order equals time order only because every value is converted to UTC
    here first. Postgres stores `TIMESTAMPTZ` and returns values in the session's time
    zone, so results are converted back to UTC.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected a datetime, got {type(value).__name__}: {value!r}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"naive datetime {value.isoformat()} has no time zone; "
                "pass an aware datetime (for example with tzinfo=timezone.utc)"
            )
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite: the stored text has no offset, and it is always UTC.
            return value.replace(tzinfo=timezone.utc)
        # Postgres: the value is in the session's time zone.
        return value.astimezone(timezone.utc)


JSONType = sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


def in_check(column_name: str, values: list[str], nullable: bool) -> str:
    """CHECK expression allowing only `values` (and NULL, when `nullable`).

    SQL already lets NULL pass an IN check; for a nullable column it is written out
    anyway so the intent is visible in the schema.
    """
    quoted_values = []
    for value in values:
        quoted_values.append("'" + value.replace("'", "''") + "'")
    in_list = f"{column_name} IN ({', '.join(quoted_values)})"
    if nullable:
        return f"{column_name} IS NULL OR {in_list}"
    return in_list
