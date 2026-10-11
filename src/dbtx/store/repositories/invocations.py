"""Invocations (plan 5.2): one dbt command run, started then finished.

The caller owns the transaction; the project's claim row must exist.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.store import models as m
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.enums import RunResult
from dbtx.store.errors import StoreError
from dbtx.store.loaders.run_results import InvocationRecord
from dbtx.store.repositories import jobs

# Node statuses that make the invocation a failure / a warning.
FAILURE_STATUSES = frozenset({"error", "fail", "runtime error"})
WARNING_STATUSES = frozenset({"warn"})

# Columns `start` sets; reloading the invocation replaces them with the same values.
START_COLUMNS = [
    "job_id",
    "project",
    "command",
    "start_time",
    "end_time",
    "result",
    "dbt_version",
    "adapter_type",
    "target",
    "selector",
    "args",
    "manifest_hash",
    "run_by",
    "host",
    "error_message",
]


def start(
    session: Session,
    adapter: StoreAdapter,
    *,
    project: str,
    adapter_type: str,
    record: InvocationRecord,
    run_by: Optional[str],
    host: Optional[str],
) -> None:
    """Store the invocation (not finished yet: no end time or result)."""
    if record.command is None:
        which = record.args.get("which", "?")
        raise StoreError(f"`{which}` is not a recorded command; invocation {record.invocation_id!r} not stored")
    if record.job_id is not None:
        jobs.record_external(session, adapter, record.job_id, project, record.start_time)
    row = {
        "invocation_id": record.invocation_id,
        "job_id": record.job_id,
        "project": project,
        "command": record.command,
        "start_time": record.start_time,
        "end_time": None,
        "result": None,
        "dbt_version": record.dbt_version,
        "adapter_type": adapter_type,
        "target": record.target,
        "selector": record.selector,
        "args": record.args,
        "manifest_hash": None,
        "run_by": run_by,
        "host": host,
        "error_message": None,
    }
    session.execute(
        adapter.upsert(m.Invocation.__table__, [row], keys=["invocation_id"], update=START_COLUMNS)
    )


def finish(
    session: Session,
    invocation_id: str,
    *,
    end_time: datetime,
    result: str,
    error_message: Optional[str],
    manifest_hash: Optional[str],
) -> None:
    invocation_table = m.Invocation.__table__
    changed = session.execute(
        sa.update(invocation_table)
        .where(invocation_table.c.invocation_id == invocation_id)
        .values(end_time=end_time, result=result, error_message=error_message, manifest_hash=manifest_hash)
    ).rowcount
    if changed == 0:
        raise StoreError(f"no invocation {invocation_id!r} to finish")


def result_from_statuses(statuses: Iterable[str]) -> str:
    """The invocation's result from its node statuses."""
    seen = set(statuses)
    if seen & FAILURE_STATUSES:
        return RunResult.FAILURE.value
    if seen & WARNING_STATUSES:
        return RunResult.WARNING.value
    return RunResult.SUCCESS.value
