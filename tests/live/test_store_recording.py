"""The run history store and isolated runs, end to end (plan Phases 5 and 6): real dbt
runs of the example project on a platform whose `dbtx.conf` sets
`DB_URI=sqlite:///.dbtx/dbtx.db` and `LOG_ROOT_LOCATION=.dbtx/logs`.

Each test asserts what the store holds after `dbtx` returns, against the artifacts dbt
wrote for the same run (in the run's own folder, which the store records). The dbt internals the recorder depends on (plan risk table:
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from dbtx.cli import main
from dbtx.config import load_config
from dbtx.docs import store_history
from dbtx.docs.server import create_app
from dbtx.docs.store_history import HistoryCache
from dbtx.store import models as m
from dbtx.store import open_store
from dbtx.store import recorder
from dbtx.store.loaders.run_results import parse_timestamp
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

    def run_folder(self) -> Path:
        """The latest recorded run's folder."""
        latest = self.invocations()[-1]
        assert latest.run_folder is not None
        return Path(latest.run_folder)

    def artifact(self, name: str) -> dict[str, Any]:
        """An artifact of the latest recorded run, from its run folder."""
        return json.loads((self.run_folder() / name).read_text(encoding="utf-8"))


@pytest.fixture
def platform(built_project: Project, tmp_path: Path) -> Iterator[PlatformProject]:
    root = tmp_path / "platform"
    root.mkdir()
    (root / "dbtx.conf").write_text(
        "DB_URI=sqlite:///.dbtx/dbtx.db\nLOG_ROOT_LOCATION=.dbtx/logs\n", encoding="utf-8"
    )
    project = copy_project(built_project.root, root / "jaffle-shop")
    project.clear_reports()
    # Once, as a user does: only `dbtx db init` / `upgrade` change the schema.
    with chdir(root):
        assert main(["db", "init"]) == 0
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
    run_folder = platform.run_folder()
    assert invocation.manifest_hash == hashlib.sha256((run_folder / "manifest.json").read_bytes()).hexdigest()
    # dbt's own start time, exactly as dbt writes it to run_results.json.
    assert invocation.start_time == parse_timestamp(run_results["metadata"]["invocation_started_at"])
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
            assert Path(path).parent == run_folder / "reports"
    assert claim.folder == "jaffle-shop"


