"""Value sets the loaders apply (plan 5.2 and D9).

Behaviour:

`command_from_which(which)`: run results record only the leaf command name
(`args.which`), so the post-hoc loader maps it to the stored `invocation.command`:
* `build`, `compile`, `test`, `run`, `seed`, `snapshot`, `parse`, `run-operation` →
  the same name
* `freshness` and the deprecated `snapshot-freshness` → `source freshness`
* `generate` → `docs generate`
* any other command (`ls`, `debug`, `deps`, `clean`, `show`, `retry`, `clone`, `serve`,
  ...) → None: it is not recorded (plan Q3)

`check_known(column, value, known)`, for app-enforced columns (values that come from
dbt, which a dbt upgrade can extend):
* a known value passes
* strict mode (`DBTX_DB_STRICT=1`, set for the whole test suite) → an unknown value
  raises UnknownValueError naming the column, the value and the known values
* otherwise → it passes, and a warning is logged once per (column, value)
"""

from __future__ import annotations

import logging
import os

import pytest

from dbtx.store import validation
from dbtx.store.enums import NODE_RESULT_STATUSES, InvocationCommand, command_from_which
from dbtx.store.errors import StoreError


@pytest.mark.parametrize(
    "which, command",
    [
        ("build", "build"),
        ("compile", "compile"),
        ("test", "test"),
        ("run", "run"),
        ("seed", "seed"),
        ("snapshot", "snapshot"),
        ("parse", "parse"),
        ("run-operation", "run-operation"),
        ("freshness", "source freshness"),
        ("snapshot-freshness", "source freshness"),
        ("generate", "docs generate"),
    ],
)
def test_recorded_commands(which: str, command: str) -> None:
    assert command_from_which(which) == command


@pytest.mark.parametrize(
    "which", ["ls", "list", "debug", "deps", "clean", "init", "show", "retry", "clone", "serve", ""]
)
def test_commands_that_are_not_recorded(which: str) -> None:
    assert command_from_which(which) is None


def test_every_stored_command_is_reachable() -> None:
    reachable = set()
    for which in ["build", "compile", "test", "run", "seed", "snapshot", "parse",
                  "run-operation", "freshness", "generate"]:
        reachable.add(command_from_which(which))
    assert reachable == {member.value for member in InvocationCommand}


# --------------------------------------------------------------------------------------
# App-enforced values (D9)
# --------------------------------------------------------------------------------------


@pytest.fixture
def lenient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production behaviour: no strict mode, and no value warned about yet."""
    monkeypatch.delenv("DBTX_DB_STRICT", raising=False)
    monkeypatch.setattr(validation, "_warned", set())


def test_the_suite_runs_in_strict_mode() -> None:
    assert os.environ.get("DBTX_DB_STRICT") == "1"


def test_a_known_value_passes() -> None:
    validation.check_known("node_result.status", "success", NODE_RESULT_STATUSES)


def test_an_unknown_value_raises_in_strict_mode() -> None:
    with pytest.raises(validation.UnknownValueError) as info:
        validation.check_known("node_result.status", "exploded", frozenset({"pass", "fail"}))
    assert isinstance(info.value, StoreError)
    assert str(info.value) == (
        "node_result.status: unknown value 'exploded' (known: fail, pass); strict mode "
        "(DBTX_DB_STRICT=1) refuses it"
    )


def test_an_unknown_value_is_stored_with_one_warning_in_lenient_mode(
    lenient: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="dbtx.store")
    validation.check_known("node_result.status", "exploded", frozenset({"pass", "fail"}))
    validation.check_known("node_result.status", "exploded", frozenset({"pass", "fail"}))
    validation.check_known("node_result.status", "imploded", frozenset({"pass", "fail"}))
    assert [record.getMessage() for record in caplog.records] == [
        "node_result.status: storing unknown value 'exploded' (known: fail, pass); a "
        "newer dbt may have added it",
        "node_result.status: storing unknown value 'imploded' (known: fail, pass); a "
        "newer dbt may have added it",
    ]
