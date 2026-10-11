"""Reports and their renders (`dbtx.store.repositories.reports`, plan 5.4).

Behaviour (the caller owns the transaction; the project's nodes are synced first, as
`report` has a foreign key to `node`):

`sync(session, adapter, project, reports, seen_at)` (`reports`: `discover_reports`'
dict) → (stored, removed):
* one row per report: `relation`; `formats`; `template` = `template_path` or
  `template`; `config` = the ReportConfig as JSON; `config_hash` = sha256 of that JSON
  with sorted keys and no spaces
* the node sync's rules: first / last seen merge; the other columns and a cleared
  `removed_at` follow the newest manifest; a report missing from the newest manifest is
  marked removed (at the project's newest manifest time), and re-adding it clears that

Render lifecycle, one `report_render` row per report per invocation (times from
`dbtx.store.clock`):
* `insert_pending_renders(session, adapter, invocation_id, project, reports)`:
  `pending`, with the report's `config_hash`; doing it again changes nothing
* `mark_render_running(...)`: `running`, `started_at` now
* `mark_render_finished(..., status, detail, paths)`: `rendered` / `skipped` / `error`,
  `completed_at` now, the detail, and `paths` as {format: path}
* `render_paths([...])`: rendered files → {format (the suffix): path}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from dbt.contracts.graph.manifest import Manifest

from dbtx.reports import Report
from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store.loaders.manifest import manifest_rows
from dbtx.store.loaders.run_results import invocation_from_run_results
from dbtx.store.repositories import invocations
from dbtx.store.repositories import reports as report_repo
from dbtx.store.repositories.nodes import sync_manifest
from tests.conftest import FROZEN_RUN_RESULTS

from .conftest import CLOCK_START, FakeClock

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = T1 + timedelta(days=1)
T3 = T1 + timedelta(days=2)
RUN_ID = "00000000-0000-4000-8000-000000000001"
TOP = "exposure.jaffle_shop.top_customers"
MIX = "exposure.jaffle_shop.jaffle_mix"


def by_id(reports: dict[str, Report]) -> dict[str, Report]:
    """`discover_reports`' keys (the `reports` fixture keys them by name)."""
    return {report.exposure_id: report for report in reports.values()}


def sync_nodes(store: Store, manifest: Manifest, at: datetime) -> None:
    manifest.metadata.generated_at = at.replace(tzinfo=None)
    with store.begin() as session:
        sync_manifest(session, store.adapter, manifest_rows(manifest))


def sync(store: Store, manifest: Manifest, reports: dict[str, Report], at: datetime) -> tuple[int, int]:
    sync_nodes(store, manifest, at)
    with store.begin() as session:
        return report_repo.sync(session, store.adapter, "jaffle_shop", reports, at)


def report_row(store: Store, exposure_id: str) -> m.Report:
    with store.session() as session:
        return session.get(m.Report, ("jaffle_shop", exposure_id))


def render(store: Store, exposure_id: str) -> m.ReportRender:
    with store.session() as session:
        return session.get(m.ReportRender, (RUN_ID, "jaffle_shop", exposure_id))


@pytest.fixture
def project_store(store: Store) -> Store:
    with store.begin() as session:
        session.add(m.Project(project="jaffle_shop", folder="jaffle-shop", claimed_at=CLOCK_START, last_seen_at=CLOCK_START))
    return store


@pytest.fixture
def invocation_store(project_store: Store, manifest: Manifest, reports: dict[str, Report]) -> Store:
    """Nodes, reports and the frozen build's invocation stored."""
    sync(project_store, manifest, by_id(reports), T1)
    record = invocation_from_run_results(json.loads(FROZEN_RUN_RESULTS.read_text(encoding="utf-8")))
    with project_store.begin() as session:
        invocations.start(
            session, project_store.adapter, project="jaffle_shop", adapter_type="duckdb", record=record,
            run_by=None, host=None,
        )
    return project_store


# --------------------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------------------


def test_sync_stores_a_row_per_report(project_store: Store, manifest: Manifest, reports: dict[str, Report]) -> None:
    assert sync(project_store, manifest, by_id(reports), T1) == (6, 0)
    report = by_id(reports)[MIX]
    config = report.config.model_dump(mode="json")
    row = report_row(project_store, MIX)
    assert (row.relation, row.formats, row.template, row.config) == (
        report.relation, ["csv", "html"], "exposure_templates/jaffle_mix.html", config
    )
    expected_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert row.config_hash == expected_hash
    assert (row.first_seen_at, row.last_seen_at, row.removed_at) == (T1, T1, None)
    assert report_row(project_store, TOP).template == "table"