def test_a_run_has_its_own_folder_and_job_id(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    before = datetime.now(timezone.utc)
    assert platform.dbtx("build", "-s", "+exposure:top_customers", *NO_SEEDS) == 0
    capsys.readouterr()
    [invocation] = platform.invocations()
    run_folder = platform.run_folder()

    # <LOG_ROOT_LOCATION>/<project>/<date>/<hour>/<job id>, the job id generated.
    assert run_folder.parent.parent.parent.parent == platform.root / ".dbtx" / "logs"
    assert run_folder.parent.parent.parent.name == "jaffle_shop"
    assert run_folder.parent.parent.name == before.strftime("%Y-%m-%d")
    assert run_folder.parent.name in (before.strftime("%H"), datetime.now(timezone.utc).strftime("%H"))
    assert run_folder.name == invocation.job_id
    # dbt wrote everything there, its log included, and recorded the job id it was given.
    names = {path.name for path in run_folder.iterdir()}
    assert {"run_results.json", "manifest.json", "dbt.log", "compiled", "run", "reports"} <= names
    assert platform.artifact("run_results.json")["metadata"]["env"] == {"DBTX_JOB_ID": invocation.job_id}
    with platform.session() as session:
        job = session.get(m.Job, invocation.job_id)
    assert (job.trigger, job.status) == ("external", "finished")
    # Nothing of the run in the project's own target folder but the docs site's reports.
    assert not (platform.target / "run_results.json").exists() or (
        (platform.target / "run_results.json").stat().st_mtime < before.timestamp()
    )


def test_a_given_job_id_names_the_run(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("build", "-s", "stg_customers", "--job-id", "nightly-42", *NO_SEEDS) == 0
    capsys.readouterr()
    [invocation] = platform.invocations()
    assert invocation.job_id == "nightly-42"
    assert platform.run_folder().name == "nightly-42"


def test_a_reused_job_id_is_refused(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("build", "-s", "stg_customers", "--job-id", "nightly-42", *NO_SEEDS) == 0
    capsys.readouterr()
    assert platform.dbtx("build", "-s", "stg_customers", "--job-id", "nightly-42", *NO_SEEDS) == 2
    assert "job id 'nightly-42' has already run this hour" in capsys.readouterr().err
    assert len(platform.invocations()) == 1


def test_compiled_sql_and_relation_are_stored(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("build", "-s", "customers", *NO_SEEDS) == 0
    capsys.readouterr()
    [invocation] = platform.invocations()
    results = {result["unique_id"]: result for result in platform.artifact("run_results.json")["results"]}
    with platform.session() as session:
        model = session.get(m.NodeResult, (invocation.invocation_id, "jaffle_shop", "model.jaffle_shop.customers"))
    assert model.compiled_code == results["model.jaffle_shop.customers"]["compiled_code"]
    assert model.compiled_code.lstrip().startswith("with")
    assert model.relation_name == results["model.jaffle_shop.customers"]["relation_name"]
    assert model.relation_name.endswith('."customers"')


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
    assert platform.dbtx(*args) == 0  # claims the project name (a warning-free first run)
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
        # Each run has its own folder (a new job id).
        line = re.sub(r"/\S*/\.dbtx/logs/jaffle_shop/\d{4}-\d{2}-\d{2}/\d{2}/[^/\s]+/", "<run folder>/", line)
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
    # No project target folder: no warm cache, no docs site. dbt parses in full.
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


def stored_statuses(platform: PlatformProject, invocation_id: str) -> dict[str, str]:
    with platform.session() as session:
        rows = session.execute(
            sa.select(m.NodeResult.unique_id, m.NodeResult.status).where(m.NodeResult.invocation_id == invocation_id)
        ).all()
    return dict(rows)


def test_source_freshness_is_recorded(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    # The example's source is its raw_orders seed, always stale: `warn`.
    assert platform.dbtx("source", "freshness") == 0
    out = capsys.readouterr().out
    [invocation] = platform.invocations()
    sources = platform.artifact("sources.json")
    assert (invocation.command, invocation.result, invocation.target) == ("source freshness", "warning", "dev")
    assert invocation.invocation_id == sources["metadata"]["invocation_id"]
    assert stored_statuses(platform, invocation.invocation_id) == {
        result["unique_id"]: result["status"] for result in sources["results"]
    } == {"source.jaffle_shop.jaffle_raw.raw_orders": "warn"}
    assert "dbtx docs:" not in out  # freshness does not patch the docs site


def test_run_operation_is_recorded(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("run-operation", "log_store_count") == 0
    out = capsys.readouterr().out
    assert "jaffle_shop: 6 stores" in out
    [invocation] = platform.invocations()
    assert (invocation.command, invocation.result, invocation.error_message) == ("run-operation", "success", None)
    # dbt hands the operation back as an artifact, not results; and an operation is not
    # a stored node: no node results. The project's nodes come from its manifest.
    assert stored_statuses(platform, invocation.invocation_id) == {}
    assert platform.count(m.Node) == 51
    assert "dbtx docs:" not in out


def test_run_operation_failure_is_recorded(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("run-operation", "no_such_macro") != 0
    capsys.readouterr()
    [invocation] = platform.invocations()
    assert (invocation.command, invocation.result) == ("run-operation", "failure")
    assert "no_such_macro" in invocation.error_message


def test_snapshot_is_recorded(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("snapshot") == 0
    capsys.readouterr()
    [invocation] = platform.invocations()
    run_results = platform.artifact("run_results.json")
    assert (invocation.command, invocation.result) == ("snapshot", "success")
    assert stored_statuses(platform, invocation.invocation_id) == {
        result["unique_id"]: result["status"] for result in run_results["results"]
    } == {"snapshot.jaffle_shop.stores_snapshot": "success"}
    with platform.session() as session:
        snapshot = session.get(m.NodeResult, (invocation.invocation_id, "jaffle_shop", "snapshot.jaffle_shop.stores_snapshot"))
    assert snapshot.relation_name.endswith('."stores_snapshot"')


@pytest.mark.parametrize(
    "argv, command",
    [(("seed", "-s", "raw_stores"), "seed"), (("test", "-s", "stg_customers"), "test")],
)
def test_seed_and_test_are_recorded_and_patch_the_docs_site(
    platform: PlatformProject, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], command: str
) -> None:
    assert platform.dbtx("docs", "generate") == 0
    assert platform.dbtx("docs", "install") == 0
    capsys.readouterr()

    assert platform.dbtx(*argv) == 0
    assert f"dbtx docs: patched the docs site {platform.target} with this run" in capsys.readouterr().out
    latest = platform.invocations()[-1]
    run_results = platform.artifact("run_results.json")
    assert (latest.command, latest.result) == (command, "success")
    expected = {result["unique_id"]: result["status"] for result in run_results["results"]}
    assert stored_statuses(platform, latest.invocation_id) == expected
    sidecar = json.loads((platform.target / "dbtx_runtime.json").read_text(encoding="utf-8"))
    for unique_id in expected:
        assert sidecar["nodes"][unique_id]["runs"][0]["job_id"] == latest.job_id


def test_ls_is_not_recorded(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    assert platform.dbtx("ls", "-s", "stg_customers", "--quiet") == 0
    capsys.readouterr()
    assert platform.count(m.Invocation) == 0
    assert not (platform.root / ".dbtx" / "logs").exists()  # no run folder either


# --------------------------------------------------------------------------------------
# The warm cache and the docs site (plan Phase 6)
# --------------------------------------------------------------------------------------


def test_a_warm_cache_is_used_by_the_run(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(platform.target)
    assert platform.dbtx("warm-cache") == 0
    # Only the cache: no manifest.json in the project's target folder (the docs site).
    assert (platform.target / "partial_parse.msgpack").is_file()
    assert not (platform.target / "manifest.json").exists()
    capsys.readouterr()

    assert platform.dbtx("build", "-s", "stg_customers", *NO_SEEDS) == 0
    log = (platform.run_folder() / "dbt.log").read_text(encoding="utf-8")
    assert "Partial parsing enabled, no changes found, skipping parsing" in log
    assert platform.count(m.Invocation) == 1  # warm-cache itself is not recorded


def test_the_docs_site_is_kept_current(platform: PlatformProject, capsys: pytest.CaptureFixture[str]) -> None:
    site = platform.target
    assert platform.dbtx("docs", "generate") == 0
    assert {"index.html", "manifest.json", "catalog.json"} <= {path.name for path in site.iterdir()}
    assert "run `dbtx docs install`" in capsys.readouterr().out
    assert platform.dbtx("docs", "install") == 0
    assert (site / "dbtx_docs.js").is_file()

    assert platform.dbtx("build", "-s", "+exposure:top_customers", *NO_SEEDS) == 0
    assert f"dbtx docs: patched the docs site {site} with this run" in capsys.readouterr().out
    assert (site / "reports" / "top_customers.html").is_file()
    sidecar = json.loads((site / "dbtx_runtime.json").read_text(encoding="utf-8"))
    [latest] = sidecar["nodes"]["model.jaffle_shop.customers"]["runs"]
    assert latest["job_id"] == platform.invocations()[-1].job_id
    assert sidecar["nodes"]["exposure.jaffle_shop.top_customers"]["report"]


# --------------------------------------------------------------------------------------
# The docs server reads the run history from the database (plan Phase 7)
# --------------------------------------------------------------------------------------


def served_history(client: TestClient) -> dict[str, Any]:
    response = client.get("/dbtx_runtime.json?cb=1")
    assert response.status_code == 200
    return response.json()


def test_the_docs_server_serves_the_run_history_from_the_database(
    platform: PlatformProject, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `dbtx docs serve` builds in platform mode: the store from the platform's
    configuration, a HistoryCache for the docs site, the app with that history."""
    site = platform.target
    assert platform.dbtx("docs", "generate") == 0
    assert platform.dbtx("docs", "install") == 0
    assert platform.dbtx("build", "-s", "+exposure:top_customers", *NO_SEEDS) == 0
    capsys.readouterr()

    probe_time = [1000.0]
    monkeypatch.setattr(store_history, "monotonic", lambda: probe_time[0])
    with chdir(platform.root):
        config = load_config(["docs", "serve", "--project-dir", "jaffle-shop"])
    store = open_store(config)
    try:
        client = TestClient(create_app(site, history=HistoryCache(store, site)))
        served = served_history(client)

        # The same runs the sidecar holds for this build, with the database's own fields.
        sidecar = json.loads((site / "dbtx_runtime.json").read_text(encoding="utf-8"))
        ran = sorted(uid for uid, entry in sidecar["nodes"].items() if entry.get("runs"))
        assert sorted(uid for uid, entry in served["nodes"].items() if entry.get("runs")) == ran
        for unique_id in ran:
            [from_file] = sidecar["nodes"][unique_id]["runs"]
            [from_database] = served["nodes"][unique_id]["runs"]
            for key in ("status", "execution_time", "checksum", "run_invocation_id", "job_id", "stale"):
                assert from_database[key] == from_file[key], (unique_id, key)
            assert parse_timestamp(from_database["last_ran_at"]) == parse_timestamp(from_file["last_ran_at"])
        [customers] = served["nodes"]["model.jaffle_shop.customers"]["runs"]
        assert customers["updated_at"] is not None
        assert served["nodes"]["exposure.jaffle_shop.top_customers"]["report"]["html"] == "reports/top_customers.html"

        # A later build shows up once the probe interval has passed.
        assert platform.dbtx("build", "-s", "customers", *NO_SEEDS) == 0
        capsys.readouterr()
        probe_time[0] += store_history.PROBE_INTERVAL_SECONDS
        runs = served_history(client)["nodes"]["model.jaffle_shop.customers"]["runs"]
        assert [run["run_invocation_id"] for run in runs] == [
            platform.invocations()[-1].invocation_id,
            customers["run_invocation_id"],
        ]
    finally:
        store.dispose()
