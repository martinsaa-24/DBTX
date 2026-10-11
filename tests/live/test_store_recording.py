"""The run history store, end to end (plan Phase 5): real dbt runs of the example
project on a platform whose `dbtx.conf` sets `DB_URI=sqlite:///.dbtx/dbtx.db`.

Each test asserts what the store holds after `dbtx` returns, against the artifacts dbt
wrote for the same run. The dbt internals the recorder depends on (plan risk table:
`MainReportVersion` first, `ConcurrencyLine.target_name`, the artifact serialisers,
the manifest rewritten at the end of the build) are all checked here, so a dbt upgrade
that breaks one fails this file rather than storing wrong data.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.cli import main
from dbtx.store import models as m
from dbtx.store import recorder
from tests.live.conftest import NO_SEEDS, Project, chdir, copy_project

REPORTS = {
    "exposure.jaffle_shop.beverage_leaderboard",
    "exposure.jaffle_shop.customer_leaderboard",
    "exposure.jaffle_shop.jaffle_mix",
    "exposure.jaffle_shop.store_revenue_monthly",
    "exposure.jaffle_shop.store_scorecard",
    "exposure.jaffle_shop.top_customers",
}


class PlatformProject:
    """The built example project as `jaffle-shop` on a throwaway platform root."""

    def __init__(self, root: Path, project: Project) -> None:
        self.root = root
        self.project = project
        self.target = project.root / "target"
        self.engine = sa.create_engine(f"sqlite:///{root / '.dbtx' / 'dbtx.db'}")

    def dbtx(self, *args: str) -> int:
        # From the project folder, so the profile's relative DuckDB path is the built
        # warehouse; the platform is named with --platform-dir (plan 6.5.1 rule 1).
        with chdir(self.project.root):
            return main([*args, "--no-use-colors", "--platform-dir", str(self.root)])

    def session(self) -> Session:
        return Session(self.engine)

    def count(self, model: type) -> int:
        with self.session() as session:
            return session.scalar(sa.select(sa.func.count()).select_from(model))

    def invocations(self) -> list[m.Invocation]:
        with self.session() as session:
            return list(session.scalars(sa.select(m.Invocation).order_by(m.Invocation.start_time)))

    def rows(self, model: type) -> list[tuple[str, ...]]:
        with self.session() as session:
            return sorted(tuple(str(v) for v in row) for row in session.execute(sa.select(model.__table__)))

    def artifact(self, name: str) -> dict[str, Any]:
        return json.loads((self.target / name).read_text(encoding="utf-8"))


@pytest.fixture
def platform(built_project: Project, tmp_path: Path) -> Iterator[PlatformProject]:
    root = tmp_path / "platform"
    root.mkdir()
    (root / "dbtx.conf").write_text("DB_URI=sqlite:///.dbtx/dbtx.db\n", encoding="utf-8")
    project = copy_project(built_project.root, root / "jaffle-shop")
    project.clear_reports()
    made = PlatformProject(root, project)
    yield made
    made.engine.dispose()


# --------------------------------------------------------------------------------------
# A build
# --------------------------------------------------------------------------------------


def test_a_build_is_recorded(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("build", *NO_SEEDS) == 0
    assert "run history not recorded" not in capsys.readouterr().out

    run_results = platform.artifact("run_results.json")
    manifest = platform.artifact("manifest.json")
    [invocation] = platform.invocations()

    # The build's own invocation id, not the internal parse's (in manifest.json).
    assert invocation.invocation_id == run_results["metadata"]["invocation_id"]
    assert invocation.invocation_id != manifest["metadata"]["invocation_id"]
    assert (invocation.project, invocation.command) == ("jaffle_shop", "build")
    # The example has one test that warns (warn_high_frequency_customers).
    assert (invocation.result, invocation.error_message) == ("warning", None)
    assert invocation.target == "dev"  # no --target given: dbt's resolved target
    assert invocation.adapter_type == "duckdb"
    assert invocation.args == run_results["args"]
    assert invocation.manifest_hash == hashlib.sha256((platform.target / "manifest.json").read_bytes()).hexdigest()
    assert invocation.start_time < invocation.end_time
    assert invocation.run_by is not None and invocation.host is not None

    with platform.session() as session:
        result_ids = set(session.scalars(sa.select(m.NodeResult.unique_id)))
        statuses = dict(session.execute(sa.select(m.NodeResult.unique_id, m.NodeResult.status)).all())
        renders = list(session.scalars(sa.select(m.ReportRender)))
        report_ids = set(session.scalars(sa.select(m.Report.exposure_id)))
        claim = session.get(m.Project, "jaffle_shop")
    # A node result for every node dbt ran (the example has no hooks or operations).
    expected = {result["unique_id"]: result["status"] for result in run_results["results"]}
    assert result_ids == set(expected)
    assert statuses == expected

    assert report_ids == REPORTS  # the exposures with meta.dbtx.report in _exposures.yml
    assert {render.exposure_id for render in renders} == REPORTS
    for render in renders:
        assert render.status == "rendered", render.exposure_id
        assert render.started_at <= render.completed_at
        assert set(render.paths) <= {"html", "csv"} and "html" in render.paths
        for path in render.paths.values():
            assert Path(path).is_file()
    assert claim.folder == "jaffle-shop"


def test_a_failing_test_skips_renders_with_their_reason(
    platform: PlatformProject, capsys: pytest.CaptureFixture[str]
) -> None:
    assert platform.dbtx("build", *NO_SEEDS) == 0
    platform.project.write("tests/assert_no_customers.sql", "select * from {{ ref('customers') }}")
    assert platform.dbtx("build", *NO_SEEDS) == 1
    capsys.readouterr()

    first, second = platform.invocations()
    assert (first.result, second.result) == ("warning", "failure")
    with platform.session() as session:
        renders = {
            render.exposure_id: render
            for render in session.scalars(
                sa.select(m.ReportRender).where(m.ReportRender.invocation_id == second.invocation_id)
            )
        }
        failed = session.get(m.NodeResult, (second.invocation_id, "jaffle_shop", "test.jaffle_shop.assert_no_customers"))
    assert failed.status == "fail"
    for name in ("top_customers", "customer_leaderboard"):
        render = renders[f"exposure.jaffle_shop.{name}"]
        assert (render.status, render.detail, render.paths) == (
            "skipped", "upstream test fail: test.jaffle_shop.assert_no_customers", None
        )
    assert renders["exposure.jaffle_shop.jaffle_mix"].status == "rendered"


def test_recording_does_not_change_the_console_output(
    platform: PlatformProject, capsys: pytest.CaptureFixture[str]
) -> None:
    # Done when (plan Phase 5): the same output as without a store. Times and thread
    # order vary between runs, so lines are compared without them, sorted.
    args = ("build", "-s", "+exposure:top_customers", *NO_SEEDS)
    assert platform.dbtx(*args) == 0  # creates the store
    capsys.readouterr()

    assert platform.dbtx(*args, "--db-backend", "disabled") == 0
    without = capsys.readouterr()
    assert platform.dbtx(*args) == 0
    recorded = capsys.readouterr()

    assert normalised(recorded.out) == normalised(without.out)
    assert recorded.err == without.err == ""
    assert len(platform.invocations()) == 2  # the disabled run is not recorded


def normalised(output: str) -> list[str]:
    lines = []
    for line in output.splitlines():
        line = re.sub(r"^\d{2}:\d{2}:\d{2}\s+", "", line)
        line = re.sub(r"^\d+ of (\d+) ", r"<n> of \1 ", line)  # numbered in completion order
        line = re.sub(r"\s+\.*\s*\[", " ... [", line)  # padded to the number's width
        line = re.sub(r"\d+ hours \d+ minutes and \d+(\.\d+)? seconds", "<t>", line)
        line = re.sub(r"\d+(\.\d+)?s\b", "<t>", line)
        lines.append(line)
    return sorted(lines)


def test_a_store_error_leaves_the_build_alone(
    platform: PlatformProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # D5: once dbt has started, recording is best-effort.
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(recorder.invocations, "start", broken)
    assert platform.dbtx("build", "-s", "+exposure:top_customers", *NO_SEEDS) == 0
    out = capsys.readouterr().out
    assert "dbtx: run history not recorded" in out and "disk full" in out
    assert "RENDERED  top_customers" in out
    assert platform.project.report_files() == {"top_customers.html"}
    assert platform.count(m.Invocation) == 0


# --------------------------------------------------------------------------------------
# Clean, full-parse and failed-parse environments (plan 5.5)
# --------------------------------------------------------------------------------------


def test_a_full_reparse_adds_no_versions_and_removes_nothing(platform: PlatformProject) -> None:
    args = ("build", "--no-partial-parse", "-s", "stg_customers", *NO_SEEDS)
    assert platform.dbtx(*args) == 0
    versions = version_keys(platform)
    edges = platform.count(m.NodeEdge)
    assert platform.dbtx(*args) == 0
    # The same versions, seen again (last_seen_at moves on): no new checksum.
    assert version_keys(platform) == versions
    assert platform.count(m.NodeEdge) == edges
    with platform.session() as session:
        removed = session.scalar(sa.select(sa.func.count()).where(m.Node.removed_at.is_not(None)))
    assert removed == 0


def version_keys(platform: PlatformProject) -> list[tuple[str, str, str]]:
    with platform.session() as session:
        rows = session.execute(sa.select(m.NodeVersion.project, m.NodeVersion.unique_id, m.NodeVersion.checksum))
        return sorted(tuple(row) for row in rows)


def test_a_first_run_without_target_stores_every_node(platform: PlatformProject) -> None:
    shutil.rmtree(platform.target)
    assert platform.dbtx("build", "-s", "stg_customers", *NO_SEEDS) == 0
    manifest = platform.artifact("manifest.json")
    stored_types = {"model", "source", "snapshot", "seed", "exposure", "test", "unit_test"}
    expected = set()
    for collection in ("nodes", "sources", "exposures", "unit_tests"):
        for unique_id, node in manifest[collection].items():
            if node["resource_type"] in stored_types:
                expected.add(unique_id)
    with platform.session() as session:
        stored = set(session.scalars(sa.select(m.Node.unique_id)))
        removed = session.scalar(sa.select(sa.func.count()).where(m.Node.removed_at.is_not(None)))
    assert stored == expected
    assert removed == 0
    # Only the selection ran: the model and its tests.
    assert platform.count(m.NodeResult) == len(platform.artifact("run_results.json")["results"]) == 3


def test_a_failed_parse_is_recorded_and_touches_no_node(
    platform: PlatformProject, capsys: pytest.CaptureFixture[str]
) -> None:
    assert platform.dbtx("build", "-s", "stg_customers", *NO_SEEDS) == 0
    nodes, reports = platform.rows(m.Node), platform.rows(m.Report)

    platform.project.write("models/marts/_broken.yml", "models: [ {name: oops\n")
    assert platform.dbtx("build", "-s", "stg_customers", *NO_SEEDS) != 0
    capsys.readouterr()

    first, failed = platform.invocations()
    assert (failed.project, failed.command, failed.result) == ("jaffle_shop", "build", "failure")
    assert failed.error_message
    assert failed.manifest_hash is None
    assert (failed.target, failed.adapter_type) == ("dev", "duckdb")  # the adapter was registered
    with platform.session() as session:
        failed_results = session.scalar(
            sa.select(sa.func.count()).where(m.NodeResult.invocation_id == failed.invocation_id)
        )
    assert failed_results == 0
    assert platform.rows(m.Node) == nodes
    assert platform.rows(m.Report) == reports


def test_no_write_json_records_everything_but_the_manifest_hash(platform: PlatformProject) -> None:
    assert platform.dbtx("build", "--no-write-json", "-s", "+exposure:top_customers", *NO_SEEDS) == 0
    [invocation] = platform.invocations()
    assert invocation.manifest_hash is None
    assert invocation.result == "warning"  # the selection includes the test that warns
    assert platform.count(m.NodeResult) > 0
    with platform.session() as session:
        render = session.get(m.ReportRender, (invocation.invocation_id, "jaffle_shop", "exposure.jaffle_shop.top_customers"))
    assert render.status == "rendered"


# --------------------------------------------------------------------------------------
# Other recorded commands
# --------------------------------------------------------------------------------------


def test_a_pass_through_run_is_recorded(platform: PlatformProject) -> None:
    assert platform.dbtx("run", "-s", "stg_customers") == 0
    [invocation] = platform.invocations()
    run_results = platform.artifact("run_results.json")
    assert (invocation.command, invocation.result, invocation.target) == ("run", "success", "dev")
    assert invocation.invocation_id == run_results["metadata"]["invocation_id"]
    # dbt parsed in the same invocation, so its manifest.json is this run's: synced.
    assert platform.count(m.Node) > 0
    assert platform.count(m.NodeResult) == 1
    assert platform.count(m.Report) == 0  # reports are synced by dbtx build / compile


def test_a_compile_is_recorded_without_renders(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("compile", "-s", "stg_customers") == 0
    capsys.readouterr()
    [invocation] = platform.invocations()
    assert (invocation.command, invocation.result) == ("compile", "success")
    assert platform.count(m.Report) == len(REPORTS)
    assert platform.count(m.ReportRender) == 0


def test_a_parse_takes_its_target_from_the_adapter(platform: PlatformProject) -> None:
    # parse fires no ConcurrencyLine (plan 5.2).
    assert platform.dbtx("parse") == 0
    [invocation] = platform.invocations()
    assert (invocation.command, invocation.target, invocation.adapter_type) == ("parse", "dev", "duckdb")
    assert platform.count(m.Node) > 0


def test_ls_is_not_recorded(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("ls", "-s", "stg_customers", "--quiet") == 0
    capsys.readouterr()
    assert not (platform.root / ".dbtx" / "dbtx.db").exists()
