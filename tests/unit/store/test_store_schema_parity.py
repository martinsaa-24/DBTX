"""Parity: the spec (`src/dbtx/store/schema.yml`) and the models must agree exactly.

The spec is the source of truth (plan D3). For every entity in the spec, the models must
have a table with:

* the same columns, in the same order, with the mapped type and nullability
* the same primary key (column order included), foreign keys and indexes
* a CHECK constraint for each `enforced_by: db` column, and none for `enforced_by: app`

and there must be no table, column, key, index or CHECK that the spec does not list.
The Python value sets (`dbtx.store.enums`) must hold exactly the spec's
`possible_values`. Every constraint and index has a deterministic name (Alembic batch
mode on SQLite needs them).
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB

from dbtx.store import enums
from dbtx.store import models as m
from dbtx.store.types import JSONType, UTCDateTime, in_check

from .conftest import load_spec, spec_tables

SPEC_TABLES = spec_tables(load_spec())
TABLE_NAMES = sorted(SPEC_TABLES)

# Which Python value set holds each enforced column's values.
DB_ENFORCED = {
    ("job", "status"): enums.JobStatus,
    ("job", "result"): enums.RunResult,
    ("invocation", "command"): enums.InvocationCommand,
    ("invocation", "result"): enums.RunResult,
    ("node", "resource_type"): enums.NodeResourceType,
    ("report_render", "status"): enums.RenderStatus,
}
APP_ENFORCED = {
    ("node_result", "status"): enums.NODE_RESULT_STATUSES,
}

POSTGRES_MAX_IDENTIFIER_LENGTH = 63


def table(table_name: str) -> sa.Table:
    tables = m.Base.metadata.tables
    assert table_name in tables, f"no model for spec table {table_name!r}"
    return tables[table_name]


def attributes(table_name: str) -> list[dict]:
    return SPEC_TABLES[table_name][1]["attributes"]


def attribute(table_name: str, field_name: str) -> dict:
    for item in attributes(table_name):
        if item["field_name"] == field_name:
            return item
    raise AssertionError(f"{table_name}.{field_name} is not in the spec")


def parse_reference(reference: str) -> tuple[str, tuple[str, ...]]:
    """'node(project, unique_id)' -> ('node', ('project', 'unique_id'))."""
    referred_table, columns = reference.rstrip(")").split("(")
    return referred_table.strip(), tuple(column.strip() for column in columns.split(","))


def mapped_class(table_name: str) -> type[m.Base]:
    for mapper in m.Base.registry.mappers:
        if mapper.local_table.name == table_name:
            return mapper.class_
    raise AssertionError(f"no mapped class for table {table_name!r}")


# --------------------------------------------------------------------------------------
# Tables and classes
# --------------------------------------------------------------------------------------


def test_models_have_exactly_the_spec_tables() -> None:
    assert sorted(m.Base.metadata.tables) == TABLE_NAMES


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_class_is_named_for_the_table_and_lives_in_the_group_module(table_name: str) -> None:
    group_name = SPEC_TABLES[table_name][0]
    expected_class_name = "".join(part.capitalize() for part in table_name.split("_"))

    cls = mapped_class(table_name)

    assert cls.__name__ == expected_class_name
    assert cls.__module__ == f"dbtx.store.models.{group_name}"
    assert getattr(m, expected_class_name) is cls


# --------------------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_columns_match_the_spec_in_order(table_name: str) -> None:
    expected = [item["field_name"] for item in attributes(table_name)]

    assert [column.name for column in table(table_name).columns] == expected


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_nullability_matches_the_spec(table_name: str) -> None:
    expected = {item["field_name"]: item.get("nullable", False) for item in attributes(table_name)}

    actual = {column.name: column.nullable for column in table(table_name).columns}

    assert actual == expected


def assert_column_type(column: sa.Column, field_type: str) -> None:
    column_type = column.type
    if field_type == "string":
        assert type(column_type) is sa.String, column.name
        assert column_type.length is None, column.name
    elif field_type == "integer":
        assert type(column_type) is sa.Integer, column.name
    elif field_type == "float":
        assert type(column_type) is sa.Double, column.name
    elif field_type == "timestamp":
        assert isinstance(column_type, UTCDateTime), column.name
    elif field_type == "json":
        # The storage type is the adapter's json_type(), chosen per dialect.
        assert isinstance(column_type, JSONType), column.name
        sqlite_type = column_type.load_dialect_impl(sqlite.dialect())
        assert isinstance(sqlite_type, sa.JSON), column.name
        assert sqlite_type.none_as_null is True, column.name
        postgres_type = column_type.load_dialect_impl(postgresql.dialect())
        assert isinstance(postgres_type, JSONB), column.name
        assert postgres_type.none_as_null is True, column.name
    else:
        raise AssertionError(f"unknown spec field_type {field_type!r} for {column.name}")


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_column_types_match_the_spec(table_name: str) -> None:
    model_table = table(table_name)
    for item in attributes(table_name):
        assert item["field_name"] in model_table.columns, item["field_name"]
        assert_column_type(model_table.columns[item["field_name"]], item["field_type"])


# --------------------------------------------------------------------------------------
# Keys and indexes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_primary_key_matches_the_spec_in_order(table_name: str) -> None:
    expected = SPEC_TABLES[table_name][1]["primary_key"]

    assert [column.name for column in table(table_name).primary_key.columns] == expected


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_foreign_keys_match_the_spec(table_name: str) -> None:
    expected = set()
    for foreign_key in SPEC_TABLES[table_name][1].get("foreign_keys", []):
        referred_table, referred_columns = parse_reference(foreign_key["references"])
        expected.add((tuple(foreign_key["columns"]), referred_table, referred_columns))

    actual = set()
    for constraint in table(table_name).foreign_key_constraints:
        local_columns = tuple(constraint.column_keys)
        referred_columns = tuple(element.column.name for element in constraint.elements)
        actual.add((local_columns, constraint.referred_table.name, referred_columns))

    assert actual == expected


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_foreign_keys_never_cascade(table_name: str) -> None:
    # History is append-only; removal is soft (`removed_at`).
    for constraint in table(table_name).foreign_key_constraints:
        assert constraint.ondelete is None
        assert constraint.onupdate is None


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_indexes_match_the_spec(table_name: str) -> None:
    expected = set()
    for index in SPEC_TABLES[table_name][1].get("indexes", []):
        expected.add(tuple(index["columns"]))

    actual = set()
    for index in table(table_name).indexes:
        assert index.unique is False, index.name
        actual.add(tuple(column.name for column in index.columns))

    assert actual == expected


# --------------------------------------------------------------------------------------
# CHECK constraints
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", TABLE_NAMES)
def test_check_constraints_are_exactly_the_db_enforced_columns(table_name: str) -> None:
    expected = {}
    for item in attributes(table_name):
        if item.get("enforced_by") == "db":
            name = f"ck_{table_name}_{item['field_name']}"
            expected[name] = in_check(
                item["field_name"], item["possible_values"], nullable=item.get("nullable", False)
            )

    actual = {}
    for constraint in table(table_name).constraints:
        if isinstance(constraint, sa.CheckConstraint):
            actual[str(constraint.name)] = str(constraint.sqltext)

    assert actual == expected


# --------------------------------------------------------------------------------------
# Enums (Python value sets)
# --------------------------------------------------------------------------------------


def test_every_enforced_column_in_the_spec_has_a_python_value_set() -> None:
    spec_db = set()
    spec_app = set()
    for table_name in TABLE_NAMES:
        for item in attributes(table_name):
            if item.get("enforced_by") == "db":
                spec_db.add((table_name, item["field_name"]))
            if item.get("enforced_by") == "app":
                spec_app.add((table_name, item["field_name"]))

    assert spec_db == set(DB_ENFORCED)
    assert spec_app == set(APP_ENFORCED)


@pytest.mark.parametrize("key", sorted(DB_ENFORCED), ids=lambda key: ".".join(key))
def test_db_enforced_enum_matches_the_spec_in_order(key: tuple[str, str]) -> None:
    table_name, field_name = key
    enum_class = DB_ENFORCED[key]

    assert [member.value for member in enum_class] == attribute(table_name, field_name)["possible_values"]
    assert enums.enum_values(enum_class) == attribute(table_name, field_name)["possible_values"]


@pytest.mark.parametrize("key", sorted(DB_ENFORCED), ids=lambda key: ".".join(key))
def test_db_enforced_enum_members_are_strings(key: tuple[str, str]) -> None:
    assert len(DB_ENFORCED[key]) > 0
    for member in DB_ENFORCED[key]:
        assert isinstance(member, str)
        assert member == member.value


@pytest.mark.parametrize("key", sorted(APP_ENFORCED), ids=lambda key: ".".join(key))
def test_app_enforced_value_set_matches_the_spec(key: tuple[str, str]) -> None:
    table_name, field_name = key

    assert isinstance(APP_ENFORCED[key], frozenset)
    assert sorted(APP_ENFORCED[key]) == sorted(attribute(table_name, field_name)["possible_values"])


# --------------------------------------------------------------------------------------
# Deterministic names
# --------------------------------------------------------------------------------------


def all_named_items() -> list[tuple[str, str, str | None]]:
    """(kind, table name, item name) for every constraint and index in the metadata."""
    items = []
    for model_table in m.Base.metadata.sorted_tables:
        for constraint in model_table.constraints:
            items.append((type(constraint).__name__, model_table.name, constraint.name))
        for index in model_table.indexes:
            items.append(("Index", model_table.name, index.name))
    return items


def test_every_constraint_and_index_has_a_conventional_name() -> None:
    prefixes = {
        "PrimaryKeyConstraint": "pk_",
        "ForeignKeyConstraint": "fk_",
        "CheckConstraint": "ck_",
        "Index": "ix_",
    }
    items = all_named_items()

    assert items, "the metadata has no constraints"
    for kind, table_name, name in items:
        assert name is not None, f"unnamed {kind} on {table_name}"
        assert str(name).startswith(prefixes[kind] + table_name), (kind, name)
        assert len(str(name)) <= POSTGRES_MAX_IDENTIFIER_LENGTH, name


def test_primary_key_and_index_names_are_unique() -> None:
    # Postgres keeps index names (primary keys included) in one namespace per schema.
    names = [str(name) for kind, _, name in all_named_items() if kind in ("PrimaryKeyConstraint", "Index")]

    assert names
    assert len(names) == len(set(names))
