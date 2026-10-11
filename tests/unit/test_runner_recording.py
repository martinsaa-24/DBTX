"""How dbtx hands its dbt invocations to the store's recorder (plan 5.7).

Behaviour:
* exactly one invocation per dbtx command is recorded: the user's command. dbtx's
  internal `parse` and `ls` get no recorder callback
* `dbtx build`: the recorder is told the parsed manifest, every report it defines and
  the reports this run renders (`use_manifest`) before dbt starts; its `on_event` is a
  callback of the build; the scheduler reports each render transition to it
  (`report_changed`); `finish(result)` comes last, after the scheduler has settled every
  report
* `dbtx build` with no report selected, or `--no-reports`; `dbtx compile` (reports
  validated, none rendered), with `--no-reports` or `--inline`: the same, without
  renders
* a failed parse: dbtx re-runs the user's command so dbt reports the error itself
  (plan 5.5). That run is the one recorded
* a pass-through command (`dbtx run`): recorded the same way
* the exit code is dbt's, whatever the recorder does
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from dbt.contracts.graph.manifest import Manifest

from dbtx import cli, runner
from dbtx.config import DbtxConfig
from dbtx.reports import Report
from dbtx.scheduler import Outcome
from tests.conftest import EXAMPLE_PROJECT

TOP_CUSTOMERS = "exposure.jaffle_shop.top_customers"
# As platform mode hands them on (dbt checks a default --profiles-dir exists).
PROJECT_ARGS = ["--project-dir", str(EXAMPLE_PROJECT), "--profiles-dir", str(EXAMPLE_PROJECT)]


class FakeRecorder:
    """Stands in for the store's Recorder: logs every call, in order."""

    def __init__(self, config: Optional[DbtxConfig]) -> None:
        self.config = config
        self.calls: list[tuple[Any, ...]] = []

    def on_event(self, event: Any) -> None:
        self.calls.append(("event", event.info.name))

    def use_manifest(self, manifest: Any, reports: dict[str, Report], renders: dict[str, Report]) -> None:
        self.calls.append(("use_manifest", sorted(reports), sorted(renders)))

    def report_changed(self, exposure_id: str, outcome: Outcome) -> None:
        self.calls.append(("report_changed", exposure_id, outcome.status))

    def finish(self, result: Any) -> None:
        self.calls.append(("finish", result))


class Recorders:
    """Replaces `Recorder.for_invocation` in runner and cli; keeps every one made."""

    def __init__(self) -> None:
        self.made: list[FakeRecorder] = []

    def for_invocation(self, config: Optional[DbtxConfig]) -> FakeRecorder:
        made = FakeRecorder(config)
        self.made.append(made)
        return made


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> Recorders:
    fake = Recorders()
    monkeypatch.setattr(runner, "Recorder", fake)
    monkeypatch.setattr(cli, "Recorder", fake)
    return fake


def event(name: str, unique_id: Optional[str] = None, status: Optional[str] = None) -> SimpleNamespace:
    node_info = SimpleNamespace(unique_id=unique_id, node_status=status)
    return SimpleNamespace(
        info=SimpleNamespace(name=name, invocation_id="inv-1"), data=SimpleNamespace(node_info=node_info)
    )


class FakeDbt:
    """Stands in for dbtRunner: answers dbtx's parse and ls from the frozen manifest, and
    fires dbt's events for any other command."""

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self.selected = [TOP_CUSTOMERS]
        self.parse_fails = False
        self.invokes: list[tuple[str, int]] = []  # (command, number of callbacks)
        self.results: list[SimpleNamespace] = []

    def runner(self, manifest: Any = None, callbacks: Optional[list] = None) -> SimpleNamespace:
        return SimpleNamespace(invoke=lambda args: self.invoke(args, callbacks or []))

    def invoke(self, args: list[str], callbacks: list) -> SimpleNamespace:
        command = args[0]
        self.invokes.append((command, len(callbacks)))
        if command == "parse":
            if self.parse_fails:
                return SimpleNamespace(success=False, exception=RuntimeError("Parsing Error"), result=None)
            return SimpleNamespace(success=True, exception=None, result=self.manifest)
        if command == "ls":
            lines = [json.dumps({"unique_id": uid}) for uid in self.selected]
            return SimpleNamespace(success=True, exception=None, result=lines)
        for callback in callbacks:
            callback(event("MainReportVersion"))
        for uid in self.selected:
            for callback in callbacks:
                callback(event("NodeFinished", uid, "success"))
        for callback in callbacks:
            callback(event("CommandCompleted"))
        result = SimpleNamespace(success=True, exception=None, result=None)
        self.results.append(result)
        return result


