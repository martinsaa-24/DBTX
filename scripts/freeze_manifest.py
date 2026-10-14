"""Freeze examples/jaffle_shop's artifacts into tests/fixtures/.

    python scripts/freeze_manifest.py

Writes `manifest.json` (a `dbt parse`), `run_results.json` (a `dbt build` of a
temporary copy, so the example's warehouse and target/ are untouched) and `sources.json`
(a `dbt source freshness` of another temporary copy, after `dbt seed`). Run this after
changing the example project. The frozen files are scrubbed of anything machine- or
run-specific (absolute paths, timestamps, ids, durations, thread names, Windows
separators) and of dbt's built-in macros, so they are small, diffable and identical on
every machine. `tests/live/test_frozen_manifest.py` fails when they drift from the
project.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROJECT_DIR = REPO / "examples" / "jaffle_shop"
FROZEN_PATH = REPO / "tests" / "fixtures" / "manifest.json"
FROZEN_RUN_RESULTS_PATH = REPO / "tests" / "fixtures" / "run_results.json"
FROZEN_SOURCES_PATH = REPO / "tests" / "fixtures" / "sources.json"
PROJECT_NAME = "jaffle_shop"

_PATH_KEYS = {"original_file_path", "path", "patch_path"}
_VOLATILE_METADATA = {
    "generated_at", "invocation_id", "invocation_started_at", "run_started_at", "user_id", "env"
}


def parse_manifest() -> dict[str, Any]:
    """`dbt parse` the example project into a throwaway target dir and return the raw JSON."""
    from dbt.cli.main import dbtRunner

    with tempfile.TemporaryDirectory() as tmp:
        result = dbtRunner().invoke(
            [
                "parse",
                "--project-dir", str(PROJECT_DIR),
                "--profiles-dir", str(PROJECT_DIR),
                "--target-path", tmp,
                "--no-partial-parse",
                "--quiet",
            ]
        )
        if not result.success:
            raise RuntimeError(f"dbt parse failed: {result.exception}")
        return json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))


def scrub(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["metadata"] = {
        k: v for k, v in manifest["metadata"].items() if k not in _VOLATILE_METADATA
    }
    manifest["metadata"]["send_anonymous_usage_stats"] = False
    manifest["macros"] = {
        uid: m for uid, m in manifest["macros"].items() if m["package_name"] == PROJECT_NAME
    }
    manifest["docs"] = {
        uid: d for uid, d in manifest["docs"].items() if d["package_name"] == PROJECT_NAME
    }

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            if obj.get("name") == "path" and isinstance(obj.get("checksum"), str):
                # Seeds too large to hash are "checksummed" by their (OS-specific) path.
                return {**obj, "checksum": obj["checksum"].replace("\\", "/")}
            out = {}
            for key, value in obj.items():
                if key == "root_path":
                    value = "."
                elif key == "created_at":
                    value = 0
                elif key in _PATH_KEYS and isinstance(value, str):
                    value = value.replace("\\", "/")
                out[key] = walk(value)
            return out
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj

    return walk(manifest)


def frozen_manifest() -> dict[str, Any]:
    return scrub(parse_manifest())


# Fixed values for what changes on every run.
FROZEN_RUN_METADATA = {
    "invocation_id": "00000000-0000-4000-8000-000000000001",
    "invocation_started_at": "2026-01-01T00:00:00Z",
    "generated_at": "2026-01-01T00:05:00Z",
    "env": {},
}
FROZEN_TIMING = {"started_at": "2026-01-01T00:01:00Z", "completed_at": "2026-01-01T00:02:00Z"}
_PATH_ARGS = ("project_dir", "profiles_dir", "log_path")


def build_run_results() -> dict[str, Any]:
    """`dbt build` a temporary copy of the example project and return run_results.json.

    The profile's DuckDB path is relative to the directory dbt runs from, so the build
    runs from inside the copy.
    """
    from dbt.cli.main import dbtRunner

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / PROJECT_NAME
        shutil.copytree(
            PROJECT_DIR,
            project,
            ignore=shutil.ignore_patterns("target", "logs", "*.duckdb", "*.duckdb.wal"),
        )
        previous = os.getcwd()
        os.chdir(project)
        try:
            result = dbtRunner().invoke(
                ["build", "--project-dir", str(project), "--profiles-dir", str(project), "--quiet"]
            )
        finally:
            os.chdir(previous)
        if result.exception is not None:
            raise RuntimeError(f"dbt build failed: {result.exception}")
        return json.loads((project / "target" / "run_results.json").read_text(encoding="utf-8"))


def scrub_run_results(run_results: dict[str, Any]) -> dict[str, Any]:
    run_results["metadata"].update(FROZEN_RUN_METADATA)
    run_results["elapsed_time"] = 300.0
    args = run_results["args"]
    for key in _PATH_ARGS:
        if key in args:
            args[key] = "."
    args["invocation_command"] = "dbt build"
    args["send_anonymous_usage_stats"] = False
    results = []
    # dbt's threads finish in any order.
    for result in sorted(run_results["results"], key=lambda r: r["unique_id"]):
        timing = []
        for entry in result["timing"]:
            timing.append({**entry, **FROZEN_TIMING})
        result["timing"] = timing
        result["execution_time"] = 60.0
        result["thread_id"] = "Thread-1 (worker)"
        results.append(result)
    run_results["results"] = results
    return run_results


def frozen_run_results() -> dict[str, Any]:
    return scrub_run_results(build_run_results())


# Fixed values for the freshness run (a day after the frozen build).
FROZEN_SOURCES_METADATA = {
    "invocation_id": "00000000-0000-4000-8000-000000000002",
    "invocation_started_at": "2026-01-02T00:00:00Z",
    "generated_at": "2026-01-02T00:01:00Z",
    "env": {},
}
# The check is frozen one week after the source's latest load (fixed seed data), so its
# age and its `warn` status agree.
FROZEN_SOURCE_AGE_SECONDS = 7 * 24 * 3600.0
FROZEN_SOURCE_TIMING = {
    "compile": {"started_at": "2026-01-02T00:00:10Z", "completed_at": "2026-01-02T00:00:11Z"},
    "execute": {"started_at": "2026-01-02T00:00:11Z", "completed_at": "2026-01-02T00:00:15Z"},
}


def check_source_freshness() -> dict[str, Any]:
    """`dbt seed` then `dbt source freshness` a temporary copy of the example project, and
    return sources.json. (The example's source is its raw_orders seed.)"""
    from dbt.cli.main import dbtRunner

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / PROJECT_NAME
        shutil.copytree(
            PROJECT_DIR,
            project,
            ignore=shutil.ignore_patterns("target", "logs", "*.duckdb", "*.duckdb.wal"),
        )
        previous = os.getcwd()
        os.chdir(project)
        try:
            dirs = ["--project-dir", str(project), "--profiles-dir", str(project), "--quiet"]
            seeded = dbtRunner().invoke(["seed", *dirs])
            if not seeded.success:
                raise RuntimeError(f"dbt seed failed: {seeded.exception}")
            result = dbtRunner().invoke(["source", "freshness", *dirs])
        finally:
            os.chdir(previous)
        if result.exception is not None:
            raise RuntimeError(f"dbt source freshness failed: {result.exception}")
        return json.loads((project / "target" / "sources.json").read_text(encoding="utf-8"))


def scrub_sources(sources: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    sources["metadata"].update(FROZEN_SOURCES_METADATA)
    sources["elapsed_time"] = 20.0
    results = []
    for result in sorted(sources["results"], key=lambda r: r["unique_id"]):
        timing = []
        for entry in result.get("timing", []):
            timing.append({**entry, **FROZEN_SOURCE_TIMING.get(entry["name"], {})})
        result["timing"] = timing
        result["execution_time"] = 5.0
        result["thread_id"] = "Thread-1 (worker)"
        # When the check ran depends on when this script ran: freeze it a week after the
        # latest load instead.
        loaded = datetime.fromisoformat(result["max_loaded_at"].replace("Z", "+00:00"))
        if loaded.tzinfo is None:
            loaded = loaded.replace(tzinfo=timezone.utc)
        snapshotted = loaded + timedelta(seconds=FROZEN_SOURCE_AGE_SECONDS)
        result["snapshotted_at"] = snapshotted.strftime("%Y-%m-%dT%H:%M:%SZ")
        result["max_loaded_at_time_ago_in_s"] = FROZEN_SOURCE_AGE_SECONDS
        results.append(result)
    sources["results"] = results
    return sources


def frozen_sources() -> dict[str, Any]:
    return scrub_sources(check_source_freshness())


def dumps(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=1, sort_keys=True) + "\n"


def main() -> None:
    # Parsing writes partial-parse state under the project; keep the example clean.
    shutil.rmtree(PROJECT_DIR / "logs", ignore_errors=True)
    FROZEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    FROZEN_PATH.write_text(dumps(frozen_manifest()), encoding="utf-8", newline="\n")
    print(f"wrote {FROZEN_PATH.relative_to(REPO)} ({FROZEN_PATH.stat().st_size // 1024} KiB)")
    FROZEN_RUN_RESULTS_PATH.write_text(dumps(frozen_run_results()), encoding="utf-8", newline="\n")
    size = FROZEN_RUN_RESULTS_PATH.stat().st_size // 1024
    print(f"wrote {FROZEN_RUN_RESULTS_PATH.relative_to(REPO)} ({size} KiB)")
    FROZEN_SOURCES_PATH.write_text(dumps(frozen_sources()), encoding="utf-8", newline="\n")
    size = FROZEN_SOURCES_PATH.stat().st_size // 1024
    print(f"wrote {FROZEN_SOURCES_PATH.relative_to(REPO)} ({size} KiB)")


if __name__ == "__main__":
    main()
