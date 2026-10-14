"""The recorder (plan 5.7): one dbt invocation that dbtx runs → the store.

Behaviour:

`Recorder.for_invocation(config)`:
* a `NullRecorder` (records nothing, opens no store) without a config, in standalone
  mode, with the backend disabled, for a command that is not recorded (`ls`, `db load`,
  ...: plan Q3), or when the project has no readable name (plan 5.5)
* otherwise a `Recorder` for the project, with the stored command (`source
  snapshot-freshness` → `source freshness`); its store is opened, not connected

Lifecycle, driven by dbt's events (`on_event`, a dbtRunner callback):
* no event (dbt refused the command line before it started) → nothing is recorded
* `MainReportVersion` → the invocation starts, in one transaction: its id from the
  event; its start time from dbt; command, project, dbt version, args (dbt's flags,
  secrets scrubbed), selector, job id (an unknown one becomes an `external` job),
  `run_by`, `host`; no end time or result yet. A platform run's job id and run folder
  come from the configuration (dbtx chose them), never from the args. With a manifest (`use_manifest`, given
  by `build` / `compile` before dbt runs): the manifest sync, the report sync and a
  `pending` render row for each report this run renders, so the render rows' foreign
  keys hold
* `ConcurrencyLine` → the target; `CommandCompleted` → the registered adapter and the
  final flags (equal to the artifact's `args`)
* `report_changed(exposure_id, outcome)` (the scheduler's observer) → the render row:
  `running` sets `started_at`; `rendered` / `skipped` / `error` set `completed_at`,
  `detail` and `paths` ({format: path}); a report not in this run, or no started
  invocation → ignored
* `finish(result)` → in one transaction: the node results from dbt's own artifact
  serialisation; target (`ConcurrencyLine`, else the registered adapter, else
  `'unknown'` with a warning), adapter type (the manifest, else the registered
  adapter, else `'unknown'`) and the final args; end time; the result from the node
  statuses, or `failure` with dbt's exception as `error_message`; `manifest_hash`, the
  sha256 of the manifest this invocation wrote: the file named by its last
  `ArtifactWritten` event for a `WritableManifest`. No such event (a failed parse,
  `--no-write-json`, a crash) → NULL, whatever `manifest.json` is on disk
* without a manifest from dbtx (pass-through commands): the manifest is the one this
  invocation produced: dbt's parse result, or `<target>/manifest.json` whose
  `metadata.invocation_id` is this invocation's. Otherwise (a failed parse, a stale
  file) there is **no** manifest, node or report sync, so nothing is marked removed

Best-effort (D5): a store error at any step is logged once through dbt's events at WARN
and never raised; the recorder then stops writing. In strict mode (D9) an unknown
app-enforced value is raised instead.

Helpers that read dbt's state, each replaced by the `dbt` fixture elsewhere:
`result_artifact(result)` (dbt's serialisers), `registered_adapter()`, `flags_args()`.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import sqlalchemy as sa
from dbt.contracts.graph.manifest import Manifest
from dbt.version import __version__ as DBT_VERSION

from dbtx.config import DbtxConfig, Setting
from dbtx.reports import Report
from dbtx.scheduler import Outcome
from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store import recorder
from dbtx.store.recorder import NullRecorder, Recorder, RegisteredAdapter
from dbtx.store.repositories.projects import claim_project
from dbtx.store.validation import UnknownValueError
from tests.conftest import FROZEN_MANIFEST, FROZEN_RUN_RESULTS

from .conftest import CLOCK_START, ON_SQLITE_FILE_AND_POSTGRES, TEST_HOST, FakeClock, store_config

PROJECT = "jaffle_shop"
# The frozen run results' invocation.
RUN_ID = "00000000-0000-4000-8000-000000000001"
OTHER_RUN_ID = "00000000-0000-4000-8000-000000000009"
STARTED = datetime(2026, 1, 1, tzinfo=timezone.utc)
GENERATED = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
TOP_CUSTOMERS = "exposure.jaffle_shop.top_customers"


# --------------------------------------------------------------------------------------
# Test fixtures
# --------------------------------------------------------------------------------------


@dataclass
class FakeDbt:
    """dbt's state as the recorder reads it, through the functions this replaces."""

    target_path: Path
    started_at: datetime = STARTED
    args: dict[str, Any] = field(
        default_factory=lambda: {"which": "build", "select": ["customers"], "vars": {}}
    )
    adapter: Optional[RegisteredAdapter] = None
    artifact: Optional[tuple[str, dict[str, Any]]] = None
    warnings: list[str] = field(default_factory=list)

    def register(self, type: str = "duckdb", target: str = "dev") -> None:
        self.adapter = RegisteredAdapter(type=type, target=target, target_path=self.target_path)


