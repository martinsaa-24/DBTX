"""Concurrent use of one store from several threads (plan Phase 2 tests, risk table).

Behaviour:
* concurrent writers on one store all succeed: 8 threads, each doing read-then-write
  transactions, never get "database is locked" (SQLite: `BEGIN IMMEDIATE` +
  `busy_timeout`) and lose no row
* `claim_next_job` never hands one job to two claimers:
  * while one claimer's transaction is still open, a second claimer gets the next job:
    on Postgres at once, without waiting (`FOR UPDATE SKIP LOCKED`); on SQLite after
    waiting for the first one's write lock (`BEGIN IMMEDIATE`)
  * two claimers draining a queue together claim every job exactly once

Only on `sqlite-file` and `postgres`: the in-memory database is one shared connection
(a test-only backend), so it has no concurrent connections to test.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta

import pytest
import sqlalchemy as sa

from dbtx.store import Store
from dbtx.store import models as m

from .conftest import CLOCK_START, ON_SQLITE_FILE_AND_POSTGRES

THREAD_TIMEOUT_SECONDS = 60


def run_in_threads(functions: list[Callable[[], object]]) -> list:
    """Run each function in its own thread, started together; re-raise the first error."""
    barrier = threading.Barrier(len(functions))
    results = [None] * len(functions)
    errors = []

    def run(index: int, function: Callable[[], object]) -> None:
        try:
            barrier.wait()
            results[index] = function()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = []
    for index, function in enumerate(functions):
        threads.append(threading.Thread(target=run, args=(index, function)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=THREAD_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "a thread did not finish"
    if errors:
        raise errors[0]
    return results


def add_pending_jobs(store: Store, count: int) -> list[str]:
    job_ids = []
    with store.begin() as session:
        for number in range(count):
            job_id = f"job-{number:03d}"
            job_ids.append(job_id)
            session.add(
                m.Job(
                    job_id=job_id,
                    trigger="manual",
                    requested_at=CLOCK_START + timedelta(seconds=number),
                    status="pending",
                )
            )
    return job_ids


# --------------------------------------------------------------------------------------
# Concurrent writers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_eight_concurrent_writers_all_succeed(store: Store) -> None:
    threads = 8
    transactions_per_thread = 25

    def writer(thread_number: int) -> Callable[[], None]:
        def write() -> None:
            for number in range(transactions_per_thread):
                with store.begin() as session:
                    # Read first, then write: the pattern that deadlocks a deferred
                    # SQLite transaction when another writer got in between.
                    session.scalar(sa.select(sa.func.count()).select_from(m.Project))
                    session.add(
                        m.Project(
                            project=f"p{thread_number}-{number}",
                            folder="f",
                            claimed_at=CLOCK_START,
                            last_seen_at=CLOCK_START,
                        )
                    )

        return write

    functions = []
    for thread_number in range(threads):
        functions.append(writer(thread_number))
    run_in_threads(functions)

    with store.session() as session:
        count = session.scalar(sa.select(sa.func.count()).select_from(m.Project))
    assert count == threads * transactions_per_thread


# --------------------------------------------------------------------------------------
# claim_next_job
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_claim_held_open_never_hands_out_the_same_job(store: Store) -> None:
    add_pending_jobs(store, 3)

    # Claimer A claims and keeps its transaction open.
    session_a = store.session()
    session_a.begin()
    job_a = store.adapter.claim_next_job(session_a).job_id

    # Claimer B claims meanwhile, in another thread.
    claimed_by_b = []
    errors = []

    def claim_b() -> None:
        try:
            with store.begin() as session_b:
                claimed_by_b.append(store.adapter.claim_next_job(session_b).job_id)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    thread = threading.Thread(target=claim_b)
    thread.start()
    try:
        if store.test_backend == "postgres":
            # SKIP LOCKED: B takes the next job without waiting for A.
            thread.join(timeout=5)
        else:
            # B waits for the write lock as long as A's transaction is open.
            thread.join(timeout=0.5)
        b_finished_while_a_was_open = not thread.is_alive()
    finally:
        # Always release A, so B (and the fixture's teardown) can never hang.
        session_a.commit()
        session_a.close()
        thread.join(timeout=THREAD_TIMEOUT_SECONDS)
    assert not thread.is_alive()
    assert errors == []

    if store.test_backend == "postgres":
        assert b_finished_while_a_was_open, "claimer B waited for claimer A's lock"
    else:
        assert not b_finished_while_a_was_open, "claimer B did not wait for A's write lock"
    assert (job_a, claimed_by_b) == ("job-000", ["job-001"])


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_two_claimers_claim_every_job_exactly_once(store: Store) -> None:
    job_ids = add_pending_jobs(store, 40)

    def claimer() -> list[str]:
        claimed = []
        while True:
            with store.begin() as session:
                job = store.adapter.claim_next_job(session)
                if job is None:
                    return claimed
                claimed.append(job.job_id)

    first, second = run_in_threads([claimer, claimer])

    assert sorted(first + second) == job_ids  # every job, none twice
    with store.session() as session:
        statuses = set(session.scalars(sa.select(m.Job.status)).all())
    assert statuses == {"running"}
