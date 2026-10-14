"""The runner hook (plan 5.7): records one dbt invocation that dbtx runs.

    recorder = Recorder.for_invocation(config)      # a NullRecorder when nothing is recorded
    recorder.use_manifest(manifest, reports, renders)  # build / compile: before dbt runs
    runner = dbtRunner(manifest=..., callbacks=[recorder.on_event, scheduler.on_event])
    scheduler = ReportScheduler(plans, render=..., on_change=recorder.report_changed)
    result = runner.invoke([...])
    recorder.finish(result)

* `on_event` starts the invocation on dbt's first event, `MainReportVersion`: dbt
  cannot be given an invocation id, so it is taken from the event (plan 5.2). With a
  manifest, the start also syncs the manifest and the reports and inserts a `pending`
  render row for each report this run renders: the render rows' foreign keys need them
  before dbt's node threads start rendering. `ConcurrencyLine` gives the target and
  `CommandCompleted` the registered adapter and the final flags. `ArtifactWritten` for
  a `WritableManifest` names the manifest this invocation wrote (its hash is stored).
* `report_changed` records each render transition, in its own short transaction (D6):
  it runs on dbt's node threads.
* `finish` stores the node results and ends the invocation, in one transaction.

Everything after dbt has started is best-effort (D5): an error is logged once through
dbt's events at WARN, and the recorder stops writing. dbt's exit code never changes.
In strict mode (D9, a test switch) an unknown app-enforced value is raised instead.

dbt internals (checked against dbt 1.12.5, plan risk table) are read only here, each
through one function: `invocation_started_at`, `flags_args`, `registered_adapter` and
`result_artifact`.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

import sqlalchemy as sa
from dbt.flags import get_flags
from dbt.utils import args_to_dict
from dbt_common.events.base_types import EventLevel
from dbt_common.events.functions import fire_event
from dbt_common.events.types import Note

from dbtx.docs.catalog import DEFAULT_JOB_ID_VAR, read_job_id
from dbtx.store import clock, open_store
from dbtx.store.enums import RunResult, command_from_cli
from dbtx.store.loaders.manifest import ManifestRows, manifest_rows, read_manifest
from dbtx.store.loaders.run_results import (
    UNKNOWN_TARGET,
    InvocationRecord,
    freshness_result_rows,
    run_result_rows,
    scrub_args,
    selector_of,
)
from dbtx.store.repositories import invocations, projects
from dbtx.store.repositories import reports as report_repo
from dbtx.store.repositories import results
from dbtx.store.repositories.nodes import sync_manifest
from dbtx.store.validation import STRICT_ENV_VAR, UnknownValueError

if TYPE_CHECKING:
    from dbtx.config import DbtxConfig
    from dbtx.reports import Report
    from dbtx.scheduler import Outcome
    from dbtx.store import Store

# `invocation.adapter_type` when nothing says which adapter ran (NOT NULL column).
UNKNOWN_ADAPTER_TYPE = "unknown"

# `ArtifactWritten.artifact_type` when dbt has written manifest.json.
MANIFEST_ARTIFACT_TYPE = "WritableManifest"

# Render outcomes that end a render.
FINISHED_RENDER_STATUSES = ("rendered", "skipped", "error")


# --------------------------------------------------------------------------------------
# dbt's state. Tests replace these functions.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredAdapter:
    """What dbt's registered adapter says about this invocation."""

    type: str
    target: str
    # The project's absolute target dir (where manifest.json is written).
    target_path: Path


def invocation_started_at() -> datetime:
    """When dbt started this invocation (dbt keeps it naive UTC), aware UTC."""
    from dbt_common.invocation import get_invocation_started_at

    started = get_invocation_started_at()
    if started.tzinfo is None:
        return started.replace(tzinfo=timezone.utc)
    return started.astimezone(timezone.utc)


def flags_args() -> dict[str, Any]:
    """The invocation's flags as dbt writes them to run_results' `args`, with the values
    of `DBT_ENV_SECRET_*` variables scrubbed (plan 5.2, Q5)."""
    return scrub_args(args_to_dict(get_flags()))