def test_a_changed_config_is_stored_by_the_newer_sync(
    project_store: Store, manifest: Manifest, reports: dict[str, Report]
) -> None:
    sync(project_store, manifest, by_id(reports), T1)
    first_hash = report_row(project_store, TOP).config_hash
    changed = by_id(reports)
    changed[TOP] = replace(changed[TOP], config=changed[TOP].config.model_copy(update={"limit": 5}))
    sync(project_store, manifest, changed, T2)
    row = report_row(project_store, TOP)
    assert row.config["limit"] == 5
    assert row.config_hash != first_hash
    assert (row.first_seen_at, row.last_seen_at) == (T1, T2)


def test_an_older_sync_loaded_later_keeps_the_newest_config(
    project_store: Store, manifest: Manifest, reports: dict[str, Report]
) -> None:
    changed = by_id(reports)
    changed[TOP] = replace(changed[TOP], config=changed[TOP].config.model_copy(update={"limit": 5}))
    sync(project_store, manifest, changed, T2)
    sync(project_store, manifest, by_id(reports), T1)
    row = report_row(project_store, TOP)
    assert row.config["limit"] == 5
    assert (row.first_seen_at, row.last_seen_at) == (T1, T2)


def test_a_report_missing_from_the_next_sync_is_removed_and_can_come_back(
    project_store: Store, manifest: Manifest, reports: dict[str, Report]
) -> None:
    sync(project_store, manifest, by_id(reports), T1)
    without_top = by_id(reports)
    del without_top[TOP]
    assert sync(project_store, manifest, without_top, T2) == (5, 1)
    assert report_row(project_store, TOP).removed_at == T2
    assert report_row(project_store, MIX).removed_at is None
    sync(project_store, manifest, by_id(reports), T3)
    assert report_row(project_store, TOP).removed_at is None


# --------------------------------------------------------------------------------------
# Renders
# --------------------------------------------------------------------------------------


def test_pending_renders_for_every_report(invocation_store: Store, reports: dict[str, Report]) -> None:
    with invocation_store.begin() as session:
        report_repo.insert_pending_renders(session, invocation_store.adapter, RUN_ID, "jaffle_shop", by_id(reports))
    with invocation_store.begin() as session:
        report_repo.insert_pending_renders(session, invocation_store.adapter, RUN_ID, "jaffle_shop", by_id(reports))
    with invocation_store.session() as session:
        rows = session.scalars(sa.select(m.ReportRender)).all()
    assert len(rows) == 6
    assert {row.status for row in rows} == {"pending"}
    assert render(invocation_store, MIX).config_hash == report_row(invocation_store, MIX).config_hash
    assert (render(invocation_store, MIX).started_at, render(invocation_store, MIX).completed_at) == (None, None)


def test_a_render_runs_then_finishes(invocation_store: Store, reports: dict[str, Report], clock: FakeClock) -> None:
    with invocation_store.begin() as session:
        report_repo.insert_pending_renders(session, invocation_store.adapter, RUN_ID, "jaffle_shop", by_id(reports))
    started = clock.advance(seconds=10)
    with invocation_store.begin() as session:
        report_repo.mark_render_running(session, RUN_ID, "jaffle_shop", MIX)
    assert (render(invocation_store, MIX).status, render(invocation_store, MIX).started_at) == ("running", started)
    completed = clock.advance(seconds=5)
    paths = {"csv": "target/reports/jaffle_mix.csv", "html": "target/reports/jaffle_mix.html"}
    with invocation_store.begin() as session:
        report_repo.mark_render_finished(session, RUN_ID, "jaffle_shop", MIX, status="rendered", detail="", paths=paths)
    row = render(invocation_store, MIX)
    assert (row.status, row.started_at, row.completed_at, row.detail, row.paths) == (
        "rendered", started, completed, "", paths
    )


@pytest.mark.parametrize("status, detail", [("skipped", "test unique_customers failed"), ("error", "no such column")])
def test_a_render_that_did_not_render(
    invocation_store: Store, reports: dict[str, Report], status: str, detail: str
) -> None:
    with invocation_store.begin() as session:
        report_repo.insert_pending_renders(session, invocation_store.adapter, RUN_ID, "jaffle_shop", by_id(reports))
        report_repo.mark_render_finished(session, RUN_ID, "jaffle_shop", TOP, status=status, detail=detail, paths=None)
    row = render(invocation_store, TOP)
    assert (row.status, row.detail, row.paths, row.completed_at) == (status, detail, None, CLOCK_START)


def test_render_paths() -> None:
    paths = [Path("target/reports/jaffle_mix.csv"), Path("target/reports/jaffle_mix.html")]
    assert report_repo.render_paths(paths) == {
        "csv": "target/reports/jaffle_mix.csv",
        "html": "target/reports/jaffle_mix.html",
    }
