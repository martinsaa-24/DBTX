"""dbt's run artifacts → an invocation and its node-result rows (plan 5.2 and 5.3).

Two shapes: `run_results.json` (`RunResultsArtifact`: build, compile, run, test, seed,
snapshot, run-operation) and `sources.json` (`FreshnessExecutionResultArtifact`:
source freshness). The post-hoc load reads the files; the in-process path (Phase 5)
serialises dbt's results with dbt's own artifact classes into the same dicts. The
loaders never change the artifacts they are given.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dbt_common.events.functions import env_secrets, scrub_secrets

from dbtx.docs.catalog import DEFAULT_JOB_ID_VAR, read_job_id
from dbtx.store.enums import InvocationCommand, command_from_which

# `invocation.target` when the artifact does not name one (the column is NOT NULL).
UNKNOWN_TARGET = "unknown"


@dataclass(frozen=True)
class InvocationRecord:
    """An invocation as one artifact describes it."""

    invocation_id: str
    # The stored command, or None: a command that is not recorded.
    command: Optional[str]
    start_time: datetime
    end_time: datetime
    dbt_version: str
    target: str
    selector: Optional[str]
    args: dict[str, Any]
    job_id: Optional[str]


# `seconds.fraction` followed by the zone (or nothing).
FRACTION = re.compile(r"(\d{2}:\d{2}:\d{2})\.(\d+)")


def parse_timestamp(text: str) -> datetime:
    """An artifact timestamp (`2026-01-01T00:00:00.123Z`) as an aware UTC datetime.

    Python 3.10's `fromisoformat` accepts neither the `Z` suffix nor a fraction of
    other than 3 or 6 digits, so both are normalised first.
    """
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = FRACTION.search(text)
    if match is not None:
        digits = (match.group(2) + "000000")[:6]
        text = text[: match.start()] + f"{match.group(1)}.{digits}" + text[match.end() :]
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def scrub_args(args: dict[str, Any]) -> dict[str, Any]:
    """A copy of `args` with every set `DBT_ENV_SECRET_*` value replaced (dbt's scrub)."""
    secrets = env_secrets()
    if len(secrets) == 0:
        return copy.deepcopy(args)
    return scrub_value(copy.deepcopy(args), secrets)


def scrub_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return scrub_secrets(value, secrets)
    if isinstance(value, dict):
        scrubbed = {}
        for key, item in value.items():
            scrubbed[key] = scrub_value(item, secrets)
        return scrubbed
    if isinstance(value, list):
        return [scrub_value(item, secrets) for item in value]
    return value


def invocation_times(metadata: dict[str, Any], elapsed_time: float) -> tuple[datetime, datetime]:
    """(start, end): `invocation_started_at` (else end minus the elapsed time) and
    `generated_at`."""
    end_time = parse_timestamp(metadata["generated_at"])
    started = metadata.get("invocation_started_at")
    if started:
        start_time = parse_timestamp(started)
    else:
        start_time = end_time - timedelta(seconds=float(elapsed_time or 0.0))
    return start_time, end_time


def selector_of(args: dict[str, Any]) -> Optional[str]:
    select = args.get("select")
    if isinstance(select, list) and len(select) > 0:
        return " ".join(str(item) for item in select)
    if isinstance(select, str) and select != "":
        return select
    selector = args.get("selector")
    if selector:
        return str(selector)
    return None


def invocation_from_run_results(
    run_results: dict[str, Any], job_id_var: str = DEFAULT_JOB_ID_VAR
) -> InvocationRecord:
    metadata = run_results["metadata"]
    args = run_results.get("args") or {}
    start_time, end_time = invocation_times(metadata, run_results.get("elapsed_time", 0.0))
    return InvocationRecord(
        invocation_id=metadata["invocation_id"],
        command=command_from_which(str(args.get("which", ""))),
        start_time=start_time,
        end_time=end_time,
        dbt_version=metadata["dbt_version"],
        target=str(args.get("target") or UNKNOWN_TARGET),
        selector=selector_of(args),
        args=scrub_args(args),
        job_id=read_job_id(run_results, job_id_var),
    )


def invocation_from_sources(sources: dict[str, Any]) -> InvocationRecord:
    """`sources.json` carries no `args`: no target, selector or job id."""
    metadata = sources["metadata"]
    start_time, end_time = invocation_times(metadata, sources.get("elapsed_time", 0.0))
    return InvocationRecord(
        invocation_id=metadata["invocation_id"],
        command=InvocationCommand.SOURCE_FRESHNESS.value,
        start_time=start_time,
        end_time=end_time,
        dbt_version=metadata["dbt_version"],
        target=UNKNOWN_TARGET,
        selector=None,
        args={},
        job_id=None,
    )


def timing_span(timing: list[dict[str, Any]]) -> tuple[Optional[datetime], Optional[datetime]]:
    """(earliest start, latest completion) of a result's timing, or (None, None)."""
    starts = []
    completions = []
    for entry in timing or []:
        if entry.get("started_at"):
            starts.append(parse_timestamp(entry["started_at"]))
        if entry.get("completed_at"):
            completions.append(parse_timestamp(entry["completed_at"]))
    started_at = min(starts) if starts else None
    completed_at = max(completions) if completions else None
    return started_at, completed_at


def adapter_response_of(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    response = result.get("adapter_response")
    if not response:
        return None
    return copy.deepcopy(response)


def run_result_rows(
    run_results: dict[str, Any], project: str, checksums: dict[str, Optional[str]]
) -> list[dict[str, Any]]:
    """One node-result row per result. `checksums`: the run's manifest checksums."""
    invocation_id = run_results["metadata"]["invocation_id"]
    rows = []
    for result in run_results["results"]:
        started_at, completed_at = timing_span(result.get("timing"))
        response = adapter_response_of(result)
        rows_affected = None
        if response is not None:
            rows_affected = response.get("rows_affected")
        rows.append(
            {
                "invocation_id": invocation_id,
                "project": project,
                "unique_id": result["unique_id"],
                "status": result["status"],
                "checksum": checksums.get(result["unique_id"]),
                "started_at": started_at,
                "completed_at": completed_at,
                "execution_time": float(result.get("execution_time") or 0.0),
                "thread_id": result.get("thread_id"),
                "message": result.get("message"),
                "failures": result.get("failures"),
                "rows_affected": rows_affected,
                "adapter_response": response,
            }
        )
    return rows


def freshness_result_rows(sources: dict[str, Any], project: str) -> list[dict[str, Any]]:
    """One node-result row per source in `sources.json`. A `runtime error` entry has
    only `unique_id`, `error` and `status`."""
    invocation_id = sources["metadata"]["invocation_id"]
    rows = []
    for result in sources["results"]:
        started_at, completed_at = timing_span(result.get("timing"))
        rows.append(
            {
                "invocation_id": invocation_id,
                "project": project,
                "unique_id": result["unique_id"],
                "status": result["status"],
                "checksum": None,
                "started_at": started_at,
                "completed_at": completed_at,
                "execution_time": float(result.get("execution_time") or 0.0),
                "thread_id": result.get("thread_id"),
                "message": result.get("error"),
                "failures": None,
                "rows_affected": None,
                "adapter_response": adapter_response_of(result),
            }
        )
    return rows