@pytest.fixture
def dbt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDbt:
    fake = FakeDbt(target_path=tmp_path / "jaffle-shop" / "target")
    monkeypatch.setattr(recorder, "invocation_started_at", lambda: fake.started_at)
    monkeypatch.setattr(recorder, "flags_args", lambda: copy.deepcopy(fake.args))
    monkeypatch.setattr(recorder, "registered_adapter", lambda: fake.adapter)
    monkeypatch.setattr(recorder, "result_artifact", lambda result: fake.artifact)
    monkeypatch.setattr(recorder, "current_user", lambda: "analyst")

    def fire_event(event: Any, level: Any = None) -> None:
        fake.warnings.append(f"{level.value}: {event.msg}")

    monkeypatch.setattr(recorder, "fire_event", fire_event)
    return fake


def recorded_config(store: Store, tmp_path: Path, command: str = "build") -> DbtxConfig:
    """The config `cli.main` hands on for a recorded command of the project."""
    return replace(
        store_config(store.uri, store.schema),
        command=command,
        project_name=PROJECT,
        project_dir=tmp_path / "jaffle-shop",
    )


@pytest.fixture
def claimed(store: Store) -> Store:
    """The store, with the project claimed, as the start-up checks leave it."""
    with store.begin() as session:
        claim_project(session, store.adapter, PROJECT, "jaffle-shop")
    return store


def new_recorder(store: Store, tmp_path: Path, command: str = "build") -> Recorder:
    made = Recorder.for_invocation(recorded_config(store, tmp_path, command))
    assert isinstance(made, Recorder)
    return made


@pytest.fixture
def frozen_manifest(manifest: Manifest) -> Manifest:
    manifest.metadata.generated_at = GENERATED.replace(tzinfo=None)
    return manifest


def frozen_run_results() -> dict[str, Any]:
    return json.loads(FROZEN_RUN_RESULTS.read_text(encoding="utf-8"))


def event(name: str, invocation_id: str = RUN_ID, **data: Any) -> SimpleNamespace:
    return SimpleNamespace(
        info=SimpleNamespace(name=name, invocation_id=invocation_id), data=SimpleNamespace(**data)
    )


def run_dbt(rec: Recorder, target: Optional[str] = "dev", invocation_id: str = RUN_ID) -> None:
    """The events dbt fires for one invocation, in dbt's order."""
    rec.on_event(event("MainReportVersion", invocation_id))
    rec.on_event(event("MainReportArgs", invocation_id))
    if target is not None:
        rec.on_event(event("ConcurrencyLine", invocation_id, target_name=target))
    rec.on_event(event("NodeFinished", invocation_id))
    rec.on_event(event("CommandCompleted", invocation_id))


def announce_manifest(rec: Recorder, path: Path, invocation_id: str = RUN_ID) -> None:
    """dbt's event for a manifest it has just written."""
    rec.on_event(event("ArtifactWritten", invocation_id, artifact_type="WritableManifest", artifact_path=str(path)))