@pytest.fixture
def fake_dbt_runner(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> FakeDbt:
    fake = FakeDbt(manifest)
    monkeypatch.setattr(runner, "dbtRunner", fake.runner)
    monkeypatch.setattr(cli, "dbtRunner", fake.runner)
    monkeypatch.setattr(runner, "query_and_render", lambda *a: [Path("top_customers.html")])
    return fake


ALL_REPORTS = [
    "exposure.jaffle_shop.beverage_leaderboard",
    "exposure.jaffle_shop.customer_leaderboard",
    "exposure.jaffle_shop.jaffle_mix",
    "exposure.jaffle_shop.store_revenue_monthly",
    "exposure.jaffle_shop.store_scorecard",
    "exposure.jaffle_shop.top_customers",
]


# --------------------------------------------------------------------------------------
# dbtx build
# --------------------------------------------------------------------------------------


def test_build_records_the_build_and_its_renders(fake_dbt_runner: FakeDbt, recorders: Recorders) -> None:
    assert runner.build(PROJECT_ARGS, config=None) == 0
    # The internal parse and ls are not recorded; the build is.
    assert fake_dbt_runner.invokes == [("parse", 0), ("ls", 0), ("build", 2)]
    [recorder] = recorders.made
    assert recorder.calls == [
        ("use_manifest", ALL_REPORTS, [TOP_CUSTOMERS]),
        ("event", "MainReportVersion"),
        ("event", "NodeFinished"),
        ("report_changed", TOP_CUSTOMERS, "running"),
        ("report_changed", TOP_CUSTOMERS, "rendered"),
        ("event", "CommandCompleted"),
        ("finish", fake_dbt_runner.results[-1]),
    ]


def test_build_gives_the_recorder_its_config(fake_dbt_runner: FakeDbt, recorders: Recorders) -> None:
    config = SimpleNamespace(name="the config")
    runner.build(PROJECT_ARGS, config=config)  # type: ignore[arg-type]
    assert [made.config for made in recorders.made] == [config]


def test_build_with_no_report_selected(fake_dbt_runner: FakeDbt, recorders: Recorders) -> None:
    fake_dbt_runner.selected = ["model.jaffle_shop.customers"]
    assert runner.build(PROJECT_ARGS) == 0
    assert fake_dbt_runner.invokes == [("parse", 0), ("ls", 0), ("build", 1)]
    [recorder] = recorders.made
    assert recorder.calls == [
        ("use_manifest", ALL_REPORTS, []),
        ("event", "MainReportVersion"),
        ("event", "NodeFinished"),
        ("event", "CommandCompleted"),
        ("finish", fake_dbt_runner.results[-1]),
    ]


def test_build_without_reports(fake_dbt_runner: FakeDbt, recorders: Recorders) -> None:
    assert runner.build(PROJECT_ARGS, reports_enabled=False) == 0
    assert fake_dbt_runner.invokes == [("build", 1)]
    [recorder] = recorders.made
    assert recorder.calls == [
        ("event", "MainReportVersion"),
        ("event", "NodeFinished"),
        ("event", "CommandCompleted"),
        ("finish", fake_dbt_runner.results[-1]),
    ]


def test_a_failed_parse_records_the_rerun(fake_dbt_runner: FakeDbt, recorders: Recorders) -> None:
    fake_dbt_runner.parse_fails = True
    assert runner.build(PROJECT_ARGS) == 0  # the fake's re-run succeeds
    assert fake_dbt_runner.invokes == [("parse", 0), ("build", 1)]
    [recorder] = recorders.made
    assert recorder.calls[0] == ("event", "MainReportVersion")
    assert recorder.calls[-1] == ("finish", fake_dbt_runner.results[-1])
    assert "use_manifest" not in [call[0] for call in recorder.calls]


# --------------------------------------------------------------------------------------
# dbtx compile
# --------------------------------------------------------------------------------------


def test_compile_records_the_compile(fake_dbt_runner: FakeDbt, recorders: Recorders) -> None:
    assert runner.compile_project(PROJECT_ARGS) == 0
    assert fake_dbt_runner.invokes == [("parse", 0), ("ls", 0), ("compile", 1)]
    [recorder] = recorders.made
    assert recorder.calls[0] == ("use_manifest", ALL_REPORTS, [])
    assert recorder.calls[-1] == ("finish", fake_dbt_runner.results[-1])


@pytest.mark.parametrize("extra, enabled", [([], False), (["--inline", "select 1"], True)])
def test_compile_without_reports(
    fake_dbt_runner: FakeDbt, recorders: Recorders, extra: list[str], enabled: bool
) -> None:
    assert runner.compile_project(PROJECT_ARGS + extra, reports_enabled=enabled) == 0
    assert fake_dbt_runner.invokes == [("compile", 1)]
    [recorder] = recorders.made
    assert recorder.calls[0] == ("event", "MainReportVersion")
    assert recorder.calls[-1] == ("finish", fake_dbt_runner.results[-1])


def test_a_failed_compile_parse_records_the_rerun(fake_dbt_runner: FakeDbt, recorders: Recorders) -> None:
    fake_dbt_runner.parse_fails = True
    runner.compile_project(PROJECT_ARGS)
    assert fake_dbt_runner.invokes == [("parse", 0), ("compile", 1)]
    [recorder] = recorders.made
    assert recorder.calls[-1] == ("finish", fake_dbt_runner.results[-1])


# --------------------------------------------------------------------------------------
# Pass-through (cli.main)
# --------------------------------------------------------------------------------------


def test_a_pass_through_command_is_recorded(
    fake_dbt_runner: FakeDbt, recorders: Recorders, lone_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(lone_project)
    assert cli.main(["run", "--standalone-mode"]) == 0
    assert fake_dbt_runner.invokes == [("run", 1)]
    [recorder] = recorders.made
    assert recorder.config is not None and recorder.config.command == "run"
    assert recorder.calls[-1] == ("finish", fake_dbt_runner.results[-1])
