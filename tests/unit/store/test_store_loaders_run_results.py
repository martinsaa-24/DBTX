"""The run-results loader (`dbtx.store.loaders.run_results`, plan 5.2 and 5.3).

Behaviour, for the two artifact shapes (`run_results.json`, `sources.json`; the
in-process path will serialise dbt's results into the same shapes, Phase 5):

`invocation_from_run_results(run_results)`
* `invocation_id`, `dbt_version`; `start_time` = `metadata.invocation_started_at`
  (else `generated_at` minus `elapsed_time`), `end_time` = `metadata.generated_at`, as
  aware UTC (dbt writes `...Z`, which Python 3.10 cannot parse by itself)
* `command` from `args.which` (None: a command that is not recorded)
* `target` = `args.target` when given, else `'unknown'` (the artifact does not say,
  and dbtx never resolves profiles itself)
* `selector` = `args.select` joined with spaces, else `args.selector`, else None
* `args` with the value of every set `DBT_ENV_SECRET_*` variable replaced by `*****`
  (dbt scrubs what it writes; this covers artifacts rebuilt by other tools too)
* `job_id` from `args.vars` (the same var the docs overlay reads)

`invocation_from_sources(sources)`: `command` `source freshness`; no args in the
artifact (`{}`), so no target, selector or job id.

`run_result_rows(run_results, project, checksums)` / `freshness_result_rows(sources,
project)`: one node-result row per result:
* `started_at` / `completed_at`: the earliest `timing[].started_at` and the latest
  `timing[].completed_at`; None when there is no timing
* `rows_affected` from `adapter_response`; an empty `adapter_response` is None
* `checksum` from the manifest of the same run (None if unknown; always None for
  freshness)
* freshness: `message` = the error, `failures` / `rows_affected` None; a `runtime
  error` entry has no timing and `execution_time` 0.0
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from dbtx.store.loaders.run_results import (
    freshness_result_rows,
    invocation_from_run_results,
    invocation_from_sources,
    run_result_rows,
)
from tests.conftest import FROZEN_RUN_RESULTS, FROZEN_SOURCES

RUN_ID = "00000000-0000-4000-8000-000000000001"
FRESHNESS_ID = "00000000-0000-4000-8000-000000000002"
WARN_TEST = "test.jaffle_shop.warn_high_frequency_customers"
SEED = "seed.jaffle_shop.raw_products"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.fixture
def run_results() -> dict:
    return json.loads(FROZEN_RUN_RESULTS.read_text(encoding="utf-8"))


@pytest.fixture
def sources() -> dict:
    return json.loads(FROZEN_SOURCES.read_text(encoding="utf-8"))


def result_of(run_results: dict, unique_id: str) -> dict:
    for result in run_results["results"]:
        if result["unique_id"] == unique_id:
            return result
    raise AssertionError(unique_id)


# --------------------------------------------------------------------------------------
# The invocation
# --------------------------------------------------------------------------------------


def test_invocation_from_the_frozen_build(run_results: dict) -> None:
    invocation = invocation_from_run_results(run_results)
    assert invocation.invocation_id == RUN_ID
    assert invocation.command == "build"
    assert invocation.start_time == utc("2026-01-01T00:00:00")
    assert invocation.end_time == utc("2026-01-01T00:05:00")
    assert invocation.dbt_version == "1.12.5"
    assert invocation.target == "unknown"
    assert invocation.selector is None
    assert invocation.job_id is None
    assert invocation.args == run_results["args"]


def test_start_time_without_invocation_started_at(run_results: dict) -> None:
    del run_results["metadata"]["invocation_started_at"]
    # generated_at 00:05:00 minus elapsed_time 300 s
    assert invocation_from_run_results(run_results).start_time == utc("2026-01-01T00:00:00")


@pytest.mark.parametrize(
    "which, command", [("generate", "docs generate"), ("freshness", "source freshness"), ("ls", None)]
)
def test_command_from_which(run_results: dict, which: str, command: str | None) -> None:
    run_results["args"]["which"] = which
    assert invocation_from_run_results(run_results).command == command


def test_target_from_args(run_results: dict) -> None:
    run_results["args"]["target"] = "prod"
    assert invocation_from_run_results(run_results).target == "prod"


@pytest.mark.parametrize(
    "args, selector",
    [
        ({"select": ["customers+", "orders"]}, "customers+ orders"),
        ({"select": [], "selector": "nightly"}, "nightly"),
        ({"select": "customers"}, "customers"),
        ({"select": []}, None),
    ],
)
def test_selector(run_results: dict, args: dict, selector: str | None) -> None:
    assert "selector" not in run_results["args"]
    run_results["args"].update(args)
    assert invocation_from_run_results(run_results).selector == selector


def test_job_id_from_vars(run_results: dict) -> None:
    run_results["args"]["vars"] = {"job_id": "job-42"}
    assert invocation_from_run_results(run_results).job_id == "job-42"


def test_secret_env_values_are_scrubbed_from_args(
    run_results: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBT_ENV_SECRET_TOKEN", "s3cr3t-value")
    run_results["args"]["vars"] = {"token": "s3cr3t-value", "nested": ["a", "x-s3cr3t-value-y"]}
    args = invocation_from_run_results(run_results).args
    assert args["vars"] == {"token": "*****", "nested": ["a", "x-*****-y"]}
    assert "s3cr3t-value" not in json.dumps(args)
    # The artifact itself is not changed.
    assert run_results["args"]["vars"]["token"] == "s3cr3t-value"


def test_invocation_from_sources(sources: dict) -> None:
    invocation = invocation_from_sources(sources)
    assert invocation.invocation_id == FRESHNESS_ID
    assert invocation.command == "source freshness"
    assert invocation.start_time == utc("2026-01-02T00:00:00")
    assert invocation.end_time == utc("2026-01-02T00:01:00")
    assert invocation.dbt_version == "1.12.5"
    assert (invocation.target, invocation.selector, invocation.job_id, invocation.args) == (
        "unknown", None, None, {}
    )


# --------------------------------------------------------------------------------------
# Node results
# --------------------------------------------------------------------------------------


def test_a_row_per_result(run_results: dict) -> None:
    rows = run_result_rows(run_results, "jaffle_shop", {})
    assert len(rows) == 48
    assert {row["invocation_id"] for row in rows} == {RUN_ID}
    assert {row["project"] for row in rows} == {"jaffle_shop"}


def test_a_seed_row(run_results: dict) -> None:
    rows = run_result_rows(run_results, "jaffle_shop", {SEED: "c" * 64})
    row = {r["unique_id"]: r for r in rows}[SEED]
    assert row == {
        "invocation_id": RUN_ID,
        "project": "jaffle_shop",
        "unique_id": SEED,
        "status": "success",
        "checksum": "c" * 64,
        "started_at": utc("2026-01-01T00:01:00"),
        "completed_at": utc("2026-01-01T00:02:00"),
        "execution_time": 60.0,
        "thread_id": "Thread-1 (worker)",
        "message": "INSERT 10",
        "failures": None,
        "rows_affected": 10,
        "adapter_response": {"_message": "INSERT 10", "code": "INSERT", "rows_affected": 10},
    }


def test_a_warning_test_row(run_results: dict) -> None:
    row = {r["unique_id"]: r for r in run_result_rows(run_results, "jaffle_shop", {})}[WARN_TEST]
    assert (row["status"], row["failures"], row["message"], row["checksum"], row["rows_affected"]) == (
        "warn", 9, "Got 9 results, configured to warn if != 0", None, None
    )


def test_an_empty_adapter_response_is_none(run_results: dict) -> None:
    rows = {r["unique_id"]: r for r in run_result_rows(run_results, "jaffle_shop", {})}
    assert rows["exposure.jaffle_shop.top_customers"]["adapter_response"] is None


def test_timing_spans_the_earliest_start_and_the_latest_completion(run_results: dict) -> None:
    result_of(run_results, SEED)["timing"] = [
        {"name": "compile", "started_at": "2026-01-01T00:01:00.5Z", "completed_at": "2026-01-01T00:01:01Z"},
        {"name": "execute", "started_at": "2026-01-01T00:01:02Z", "completed_at": "2026-01-01T00:03:04.25Z"},
    ]
    row = {r["unique_id"]: r for r in run_result_rows(run_results, "jaffle_shop", {})}[SEED]
    assert row["started_at"] == datetime(2026, 1, 1, 0, 1, 0, 500000, tzinfo=timezone.utc)
    assert row["completed_at"] == datetime(2026, 1, 1, 0, 3, 4, 250000, tzinfo=timezone.utc)


def test_a_result_without_timing(run_results: dict) -> None:
    skipped = result_of(run_results, SEED)
    skipped.update(status="skipped", timing=[], execution_time=0.0)
    row = {r["unique_id"]: r for r in run_result_rows(run_results, "jaffle_shop", {})}[SEED]
    assert (row["status"], row["started_at"], row["completed_at"]) == ("skipped", None, None)


def test_freshness_rows(sources: dict) -> None:
    rows = freshness_result_rows(sources, "jaffle_shop")
    by_id = {row["unique_id"]: row for row in rows}
    assert by_id["source.jaffle_shop.ecom.raw_orders"] == {
        "invocation_id": FRESHNESS_ID,
        "project": "jaffle_shop",
        "unique_id": "source.jaffle_shop.ecom.raw_orders",
        "status": "pass",
        "checksum": None,
        "started_at": utc("2026-01-02T00:00:10"),
        "completed_at": utc("2026-01-02T00:00:15"),
        "execution_time": 5.0,
        "thread_id": "Thread-1 (worker)",
        "message": None,
        "failures": None,
        "rows_affected": None,
        "adapter_response": None,
    }
    assert by_id["source.jaffle_shop.ecom.raw_customers"] == {
        "invocation_id": FRESHNESS_ID,
        "project": "jaffle_shop",
        "unique_id": "source.jaffle_shop.ecom.raw_customers",
        "status": "runtime error",
        "checksum": None,
        "started_at": None,
        "completed_at": None,
        "execution_time": 0.0,
        "thread_id": None,
        "message": "Database Error\n  Catalog Error: Table with name raw_customers does not exist!",
        "failures": None,
        "rows_affected": None,
        "adapter_response": None,
    }


def test_the_loaders_do_not_change_the_artifacts(run_results: dict, sources: dict) -> None:
    run_before = copy.deepcopy(run_results)
    sources_before = copy.deepcopy(sources)
    invocation_from_run_results(run_results)
    run_result_rows(run_results, "jaffle_shop", {})
    invocation_from_sources(sources)
    freshness_result_rows(sources, "jaffle_shop")
    assert run_results == run_before
    assert sources == sources_before