def dbt_result(success: bool = True, exception: Optional[BaseException] = None, result: Any = None) -> SimpleNamespace:
    return SimpleNamespace(success=success, exception=exception, result=result)


def invocation(store: Store, invocation_id: str = RUN_ID) -> Optional[m.Invocation]:
    with store.session() as session:
        return session.get(m.Invocation, invocation_id)


def count(store: Store, model: type) -> int:
    with store.session() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(model))


def render(store: Store, exposure_id: str = TOP_CUSTOMERS, invocation_id: str = RUN_ID) -> Optional[m.ReportRender]:
    with store.session() as session:
        return session.get(m.ReportRender, (invocation_id, PROJECT, exposure_id))


def write_manifest_json(target: Path, invocation_id: str) -> Path:
    """The frozen manifest at `<target>/manifest.json`, as written by `invocation_id`."""
    target.mkdir(parents=True, exist_ok=True)
    data = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    data["metadata"]["invocation_id"] = invocation_id
    data["metadata"]["generated_at"] = "2025-12-31T23:59:00Z"
    path = target / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# Recorder.for_invocation: the gates
# --------------------------------------------------------------------------------------


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[DbtxConfig]:
    """Every config the recorder opens a store for."""
    configs: list[DbtxConfig] = []
    real_open_store = recorder.open_store

    def counting_open_store(config: DbtxConfig) -> Store:
        configs.append(config)
        return real_open_store(config)

    monkeypatch.setattr(recorder, "open_store", counting_open_store)
    return configs


def test_no_config_records_nothing(opened: list[DbtxConfig]) -> None:
    assert type(Recorder.for_invocation(None)) is NullRecorder
    assert opened == []


def test_standalone_mode_records_nothing(opened: list[DbtxConfig], tmp_path: Path) -> None:
    config = replace(
        store_config("sqlite:///x.db"),
        mode="standalone",
        db_backend=None,
        db_uri=None,
        db_schema=None,
        db_adapter=None,
        project_name=PROJECT,
    )
    assert type(Recorder.for_invocation(config)) is NullRecorder
    assert opened == []


def test_a_disabled_backend_records_nothing(opened: list[DbtxConfig]) -> None:
    config = replace(
        store_config("sqlite:///x.db"),
        db_backend=Setting("disabled", "cli", None),
        project_name=PROJECT,
    )
    assert type(Recorder.for_invocation(config)) is NullRecorder
    assert opened == []


@pytest.mark.parametrize("command", ["ls", "debug", "show", "docs serve", "db load", "templates", ""])
def test_a_command_that_is_not_recorded_records_nothing(opened: list[DbtxConfig], command: str) -> None:
    config = replace(store_config("sqlite:///x.db"), command=command, project_name=PROJECT)
    assert type(Recorder.for_invocation(config)) is NullRecorder
    assert opened == []


def test_a_project_without_a_name_records_nothing(opened: list[DbtxConfig]) -> None:
    config = replace(store_config("sqlite:///x.db"), command="build", project_name=None)
    assert type(Recorder.for_invocation(config)) is NullRecorder
    assert opened == []


@pytest.mark.parametrize(
    "command, stored",
    [("build", "build"), ("run", "run"), ("source snapshot-freshness", "source freshness"), ("docs generate", "docs generate")],
)
def test_a_recorded_command_gets_a_recorder(
    opened: list[DbtxConfig], tmp_path: Path, command: str, stored: str
) -> None:
    uri = f"sqlite:///{tmp_path / 'dbtx.db'}"
    config = replace(store_config(uri), command=command, project_name=PROJECT)
    made = Recorder.for_invocation(config)
    assert isinstance(made, Recorder)
    assert (made.project, made.command) == (PROJECT, stored)
    assert opened == [config]
    assert not (tmp_path / "dbtx.db").exists()  # opened, not connected
    made.finish(dbt_result())