def registered_adapter() -> Optional[RegisteredAdapter]:
    """The adapter dbt registered for this invocation, or None (dbt failed before it
    loaded the profile). dbt resets the registry at the start of every invoke."""
    from dbt.adapters.factory import FACTORY

    adapters = list(FACTORY.adapters.values())
    if len(adapters) != 1:
        return None
    adapter = adapters[0]
    return RegisteredAdapter(
        type=str(adapter.type()),
        target=str(adapter.config.target_name),
        target_path=Path(adapter.config.project_target_path),
    )


def result_artifact(result: Any) -> Optional[tuple[str, dict[str, Any]]]:
    """dbt's result as the artifact dbt writes for it, through dbt's own serialisers
    (plan 5.3): ("run_results", ...) or ("sources", ...). None for other results
    (a parse's Manifest, docs generate's catalog, nothing)."""
    from dbt.artifacts.schemas.freshness import FreshnessExecutionResultArtifact, FreshnessResult
    from dbt.artifacts.schemas.run import RunExecutionResult, RunResultsArtifact

    if isinstance(result, RunExecutionResult):
        artifact = RunResultsArtifact.from_execution_results(
            results=result.results,
            elapsed_time=result.elapsed_time,
            generated_at=result.generated_at,
            args=result.args,
        )
        return "run_results", artifact.to_dict(omit_none=False)
    if isinstance(result, FreshnessResult):
        artifact = FreshnessExecutionResultArtifact.from_result(result)
        return "sources", artifact.to_dict(omit_none=False)
    return None


def current_user() -> Optional[str]:
    try:
        return getpass.getuser()
    except Exception:
        return None


def this_host() -> Optional[str]:
    try:
        return projects.current_host()
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Recorders
# --------------------------------------------------------------------------------------


class NullRecorder:
    """Records nothing: standalone mode, a disabled backend, a command not recorded."""

    def on_event(self, event: Any) -> None:
        return

    def use_manifest(self, manifest: Any, reports: dict[str, Report], renders: dict[str, Report]) -> None:
        return

    def job_id_of(self, args: dict[str, Any]) -> Optional[str]:
        """dbtx's own job id (platform runs); else the one the args name."""
        if self.job_id is not None:
            return self.job_id
        return read_job_id({"args": args}, DEFAULT_JOB_ID_VAR)

    def run_folder_text(self) -> Optional[str]:
        if self.run_folder is None:
            return None
        return str(self.run_folder)

    def report_changed(self, exposure_id: str, outcome: Outcome) -> None:
        return

    def finish(self, result: Any) -> None:
        return


