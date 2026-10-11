"""The job queue (`dbtx.store.repositories.jobs`, plan 5.6).

Behaviour (the caller owns the transaction; times come from `dbtx.store.clock`):
* `enqueue(session, trigger, project=None, job_id=None)` → the job id: a new `pending`
  job, `requested_at` now; a uuid4 id when none is given. An id that exists →
  JobStateError
* `claim_next(session, adapter)`: the adapter's atomic claim (oldest pending →
  running), or None
* `finish(session, job_id, result)`: only a `running` job → `finished` with the result
  and `end_time` now. A result outside `success` / `warning` / `failure` → ValueError
* `cancel(session, job_id)`: only a `pending` job → `cancelled`, `end_time` now
* a transition from the wrong status, or for a job that does not exist → JobStateError
  (a StoreError) naming the job and its status; nothing changes
* `record_external(session, adapter, job_id, project, at)`: the row for an invocation
  launched with a job id outside the queue (`dbt run --vars 'job_id: x'`), so the
  invocation's foreign key holds: `trigger` `external`, `finished`, at `at`. A job
  that exists already is left alone
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store.errors import StoreError
from dbtx.store.repositories import jobs

from .conftest import CLOCK_START, FakeClock


def enqueue(store: Store, trigger: str = "manual", job_id: str | None = None, project: str | None = None) -> str:
    with store.begin() as session:
        return jobs.enqueue(session, trigger, project=project, job_id=job_id)


def job(store: Store, job_id: str) -> m.Job:
    with store.session() as session:
        return session.get(m.Job, job_id)


def state(store: Store, job_id: str) -> tuple:
    row = job(store, job_id)
    return (row.status, row.result, row.start_time, row.end_time)


def test_enqueue_generates_an_id(store: Store) -> None:
    job_id = enqueue(store, trigger="schedule")
    assert uuid.UUID(job_id).version == 4
    row = job(store, job_id)
    assert (row.trigger, row.status, row.project, row.requested_at) == ("schedule", "pending", None, CLOCK_START)


def test_enqueue_with_an_id_and_a_project(store: Store) -> None:
    with store.begin() as session:
        session.add(m.Project(project="jaffle_shop", folder="jaffle-shop", claimed_at=CLOCK_START, last_seen_at=CLOCK_START))
    assert enqueue(store, job_id="job-1", project="jaffle_shop") == "job-1"
    assert job(store, "job-1").project == "jaffle_shop"


def test_enqueue_an_existing_id_is_an_error(store: Store) -> None:
    enqueue(store, job_id="job-1")
    with pytest.raises(jobs.JobStateError) as info:
        enqueue(store, job_id="job-1")
    assert isinstance(info.value, StoreError)
    assert str(info.value) == "job 'job-1' already exists"


def test_claim_next_takes_the_oldest_pending_job(store: Store, clock: FakeClock) -> None:
    enqueue(store, job_id="older")
    clock.advance(minutes=1)
    enqueue(store, job_id="newer")
    with store.begin() as session:
        claimed = jobs.claim_next(session, store.adapter)
        assert claimed is not None and claimed.job_id == "older"
    assert job(store, "older").status == "running"
    assert job(store, "newer").status == "pending"


def test_claim_next_on_an_empty_queue(store: Store) -> None:
    with store.begin() as session:
        assert jobs.claim_next(session, store.adapter) is None


def test_finish_a_running_job(store: Store, clock: FakeClock) -> None:
    enqueue(store, job_id="job-1")
    with store.begin() as session:
        jobs.claim_next(session, store.adapter)
    ended = clock.advance(minutes=5)
    with store.begin() as session:
        jobs.finish(session, "job-1", "warning")
    assert state(store, "job-1") == ("finished", "warning", CLOCK_START, ended)


@pytest.mark.parametrize("result", ["", "succeeded", "SUCCESS"])
def test_finish_with_an_unknown_result(store: Store, result: str) -> None:
    enqueue(store, job_id="job-1")
    with store.begin() as session:
        jobs.claim_next(session, store.adapter)
    with pytest.raises(ValueError, match="not a job result"):
        with store.begin() as session:
            jobs.finish(session, "job-1", result)
    assert job(store, "job-1").status == "running"


def test_finish_a_pending_job_is_an_error(store: Store) -> None:
    enqueue(store, job_id="job-1")
    with pytest.raises(jobs.JobStateError) as info:
        with store.begin() as session:
            jobs.finish(session, "job-1", "success")
    assert str(info.value) == "job 'job-1' is pending, not running"
    assert state(store, "job-1") == ("pending", None, None, None)


def test_finish_a_job_that_does_not_exist(store: Store) -> None:
    with pytest.raises(jobs.JobStateError) as info:
        with store.begin() as session:
            jobs.finish(session, "nope", "success")
    assert str(info.value) == "no job 'nope'"


def test_cancel_a_pending_job(store: Store, clock: FakeClock) -> None:
    enqueue(store, job_id="job-1")
    ended = clock.advance(minutes=2)
    with store.begin() as session:
        jobs.cancel(session, "job-1")
    assert state(store, "job-1") == ("cancelled", None, None, ended)


@pytest.mark.parametrize("claimed, status", [(True, "running"), (False, "cancelled")])
def test_cancel_from_another_status_is_an_error(store: Store, claimed: bool, status: str) -> None:
    enqueue(store, job_id="job-1")
    with store.begin() as session:
        if claimed:
            jobs.claim_next(session, store.adapter)
        else:
            jobs.cancel(session, "job-1")
    with pytest.raises(jobs.JobStateError) as info:
        with store.begin() as session:
            jobs.cancel(session, "job-1")
    assert str(info.value) == f"job 'job-1' is {status}, not pending"
    assert job(store, "job-1").status == status


def test_record_external_inserts_a_finished_external_job(store: Store) -> None:
    at = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    with store.begin() as session:
        session.add(m.Project(project="jaffle_shop", folder="jaffle-shop", claimed_at=CLOCK_START, last_seen_at=CLOCK_START))
    with store.begin() as session:
        jobs.record_external(session, store.adapter, "ext-1", "jaffle_shop", at)
    row = job(store, "ext-1")
    assert (row.trigger, row.status, row.project, row.requested_at, row.start_time, row.end_time) == (
        "external", "finished", "jaffle_shop", at, at, at
    )
    # Again: nothing changes.
    with store.begin() as session:
        jobs.record_external(session, store.adapter, "ext-1", "jaffle_shop", at + timedelta(days=1))
    assert job(store, "ext-1").requested_at == at


def test_record_external_leaves_a_queued_job_alone(store: Store) -> None:
    enqueue(store, job_id="job-1")
    with store.begin() as session:
        jobs.record_external(session, store.adapter, "job-1", None, CLOCK_START + timedelta(hours=1))
    row = job(store, "job-1")
    assert (row.trigger, row.status) == ("manual", "pending")