# --------------------------------------------------------------------------------------
# Starting the invocation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_no_event_records_nothing(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    rec = new_recorder(claimed, tmp_path)
    rec.finish(dbt_result(success=False, exception=RuntimeError("No such option '--x'")))
    assert count(claimed, m.Invocation) == 0
    assert dbt.warnings == []


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_main_report_version_starts_the_invocation(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    rec = new_recorder(claimed, tmp_path)
    rec.on_event(event("MainReportVersion"))
    row = invocation(claimed)
    assert row is not None
    assert (row.project, row.command, row.start_time, row.end_time, row.result) == (
        PROJECT, "build", STARTED, None, None
    )
    assert (row.dbt_version, row.run_by, row.host) == (DBT_VERSION, "analyst", TEST_HOST)
    assert (row.args, row.selector, row.job_id) == ({"which": "build", "select": ["customers"], "vars": {}}, "customers", None)
    # Not known yet: dbt resolves them later in the invocation.
    assert (row.target, row.adapter_type) == ("unknown", "unknown")
    rec.finish(dbt_result())


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_job_id_var_records_an_external_job(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    dbt.args = {"which": "build", "vars": {"job_id": "nightly-42"}}
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec)
    rec.finish(dbt_result())
    with claimed.session() as session:
        job = session.get(m.Job, "nightly-42")
    assert invocation(claimed).job_id == "nightly-42"
    assert (job.trigger, job.status, job.project) == ("external", "finished", PROJECT)


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_platform_runs_take_the_job_id_and_run_folder_from_the_config(
    claimed: Store, dbt: FakeDbt, tmp_path: Path
) -> None:
    # dbtx chose the job id (and handed it to dbt); a legacy job_id var does not change it.
    dbt.args = {"which": "build", "vars": {"job_id": "legacy"}}
    folder = tmp_path / "runs" / "jaffle_shop" / "2026-10-12" / "14" / "nightly-42"
    config = replace(recorded_config(claimed, tmp_path), job_id="nightly-42", run_folder=folder)
    rec = Recorder.for_invocation(config)
    assert isinstance(rec, Recorder)
    run_dbt(rec)
    rec.finish(dbt_result())
    row = invocation(claimed)
    assert (row.job_id, row.run_folder) == ("nightly-42", str(folder))


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_manifest_is_synced_at_the_start(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest, reports: dict[str, Report]
) -> None:
    by_id = {report.exposure_id: report for report in reports.values()}
    rec = new_recorder(claimed, tmp_path)
    rec.use_manifest(frozen_manifest, by_id, {TOP_CUSTOMERS: by_id[TOP_CUSTOMERS]})
    assert count(claimed, m.Node) == 0  # nothing is written before dbt starts
    rec.on_event(event("MainReportVersion"))
    assert (count(claimed, m.Node), count(claimed, m.NodeEdge), count(claimed, m.Report)) == (51, 54, 6)
    assert invocation(claimed).adapter_type == "duckdb"  # from the manifest
    pending = render(claimed)
    assert (pending.status, pending.started_at, pending.completed_at, pending.paths) == ("pending", None, None, None)
    assert count(claimed, m.ReportRender) == 1  # only the reports this run renders
    rec.finish(dbt_result())


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_second_main_report_version_is_ignored(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    rec = new_recorder(claimed, tmp_path)
    rec.on_event(event("MainReportVersion"))
    rec.on_event(event("MainReportVersion", OTHER_RUN_ID))
    assert count(claimed, m.Invocation) == 1
    rec.finish(dbt_result())


# --------------------------------------------------------------------------------------
# Finishing the invocation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_build_is_recorded_with_its_node_results(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest, clock: FakeClock
) -> None:
    dbt.artifact = ("run_results", frozen_run_results())
    dbt.register(target="prod")  # ConcurrencyLine wins over the adapter
    rec = new_recorder(claimed, tmp_path)
    rec.use_manifest(frozen_manifest, {}, {})
    rec.on_event(event("MainReportVersion"))
    rec.on_event(event("ConcurrencyLine", target_name="dev"))
    dbt.args = {"which": "build", "select": ["customers"], "vars": {}, "warn_error_options": {}}
    rec.on_event(event("CommandCompleted"))
    clock.advance(minutes=5)
    rec.finish(dbt_result(success=True, result=object()))

    row = invocation(claimed)
    assert (row.end_time, row.result, row.error_message) == (CLOCK_START.replace(minute=5), "warning", None)
    assert (row.target, row.adapter_type) == ("dev", "duckdb")
    assert row.args == {"which": "build", "select": ["customers"], "vars": {}, "warn_error_options": {}}
    assert count(claimed, m.NodeResult) == 49
    with claimed.session() as session:
        result = session.get(m.NodeResult, (RUN_ID, PROJECT, "model.jaffle_shop.customers"))
    assert (result.status, result.checksum) == ("success", frozen_manifest.nodes["model.jaffle_shop.customers"].checksum.checksum)
    assert dbt.warnings == []


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_the_target_falls_back_to_the_registered_adapter(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    # parse and run-operation fire no ConcurrencyLine (plan 5.2).
    dbt.register(type="postgres", target="ci")
    rec = new_recorder(claimed, tmp_path, command="parse")
    run_dbt(rec, target=None)
    rec.finish(dbt_result())
    row = invocation(claimed)
    assert (row.command, row.target, row.adapter_type) == ("parse", "ci", "postgres")
    assert dbt.warnings == []


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_no_target_anywhere_is_stored_as_unknown_with_a_warning(
    claimed: Store, dbt: FakeDbt, tmp_path: Path
) -> None:
    # dbt failed before loading the profile: no adapter was registered (plan 5.5).
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec, target=None)
    rec.finish(dbt_result(success=False, exception=RuntimeError("Could not find profile named 'jaffle_shop'")))
    row = invocation(claimed)
    assert (row.target, row.adapter_type) == ("unknown", "unknown")
    assert (row.result, row.error_message) == ("failure", "Could not find profile named 'jaffle_shop'")
    assert dbt.warnings == [
        f"warn: dbtx: invocation {RUN_ID}: dbt did not resolve a target; recorded as 'unknown'"
    ]


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_failure_without_an_exception_or_results_is_a_failure(
    claimed: Store, dbt: FakeDbt, tmp_path: Path
) -> None:
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec)
    rec.finish(dbt_result(success=False))
    assert (invocation(claimed).result, invocation(claimed).error_message) == ("failure", None)


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_failed_parse_touches_no_node_or_report(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest, reports: dict[str, Report]
) -> None:
    by_id = {report.exposure_id: report for report in reports.values()}
    earlier = new_recorder(claimed, tmp_path)
    earlier.use_manifest(frozen_manifest, by_id, {})
    run_dbt(earlier)
    earlier.finish(dbt_result())
    with claimed.session() as session:
        before = sorted(tuple(str(v) for v in row) for row in session.execute(sa.select(m.Node.__table__)))

    # A stale manifest.json from an earlier run is on disk: it must not be used.
    dbt.register()
    write_manifest_json(dbt.target_path, RUN_ID)
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec, invocation_id=OTHER_RUN_ID)
    rec.finish(dbt_result(success=False, exception=RuntimeError("Parsing Error")))

    row = invocation(claimed, OTHER_RUN_ID)
    assert (row.project, row.result, row.error_message, row.manifest_hash) == (PROJECT, "failure", "Parsing Error", None)
    with claimed.session() as session:
        after = sorted(tuple(str(v) for v in row) for row in session.execute(sa.select(m.Node.__table__)))
        removed_reports = session.scalar(sa.select(sa.func.count()).where(m.Report.removed_at.is_not(None)))
    assert after == before
    assert removed_reports == 0


