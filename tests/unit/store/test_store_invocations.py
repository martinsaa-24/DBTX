"""Invocations and node results (`dbtx.store.repositories.invocations` / `results`,
plan 5.2 and 5.3).

Behaviour (the caller owns the transaction; the project's claim row and its nodes
exist):

`invocations.start(session, adapter, project=, adapter_type=, record=, run_by=, host=)`
* stores the invocation from an `InvocationRecord`; reloading the same invocation
  leaves the same row (an upsert keyed on `invocation_id`)
* a job id the queue never saw gets an `external` job first, so the foreign key holds;
  a queued job is left alone
* a command that is not recorded → StoreError

`invocations.finish(session, invocation_id, end_time=, result=, error_message=,
manifest_hash=)`: sets them; an invocation that was never started → StoreError

`result_from_statuses(statuses)`: any `error` / `fail` / `runtime error` → `failure`,
else any `warn` → `warning`, else `success` (also for no results)

`results.load(session, adapter, project, rows)` → (stored, skipped)
* a result whose node is not stored (hooks, analyses, `operation.*`) is skipped and
  counted
* each status is app-enforced (D9): strict mode refuses an unknown one, otherwise it is
  stored with a warning
* rows are upserted on the primary key: loading the same results again changes nothing
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from dbt.contracts.graph.manifest import Manifest

from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store import validation
from dbtx.store.errors import StoreError
from dbtx.store.loaders.manifest import manifest_rows
from dbtx.store.loaders.run_results import (
    InvocationRecord,
    freshness_result_rows,
    invocation_from_run_results,
    invocation_from_sources,
    run_result_rows,
)
from dbtx.store.repositories import invocations, jobs, results
from dbtx.store.repositories.nodes import sync_manifest
from tests.conftest import FROZEN_RUN_RESULTS, FROZEN_SOURCES

from .conftest import CLOCK_START

SEEN_AT = datetime(2025, 12, 31, tzinfo=timezone.utc)
RUN_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def project_store(store: Store, manifest: Manifest) -> Store:
    """`store` with the jaffle_shop claim and the frozen manifest's nodes."""
    with store.begin() as session:
        session.add(m.Project(project="jaffle_shop", folder="jaffle-shop", claimed_at=CLOCK_START, last_seen_at=CLOCK_START))
    manifest.metadata.generated_at = SEEN_AT.replace(tzinfo=None)
    with store.begin() as session:
        sync_manifest(session, store.adapter, manifest_rows(manifest))
    return store


@pytest.fixture
def run_results() -> dict:
    return json.loads(FROZEN_RUN_RESULTS.read_text(encoding="utf-8"))


def start(store: Store, record: InvocationRecord) -> None:
    with store.begin() as session:
        invocations.start(
            session, store.adapter, project="jaffle_shop", adapter_type="duckdb", record=record, run_by=None, host=None
        )


def invocation(store: Store, invocation_id: str) -> m.Invocation:
    with store.session() as session:
        return session.get(m.Invocation, invocation_id)


def load(store: Store, rows: list[dict]) -> tuple[int, int]:
    with store.begin() as session:
        return results.load(session, store.adapter, "jaffle_shop", rows)


def node_results(store: Store) -> list[tuple]:
    with store.session() as session:
        rows = session.execute(sa.select(m.NodeResult.__table__)).all()
        return sorted(tuple(row) for row in rows)


# --------------------------------------------------------------------------------------
# Invocations
# --------------------------------------------------------------------------------------


def test_start_stores_the_invocation(project_store: Store, run_results: dict) -> None:
    record = invocation_from_run_results(run_results)
    start(project_store, record)
    row = invocation(project_store, RUN_ID)
    assert (row.project, row.command, row.start_time, row.dbt_version, row.adapter_type, row.target) == (
        "jaffle_shop", "build", record.start_time, "1.12.5", "duckdb", "unknown"
    )
    assert (row.selector, row.job_id, row.args) == (None, None, run_results["args"])
    assert (row.end_time, row.result, row.error_message, row.manifest_hash, row.run_by, row.host) == (
        None, None, None, None, None, None
    )


def test_finish_sets_the_outcome(project_store: Store, run_results: dict) -> None:
    record = invocation_from_run_results(run_results)
    start(project_store, record)
    with project_store.begin() as session:
        invocations.finish(
            session, RUN_ID, end_time=record.end_time, result="warning", error_message=None, manifest_hash="ab" * 32
        )
    row = invocation(project_store, RUN_ID)
    assert (row.end_time, row.result, row.error_message, row.manifest_hash) == (
        record.end_time, "warning", None, "ab" * 32
    )


def test_starting_the_same_invocation_again_leaves_the_same_row(project_store: Store, run_results: dict) -> None:
    record = invocation_from_run_results(run_results)
    start(project_store, record)
    start(project_store, record)
    with project_store.session() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(m.Invocation)) == 1


