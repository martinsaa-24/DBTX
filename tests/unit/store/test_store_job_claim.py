"""`adapter.claim_next_job(session)`: take the oldest pending job off the queue.

Behaviour (plan section 4; concurrency is in test_store_concurrency.py):
* returns the pending job with the earliest `requested_at` (ties: lowest `job_id`),
  now `running` with `start_time` = the store clock's now
* returns None when no job is pending; jobs in any other status are never claimed
* changes only the job it returns
* the caller owns the transaction: rolled back, the job is pending again
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from dbtx.store import Store
from dbtx.store import models as m

from .conftest import CLOCK_START, FakeClock


def add_job(store: Store, job_id: str, minutes: int, status: str = "pending") -> None:
    """A job requested `minutes` after CLOCK_START (negative: before)."""
    with store.begin() as session:
        session.add(
            m.Job(
                job_id=job_id,
                trigger="manual",
                requested_at=CLOCK_START + timedelta(minutes=minutes),
                status=status,
            )
        )


def jobs(store: Store) -> dict[str, tuple]:
    with store.session() as session:
        stored = {}
        for job in session.scalars(sa.select(m.Job)).all():
            stored[job.job_id] = (job.status, job.start_time)
        return stored


def claim(store: Store) -> tuple[str, str, datetime] | None:
    with store.begin() as session:
        job = store.adapter.claim_next_job(session)
        if job is None:
            return None
        return (job.job_id, job.status, job.start_time)


def test_an_empty_queue_gives_none(store: Store) -> None:
    assert claim(store) is None


@pytest.mark.parametrize("status", ["running", "cancelled", "finished"])
def test_only_pending_jobs_are_claimed(store: Store, status: str) -> None:
    add_job(store, "j1", minutes=-10, status=status)
    assert claim(store) is None
    assert jobs(store) == {"j1": (status, None)}


def test_the_oldest_pending_job_is_claimed(store: Store, clock: FakeClock) -> None:
    add_job(store, "newer", minutes=-1)
    add_job(store, "oldest", minutes=-30)
    add_job(store, "older", minutes=-20)
    clock.advance(minutes=5)

    assert claim(store) == ("oldest", "running", clock.now())
    assert jobs(store) == {
        "oldest": ("running", clock.now()),
        "older": ("pending", None),
        "newer": ("pending", None),
    }


def test_ties_go_to_the_lowest_job_id(store: Store) -> None:
    add_job(store, "b", minutes=-5)
    add_job(store, "a", minutes=-5)
    assert claim(store)[0] == "a"


def test_successive_claims_walk_the_queue_in_order(store: Store, clock: FakeClock) -> None:
    add_job(store, "j3", minutes=-1)
    add_job(store, "j1", minutes=-3)
    add_job(store, "running", minutes=-9, status="running")
    add_job(store, "j2", minutes=-2)

    claimed = []
    while True:
        clock.advance(seconds=1)
        result = claim(store)
        if result is None:
            break
        claimed.append(result)

    assert claimed == [
        ("j1", "running", CLOCK_START + timedelta(seconds=1)),
        ("j2", "running", CLOCK_START + timedelta(seconds=2)),
        ("j3", "running", CLOCK_START + timedelta(seconds=3)),
    ]


def test_a_rolled_back_claim_leaves_the_job_pending(store: Store) -> None:
    add_job(store, "j1", minutes=-1)
    with store.session() as session:
        with session.begin():
            assert store.adapter.claim_next_job(session).job_id == "j1"
            session.rollback()
    assert jobs(store) == {"j1": ("pending", None)}
    assert claim(store)[0] == "j1"