# --------------------------------------------------------------------------------------
# manifest_hash and the pass-through manifest
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_manifest_hash_of_the_manifest_this_invocation_wrote(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest
) -> None:
    dbt.register()
    rec = new_recorder(claimed, tmp_path)
    rec.use_manifest(frozen_manifest, {}, {})
    run_dbt(rec)
    # dbt writes manifest.json at the end of the build and says so.
    path = write_manifest_json(dbt.target_path, OTHER_RUN_ID)
    announce_manifest(rec, path)
    rec.finish(dbt_result())
    assert invocation(claimed).manifest_hash == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_no_manifest_hash_when_this_invocation_wrote_no_manifest(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest
) -> None:
    # --no-write-json, a failed parse, a crash: dbt announces no manifest. The file on
    # disk belongs to another run, however recent it looks.
    dbt.register()
    write_manifest_json(dbt.target_path, OTHER_RUN_ID)
    rec = new_recorder(claimed, tmp_path)
    rec.use_manifest(frozen_manifest, {}, {})
    run_dbt(rec)
    rec.finish(dbt_result())
    assert invocation(claimed).manifest_hash is None


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_no_manifest_hash_from_another_artifact(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    dbt.register()
    write_manifest_json(dbt.target_path, OTHER_RUN_ID)
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec)
    rec.on_event(
        event("ArtifactWritten", artifact_type="RunExecutionResult", artifact_path=str(dbt.target_path / "run_results.json"))
    )
    rec.finish(dbt_result())
    assert invocation(claimed).manifest_hash is None


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_no_manifest_hash_when_the_written_file_is_gone(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    dbt.register()
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec)
    announce_manifest(rec, dbt.target_path / "manifest.json")
    rec.finish(dbt_result())
    assert invocation(claimed).manifest_hash is None


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_manifest_hash_needs_no_registered_adapter(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    # The event names the file; dbtx never has to work out where the target dir is.
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec)
    path = write_manifest_json(tmp_path / "elsewhere", OTHER_RUN_ID)
    announce_manifest(rec, path)
    rec.finish(dbt_result())
    assert invocation(claimed).manifest_hash == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_pass_through_syncs_the_manifest_it_wrote(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    dbt.register()
    dbt.artifact = ("run_results", frozen_run_results())
    rec = new_recorder(claimed, tmp_path, command="run")
    run_dbt(rec)
    path = write_manifest_json(dbt.target_path, RUN_ID)
    announce_manifest(rec, path)
    rec.finish(dbt_result(result=object()))
    assert (count(claimed, m.Node), count(claimed, m.NodeResult)) == (51, 49)
    assert count(claimed, m.Report) == 0  # reports are synced by dbtx's own build / compile
    assert invocation(claimed).manifest_hash == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_pass_through_ignores_another_invocations_manifest(claimed: Store, dbt: FakeDbt, tmp_path: Path) -> None:
    dbt.register()
    dbt.artifact = ("run_results", frozen_run_results())
    rec = new_recorder(claimed, tmp_path, command="run")
    run_dbt(rec)
    write_manifest_json(dbt.target_path, OTHER_RUN_ID)
    rec.finish(dbt_result(result=object()))
    # No manifest of this run: no nodes, so every node result is skipped.
    assert (count(claimed, m.Node), count(claimed, m.NodeResult)) == (0, 0)
    assert invocation(claimed).result == "warning"  # still from every status


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_parse_syncs_the_manifest_it_returned(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest
) -> None:
    rec = new_recorder(claimed, tmp_path, command="parse")
    run_dbt(rec, target=None)
    rec.finish(dbt_result(result=frozen_manifest))
    assert count(claimed, m.Node) == 51
    assert invocation(claimed).adapter_type == "duckdb"


# --------------------------------------------------------------------------------------
# Report renders (the scheduler's observer)
# --------------------------------------------------------------------------------------


@pytest.fixture
def rendering(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest, reports: dict[str, Report]
) -> Recorder:
    """A started build that renders top_customers and store_scorecard."""
    by_id = {report.exposure_id: report for report in reports.values()}
    renders = {}
    for name in ("top_customers", "store_scorecard"):
        renders[reports[name].exposure_id] = reports[name]
    rec = new_recorder(claimed, tmp_path)
    rec.use_manifest(frozen_manifest, by_id, renders)
    rec.on_event(event("MainReportVersion"))
    return rec


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_render_is_recorded_from_running_to_rendered(
    claimed: Store, rendering: Recorder, clock: FakeClock, tmp_path: Path
) -> None:
    rendering.report_changed(TOP_CUSTOMERS, Outcome("running"))
    running = render(claimed)
    assert (running.status, running.started_at, running.completed_at) == ("running", CLOCK_START, None)

    clock.advance(seconds=3)
    html, csv = tmp_path / "reports" / "top_customers.html", tmp_path / "reports" / "top_customers.csv"
    rendering.report_changed(TOP_CUSTOMERS, Outcome("rendered", paths=[html, csv]))
    done = render(claimed)
    assert (done.status, done.detail, done.started_at) == ("rendered", None, CLOCK_START)
    assert done.completed_at == CLOCK_START.replace(second=3)
    assert done.paths == {"html": str(html), "csv": str(csv)}
    rendering.finish(dbt_result())


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
@pytest.mark.parametrize("status", ["skipped", "error"])
def test_a_render_that_did_not_happen_keeps_its_reason(claimed: Store, rendering: Recorder, status: str) -> None:
    rendering.report_changed(TOP_CUSTOMERS, Outcome(status, "upstream test fail: test.x"))
    row = render(claimed)
    assert (row.status, row.detail, row.paths, row.completed_at) == (status, "upstream test fail: test.x", None, CLOCK_START)
    other = render(claimed, "exposure.jaffle_shop.store_scorecard")
    assert other.status == "pending"
    rendering.finish(dbt_result())


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_report_outside_this_run_is_ignored(claimed: Store, rendering: Recorder, dbt: FakeDbt) -> None:
    rendering.report_changed("exposure.jaffle_shop.jaffle_mix", Outcome("running"))
    assert render(claimed, "exposure.jaffle_shop.jaffle_mix") is None
    assert dbt.warnings == []
    rendering.finish(dbt_result())


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_render_change_before_the_start_is_ignored(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest, reports: dict[str, Report]
) -> None:
    rec = new_recorder(claimed, tmp_path)
    report = reports["top_customers"]
    rec.use_manifest(frozen_manifest, {TOP_CUSTOMERS: report}, {TOP_CUSTOMERS: report})
    rec.report_changed(TOP_CUSTOMERS, Outcome("running"))
    assert count(claimed, m.ReportRender) == 0
    assert dbt.warnings == []
    rec.finish(dbt_result())


# --------------------------------------------------------------------------------------
# Best-effort (D5) and strict mode (D9)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_store_error_at_the_start_is_a_warning_and_stops_recording(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest, reports: dict[str, Report],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise sa.exc.OperationalError("INSERT ...", {}, Exception("database is locked\nmore detail"))

    monkeypatch.setattr(recorder.invocations, "start", broken)
    report = reports["top_customers"]
    rec = new_recorder(claimed, tmp_path)
    rec.use_manifest(frozen_manifest, {TOP_CUSTOMERS: report}, {TOP_CUSTOMERS: report})
    run_dbt(rec)
    rec.report_changed(TOP_CUSTOMERS, Outcome("running"))
    rec.finish(dbt_result())
    assert dbt.warnings == [f"warn: dbtx: run history not recorded (invocation {RUN_ID}): database is locked"]
    # The start was one transaction: the manifest sync went with it.
    assert (count(claimed, m.Invocation), count(claimed, m.Node), count(claimed, m.ReportRender)) == (0, 0, 0)


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_store_error_at_the_finish_is_a_warning(
    claimed: Store, dbt: FakeDbt, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    dbt.artifact = ("run_results", frozen_run_results())
    rec = new_recorder(claimed, tmp_path)
    run_dbt(rec)
    monkeypatch.setattr(recorder.results, "load", broken)
    rec.finish(dbt_result(result=object()))
    assert dbt.warnings == [f"warn: dbtx: run history not recorded (invocation {RUN_ID}): disk full"]
    row = invocation(claimed)
    assert (row.end_time, row.result) == (None, None)  # the finish was rolled back


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_store_error_in_a_render_is_a_warning(
    claimed: Store, rendering: Recorder, dbt: FakeDbt, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(recorder.report_repo, "mark_render_running", broken)
    rendering.report_changed(TOP_CUSTOMERS, Outcome("running"))
    rendering.report_changed(TOP_CUSTOMERS, Outcome("rendered", paths=[]))
    rendering.finish(dbt_result())
    assert dbt.warnings == [f"warn: dbtx: run history not recorded (invocation {RUN_ID}): connection lost"]
    assert render(claimed).status == "pending"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_strict_mode_raises_an_unknown_node_status(claimed: Store, dbt: FakeDbt, tmp_path: Path, frozen_manifest: Manifest) -> None:
    run_results = frozen_run_results()
    run_results["results"][0]["status"] = "reused"
    dbt.artifact = ("run_results", run_results)
    rec = new_recorder(claimed, tmp_path)
    rec.use_manifest(frozen_manifest, {}, {})
    run_dbt(rec)
    with pytest.raises(UnknownValueError, match="'reused'"):
        rec.finish(dbt_result(result=object()))


# --------------------------------------------------------------------------------------
# Reading dbt's state (the functions the `dbt` fixture replaces elsewhere)
# --------------------------------------------------------------------------------------


def test_result_artifact_of_nothing_or_of_a_manifest_is_none(manifest: Manifest) -> None:
    assert recorder.result_artifact(None) is None
    assert recorder.result_artifact(manifest) is None


def test_result_artifact_of_run_results() -> None:
    from dbt.artifacts.schemas.run import RunExecutionResult

    result = RunExecutionResult(results=[], elapsed_time=1.5, generated_at=GENERATED.replace(tzinfo=None), args={"which": "run", "vars": {}, "invocation_command": "dbt run"})
    kind, data = recorder.result_artifact(result)
    assert kind == "run_results"
    assert (data["results"], data["elapsed_time"], data["args"]) == ([], 1.5, {"which": "run", "vars": {}, "invocation_command": "dbt run"})


def test_result_artifact_of_freshness() -> None:
    from dbt.artifacts.schemas.freshness import FreshnessResult

    result = FreshnessResult.from_node_results(results=[], elapsed_time=2.0, generated_at=GENERATED.replace(tzinfo=None))
    kind, data = recorder.result_artifact(result)
    assert kind == "sources"
    assert (data["results"], data["elapsed_time"]) == ([], 2.0)


def test_registered_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt.adapters.factory import FACTORY

    monkeypatch.setattr(FACTORY, "adapters", {})
    assert recorder.registered_adapter() is None

    config = SimpleNamespace(target_name="dev", project_target_path="/srv/dbtx/jaffle-shop/target")
    monkeypatch.setattr(FACTORY, "adapters", {"duckdb": SimpleNamespace(type=lambda: "duckdb", config=config)})
    assert recorder.registered_adapter() == RegisteredAdapter("duckdb", "dev", Path("/srv/dbtx/jaffle-shop/target"))


def test_flags_args_are_scrubbed_of_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBT_ENV_SECRET_TOKEN", "s3cret-value")
    monkeypatch.setattr(recorder, "get_flags", lambda: object())
    monkeypatch.setattr(recorder, "args_to_dict", lambda flags: {"which": "run", "vars": {"token": "s3cret-value"}})
    assert recorder.flags_args() == {"which": "run", "vars": {"token": "*****"}}