def test_finish_an_invocation_that_was_never_started(project_store: Store) -> None:
    with pytest.raises(StoreError, match="no invocation 'nope'"):
        with project_store.begin() as session:
            invocations.finish(
                session, "nope", end_time=CLOCK_START, result="success", error_message=None, manifest_hash=None
            )


def test_a_command_that_is_not_recorded_is_refused(project_store: Store, run_results: dict) -> None:
    run_results["args"]["which"] = "ls"
    with pytest.raises(StoreError, match="`ls` is not a recorded command"):
        start(project_store, invocation_from_run_results(run_results))


def test_an_unknown_job_id_gets_an_external_job(project_store: Store, run_results: dict) -> None:
    run_results["args"]["vars"] = {"job_id": "ext-7"}
    record = invocation_from_run_results(run_results)
    start(project_store, record)
    assert invocation(project_store, RUN_ID).job_id == "ext-7"
    with project_store.session() as session:
        job = session.get(m.Job, "ext-7")
        assert (job.trigger, job.status, job.project, job.requested_at) == (
            "external", "finished", "jaffle_shop", record.start_time
        )


def test_a_queued_job_is_left_alone(project_store: Store, run_results: dict) -> None:
    with project_store.begin() as session:
        jobs.enqueue(session, "manual", project="jaffle_shop", job_id="job-1")
    run_results["args"]["vars"] = {"job_id": "job-1"}
    start(project_store, invocation_from_run_results(run_results))
    with project_store.session() as session:
        job = session.get(m.Job, "job-1")
        assert (job.trigger, job.status) == ("manual", "pending")


@pytest.mark.parametrize(
    "statuses, result",
    [
        ([], "success"),
        (["success", "pass", "no-op", "skipped"], "success"),
        (["pass", "warn"], "warning"),
        (["warn", "fail"], "failure"),
        (["success", "error"], "failure"),
        (["pass", "runtime error"], "failure"),
        (["partial success"], "success"),
    ],
)
def test_result_from_statuses(statuses: list[str], result: str) -> None:
    assert invocations.result_from_statuses(statuses) == result


# --------------------------------------------------------------------------------------
# Node results
# --------------------------------------------------------------------------------------


def test_load_stores_a_row_per_result(project_store: Store, run_results: dict) -> None:
    start(project_store, invocation_from_run_results(run_results))
    assert load(project_store, run_result_rows(run_results, "jaffle_shop", {})) == (48, 0)
    assert len(node_results(project_store)) == 48


def test_results_without_a_stored_node_are_skipped(
    project_store: Store, run_results: dict, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="dbtx.store")
    start(project_store, invocation_from_run_results(run_results))
    hook = dict(run_results["results"][0], unique_id="operation.jaffle_shop.jaffle_shop-on-run-end-0")
    analysis = dict(run_results["results"][0], unique_id="analysis.jaffle_shop.revenue_check")
    run_results["results"] += [hook, analysis]
    assert load(project_store, run_result_rows(run_results, "jaffle_shop", {})) == (48, 2)
    assert "node results: skipped 2 without a stored node in jaffle_shop" in caplog.text


def test_loading_the_same_results_again_changes_nothing(project_store: Store, run_results: dict) -> None:
    start(project_store, invocation_from_run_results(run_results))
    rows = run_result_rows(run_results, "jaffle_shop", {})
    load(project_store, rows)
    before = node_results(project_store)
    assert load(project_store, rows) == (48, 0)
    assert node_results(project_store) == before


def test_an_unknown_status_is_refused_in_strict_mode(project_store: Store, run_results: dict) -> None:
    start(project_store, invocation_from_run_results(run_results))
    run_results["results"][0]["status"] = "exploded"
    with pytest.raises(validation.UnknownValueError):
        load(project_store, run_result_rows(run_results, "jaffle_shop", {}))
    assert node_results(project_store) == []


def test_an_unknown_status_is_stored_with_a_warning_otherwise(
    project_store: Store, run_results: dict, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("DBTX_DB_STRICT")
    monkeypatch.setattr(validation, "_warned", set())
    start(project_store, invocation_from_run_results(run_results))
    run_results["results"][0]["status"] = "exploded"
    assert load(project_store, run_result_rows(run_results, "jaffle_shop", {})) == (48, 0)
    assert "storing unknown value 'exploded'" in caplog.text


def test_freshness_results_for_stored_sources(project_store: Store) -> None:
    sources = json.loads(FROZEN_SOURCES.read_text(encoding="utf-8"))
    with project_store.begin() as session:
        for name in ("raw_orders", "raw_customers"):
            session.add(
                m.Node(
                    project="jaffle_shop", unique_id=f"source.jaffle_shop.ecom.{name}", resource_type="source",
                    package_name="jaffle_shop", name=name, checksum=None, first_seen_at=SEEN_AT, last_seen_at=SEEN_AT,
                )
            )
    start(project_store, invocation_from_sources(sources))
    assert load(project_store, freshness_result_rows(sources, "jaffle_shop")) == (2, 0)
    statuses = sorted(row[3] for row in node_results(project_store))
    assert statuses == ["pass", "runtime error"]