class Recorder(NullRecorder):
    """Records one invocation of a project's recorded command."""

    def __init__(
        self,
        store: Store,
        project: str,
        command: str,
        job_id: Optional[str] = None,
        run_folder: Optional[Path] = None,
    ) -> None:
        self.store = store
        self.project = project
        self.command = command
        # Platform runs: the job id dbtx chose (and handed to dbt) and the run's folder.
        self.job_id = job_id
        self.run_folder = run_folder
        # From use_manifest (build / compile): the manifest dbtx parsed, its reports,
        # and the reports this run renders. All keyed by exposure unique_id.
        self.manifest_rows: Optional[ManifestRows] = None
        self.reports: Optional[dict[str, Report]] = None
        self.renders: dict[str, Report] = {}
        # From dbt's events.
        self.invocation_id: Optional[str] = None
        self.start_time: Optional[datetime] = None
        self.target: Optional[str] = None
        self.adapter: Optional[RegisteredAdapter] = None
        # The manifest.json this invocation wrote, as dbt announced it; None if it wrote none.
        self.written_manifest: Optional[Path] = None
        self.start_args: dict[str, Any] = {}
        self.final_args: Optional[dict[str, Any]] = None
        # Set once something failed (give_up): nothing more is written.
        self.stopped = False
        self.lock = threading.Lock()

    @staticmethod
    def for_invocation(config: Optional[DbtxConfig]) -> Union[Recorder, NullRecorder]:
        """A Recorder for a recorded command of a platform project with the backend
        enabled; a NullRecorder otherwise. The store is opened, not connected."""
        if config is None:
            return NullRecorder()
        if not config.is_platform:
            return NullRecorder()
        if not config.backend_enabled:
            return NullRecorder()
        command = command_from_cli(config.command)
        if command is None:
            return NullRecorder()
        if config.project_name is None:
            # dbt_project.yml has no usable name: nothing to attribute the run to.
            return NullRecorder()
        if config.db_uri is None or config.db_uri.value is None:
            return NullRecorder()
        return Recorder(
            open_store(config),
            config.project_name,
            command,
            job_id=config.job_id,
            run_folder=config.run_folder,
        )

    # ----------------------------------------------------------------------------------
    # Before dbt runs
    # ----------------------------------------------------------------------------------

    def use_manifest(self, manifest: Any, reports: dict[str, Report], renders: dict[str, Report]) -> None:
        """The manifest dbtx parsed, its reports, and the reports this run renders."""
        try:
            self.manifest_rows = manifest_rows(manifest)
        except Exception as exc:
            self.give_up(exc)
            return
        self.reports = reports
        self.renders = renders

    # ----------------------------------------------------------------------------------
    # While dbt runs
    # ----------------------------------------------------------------------------------

    def on_event(self, event: Any) -> None:
        """dbtRunner callback, on whichever of dbt's threads fired the event."""
        name = event.info.name
        if name == "MainReportVersion":
            if self.invocation_id is None:
                self.start(event.info.invocation_id)
            return
        if name == "ConcurrencyLine":
            self.target = event.data.target_name
            return
        if name == "ArtifactWritten":
            if event.data.artifact_type == MANIFEST_ARTIFACT_TYPE:
                self.written_manifest = Path(event.data.artifact_path)
            return
        if name == "CommandCompleted":
            try:
                self.adapter = registered_adapter()
                self.final_args = flags_args()
            except Exception as exc:
                self.give_up(exc)

    def start(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id
        if self.stopped:
            return
        try:
            self.start_time = invocation_started_at()
            args = flags_args()
            self.start_args = args
            record = InvocationRecord(
                invocation_id=invocation_id,
                command=self.command,
                start_time=self.start_time,
                end_time=self.start_time,
                dbt_version=dbt_version(),
                target=UNKNOWN_TARGET,
                selector=selector_of(args),
                args=args,
                job_id=self.job_id_of(args),
            )
            adapter_type = UNKNOWN_ADAPTER_TYPE
            if self.manifest_rows is not None:
                adapter_type = self.manifest_rows.adapter_type
            with self.store.begin() as session:
                if self.manifest_rows is not None:
                    sync_manifest(session, self.store.adapter, self.manifest_rows)
                    if self.reports is not None:
                        report_repo.sync(
                            session, self.store.adapter, self.project, self.reports, self.manifest_rows.seen_at
                        )
                invocations.start(
                    session,
                    self.store.adapter,
                    project=self.project,
                    adapter_type=adapter_type,
                    record=record,
                    run_by=current_user(),
                    host=this_host(),
                    run_folder=self.run_folder_text(),
                )
                if len(self.renders) > 0:
                    report_repo.insert_pending_renders(
                        session, self.store.adapter, invocation_id, self.project, self.renders
                    )
        except Exception as exc:
            self.give_up(exc)

    def report_changed(self, exposure_id: str, outcome: Outcome) -> None:
        """The scheduler's observer: one render transition, on a dbt node thread."""
        if self.stopped or self.invocation_id is None:
            return
        if exposure_id not in self.renders:
            return
        try:
            with self.store.begin() as session:
                if outcome.status == "running":
                    report_repo.mark_render_running(session, self.invocation_id, self.project, exposure_id)
                elif outcome.status in FINISHED_RENDER_STATUSES:
                    paths = None
                    if outcome.status == "rendered":
                        paths = report_repo.render_paths(outcome.paths)
                    detail = outcome.detail
                    if detail == "":
                        detail = None
                    report_repo.mark_render_finished(
                        session,
                        self.invocation_id,
                        self.project,
                        exposure_id,
                        status=outcome.status,
                        detail=detail,
                        paths=paths,
                    )
        except Exception as exc:
            self.give_up(exc)

    # ----------------------------------------------------------------------------------
    # After dbt returned
    # ----------------------------------------------------------------------------------

    def finish(self, result: Any) -> None:
        """Store the node results and end the invocation; then close the store."""
        try:
            if self.invocation_id is not None and not self.stopped:
                self.finish_invocation(result)
        except Exception as exc:
            self.give_up(exc)
        finally:
            self.store.dispose()

    def finish_invocation(self, result: Any) -> None:
        assert self.invocation_id is not None
        end_time = clock.now()
        artifact = result_artifact(result.result)

        # Pass-through commands: the manifest this invocation produced, if any.
        rows_to_sync = None
        rows = self.manifest_rows
        if rows is None:
            rows = self.manifest_of_this_invocation(result.result)
            rows_to_sync = rows

        adapter_type = UNKNOWN_ADAPTER_TYPE
        if rows is not None:
            adapter_type = rows.adapter_type
        elif self.adapter is not None:
            adapter_type = self.adapter.type

        target = self.target
        if target is None and self.adapter is not None:
            target = self.adapter.target
        if target is None:
            target = UNKNOWN_TARGET
            self.warn(f"invocation {self.invocation_id}: dbt did not resolve a target; recorded as 'unknown'")

        checksums: dict[str, Optional[str]] = {}
        if rows is not None:
            for node in rows.nodes:
                checksums[node["unique_id"]] = node["checksum"]
        node_rows: list[dict[str, Any]] = []
        if artifact is not None:
            kind, data = artifact
            if kind == "run_results":
                node_rows = run_result_rows(data, self.project, checksums)
            else:
                node_rows = freshness_result_rows(data, self.project)

        error_message = None
        if result.exception is not None:
            outcome = RunResult.FAILURE.value
            error_message = str(result.exception)
        elif not result.success and len(node_rows) == 0:
            outcome = RunResult.FAILURE.value
        else:
            outcome = invocations.result_from_statuses(row["status"] for row in node_rows)

        with self.store.begin() as session:
            if rows_to_sync is not None:
                sync_manifest(session, self.store.adapter, rows_to_sync)
            results.load(session, self.store.adapter, self.project, node_rows)
            # The flags at CommandCompleted equal the artifact's args; the start's are
            # the fallback (dbt fills some in during the run).
            args = self.final_args
            if args is None:
                args = self.start_args
            invocations.set_resolved(session, self.invocation_id, adapter_type=adapter_type, target=target, args=args)
            invocations.finish(
                session,
                self.invocation_id,
                end_time=end_time,
                result=outcome,
                error_message=error_message,
                manifest_hash=self.manifest_hash(),
            )

    def manifest_of_this_invocation(self, result: Any) -> Optional[ManifestRows]:
        """For a pass-through: dbt's parse result, or `<target>/manifest.json` when this
        invocation wrote it. None otherwise (a failed parse; a stale file)."""
        from dbt.contracts.graph.manifest import Manifest

        if isinstance(result, Manifest):
            return manifest_rows(result)
        path = self.manifest_path()
        if path is None or not path.is_file():
            return None
        manifest = read_manifest(path)
        if manifest.metadata.invocation_id != self.invocation_id:
            return None
        return manifest_rows(manifest)

    def manifest_path(self) -> Optional[Path]:
        if self.adapter is None:
            return None
        return self.adapter.target_path / "manifest.json"

    def manifest_hash(self) -> Optional[str]:
        """sha256 of the manifest.json this invocation wrote (plan 5.2), as dbt announced
        it with `ArtifactWritten`. None when it wrote none (`--no-write-json`, a failed
        parse, a crash): the file on disk is then another run's."""
        if self.written_manifest is None or not self.written_manifest.is_file():
            return None
        return hashlib.sha256(self.written_manifest.read_bytes()).hexdigest()

    # ----------------------------------------------------------------------------------
    # Errors (D5)
    # ----------------------------------------------------------------------------------

    def give_up(self, exc: Exception) -> None:
        """Stop recording, with one warning. Strict mode raises an unknown value."""
        if isinstance(exc, UnknownValueError) and os.environ.get(STRICT_ENV_VAR) == "1":
            raise exc
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
        self.warn(f"run history not recorded (invocation {self.invocation_id}): {error_reason(exc)}")

    def warn(self, message: str) -> None:
        fire_event(Note(msg=f"dbtx: {message}"), level=EventLevel.WARN)


def dbt_version() -> str:
    from dbt.version import __version__

    return str(__version__)


def error_reason(exc: Exception) -> str:
    """The first line of the error; a database error's driver message only."""
    if isinstance(exc, sa.exc.SQLAlchemyError):
        original = getattr(exc, "orig", None)
        if original is not None:
            text = str(original)
        else:
            text = str(exc)
    else:
        text = str(exc)
    lines = text.strip().splitlines()
    if len(lines) == 0:
        return type(exc).__name__
    return lines[0]
