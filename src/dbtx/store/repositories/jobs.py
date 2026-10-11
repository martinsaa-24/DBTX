"""The job queue (plan 5.6): the primitives a worker or scheduler would use.

Transitions are guarded by `WHERE status = :expected`, so two processes can never move
the same job twice; when no row changes, JobStateError says why. A worker or
scheduler process is out of scope. The caller owns the transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.store import clock
from dbtx.store import models as m
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.enums import JobStatus, RunResult, enum_values
from dbtx.store.errors import StoreError

# `job.trigger` for an invocation that named a job id the queue never saw.
EXTERNAL_TRIGGER = "external"


class JobStateError(StoreError):
    """A job transition that does not apply: no such job, or the wrong status."""


def new_job_id() -> str:
    return str(uuid.uuid4())


def enqueue(
    session: Session, trigger: str, project: Optional[str] = None, job_id: Optional[str] = None
) -> str:
    """Queue a `pending` job and return its id (a uuid4 when none is given)."""
    if job_id is None:
        job_id = new_job_id()
    if session.get(m.Job, job_id) is not None:
        raise JobStateError(f"job {job_id!r} already exists")
    session.add(
        m.Job(
            job_id=job_id,
            project=project,
            trigger=trigger,
            requested_at=clock.now(),
            status=JobStatus.PENDING.value,
        )
    )
    session.flush()
    return job_id


def claim_next(session: Session, adapter: StoreAdapter) -> Optional[m.Job]:
    """The oldest pending job, now `running`, or None (the adapter's atomic claim)."""
    return adapter.claim_next_job(session)


def finish(session: Session, job_id: str, result: str) -> None:
    """A `running` job → `finished` with `result`."""
    if result not in enum_values(RunResult):
        raise ValueError(
            f"{result!r} is not a job result; use one of {', '.join(enum_values(RunResult))}"
        )
    move(
        session,
        job_id,
        JobStatus.RUNNING,
        status=JobStatus.FINISHED.value,
        result=result,
        end_time=clock.now(),
    )


def cancel(session: Session, job_id: str) -> None:
    """A `pending` job → `cancelled`."""
    move(session, job_id, JobStatus.PENDING, status=JobStatus.CANCELLED.value, end_time=clock.now())


def move(session: Session, job_id: str, expected: JobStatus, **values: object) -> None:
    """Update the job only while it is in `expected`; JobStateError otherwise."""
    job_table = m.Job.__table__
    changed = session.execute(
        sa.update(job_table)
        .where(job_table.c.job_id == job_id)
        .where(job_table.c.status == expected.value)
        .values(**values)
    ).rowcount
    if changed == 1:
        return
    status = session.execute(
        sa.select(job_table.c.status).where(job_table.c.job_id == job_id)
    ).scalar_one_or_none()
    if status is None:
        raise JobStateError(f"no job {job_id!r}")
    raise JobStateError(f"job {job_id!r} is {status}, not {expected.value}")


def record_external(
    session: Session, adapter: StoreAdapter, job_id: str, project: Optional[str], at: datetime
) -> None:
    """Make sure the job an invocation names exists: a finished `external` job at
    `at`. A job that exists already is left alone."""
    row = {
        "job_id": job_id,
        "project": project,
        "trigger": EXTERNAL_TRIGGER,
        "requested_at": at,
        "status": JobStatus.FINISHED.value,
        "result": None,
        "start_time": at,
        "end_time": at,
    }
    session.execute(adapter.upsert(m.Job.__table__, [row], keys=["job_id"], update=[]))
