"""Closed value sets for the store's columns (plan section 3.3).

**Database-enforced** (`enforced_by: db` in the spec): values dbtx itself controls.
Each is a `str, Enum` (Python 3.10 has no `StrEnum`), and the model's CHECK constraint
is built from it, so every value is defined here only.

**App-enforced** (`enforced_by: app`): values that come from dbt. A dbt upgrade can add
one, so the database does not check them; Python does (plan D9).

Member order follows the spec. The parity test (`tests/unit/store/
test_store_schema_parity.py`) fails if a value set and the spec disagree.
"""

from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    """`job.status`"""

    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    FINISHED = "finished"


class RunResult(str, Enum):
    """`job.result` and `invocation.result`"""

    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"


class InvocationCommand(str, Enum):
    """`invocation.command`"""

    PARSE = "parse"
    COMPILE = "compile"
    TEST = "test"
    RUN = "run"
    BUILD = "build"
    SEED = "seed"
    SNAPSHOT = "snapshot"
    SOURCE_FRESHNESS = "source freshness"
    RUN_OPERATION = "run-operation"
    DOCS_GENERATE = "docs generate"


class NodeResourceType(str, Enum):
    """`node.resource_type`: the only node types the loader stores."""

    MODEL = "model"
    SOURCE = "source"
    SNAPSHOT = "snapshot"
    SEED = "seed"
    EXPOSURE = "exposure"
    TEST = "test"
    UNIT_TEST = "unit_test"


class RenderStatus(str, Enum):
    """`report_render.status`"""

    PENDING = "pending"
    RUNNING = "running"
    RENDERED = "rendered"
    SKIPPED = "skipped"
    ERROR = "error"


# `node_result.status`: dbt's raw node status. App-enforced.
NODE_RESULT_STATUSES: frozenset[str] = frozenset(
    {
        "success",
        "error",
        "skipped",
        "no-op",
        "partial success",
        "pass",
        "fail",
        "warn",
        "runtime error",  # source freshness
    }
)


def enum_values(enum_class) -> list[str]:
    """The values of a `str, Enum`, in declaration order."""
    values = []
    for member in enum_class:
        values.append(member.value)
    return values
