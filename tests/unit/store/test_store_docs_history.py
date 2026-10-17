"""The docs overlay's run history from the database (`dbtx.docs.store_history`, plan
Phase 7).

Behaviour of `build_document(store, docs_loc, project)`: the sidecar's JSON shape
(`dbtx_runtime.json`), built from the database instead of the file.

* the node set is the docs site's `manifest.json` (nodes and exposures); results for
  nodes it does not describe are left out
* `runs` per node: its newest results, newest first by the invocation's `start_time`,
  capped at the history limit (the sidecar file's own limit when there is one, else the
  default, 3)
* only invocations whose command executes nodes (`build`, `run`, `test`, `seed`,
  `snapshot`); never another project's
* per run: `status`, `execution_time`, `checksum`, `last_ran_at` (`completed_at`),
  `run_invocation_id`, `run_generated_at` (the invocation's `end_time`), `job_id`,
  `updated_at` (`node_version.first_seen_at` of the run's checksum; None without one),
  `stale` (the run's checksum differs from the docs manifest's), `last_compiled_at`
  None (not stored)
* `report`: a rendered report found under `<site>/reports/`, as the sidecar does

`project_of(docs_loc)`: the docs manifest's `metadata.project_name`; HistoryError
without one.

`HistoryCache(store, docs_loc).document()`: the document as JSON bytes, built once and
kept.
* within the probe interval (`PROBE_INTERVAL_SECONDS`) of the last check, the kept
  document, whatever changed
* after it, the database and the docs site are checked (the project's invocation count
  and newest end time; the docs manifest, the sidecar file and the reports folder):
  rebuilt only when one of them changed
* a database error with a kept document → that document, and one warning on stderr
  until the database answers again; with none → HistoryUnavailable
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from dbtx.docs import sidecar, store_history
from dbtx.docs.store_history import (
    PROBE_INTERVAL_SECONDS,
    HistoryCache,
    HistoryError,
    HistoryUnavailable,
    build_document,
    project_of,
)
from dbtx.store import Store
from dbtx.store import models as m

from .conftest import CLOCK_START

PROJECT = "proj"
OTHER_PROJECT = "other"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------------------
# The docs site
# --------------------------------------------------------------------------------------


def write_docs_site(
    site: Path,
    nodes: dict[str, Optional[str]],
    exposures: tuple[str, ...] = (),
    project: Optional[str] = PROJECT,
) -> Path:
    """A docs site holding a `manifest.json`: `nodes` maps unique_id → checksum."""
    site.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {"invocation_id": "docs-invocation"}
    if project is not None:
        metadata["project_name"] = project
    manifest_nodes = {}
    for unique_id, checksum in nodes.items():
        manifest_nodes[unique_id] = {"checksum": {"checksum": checksum}}
    manifest_exposures = {}
    for unique_id in exposures:
        manifest_exposures[unique_id] = {"name": unique_id.rsplit(".", 1)[-1]}
    manifest = {"metadata": metadata, "nodes": manifest_nodes, "exposures": manifest_exposures}
    (site / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return site


@pytest.fixture
def site(tmp_path: Path) -> Path:
    return tmp_path / "site"


# --------------------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------------------


class History:
    """Writes projects, nodes, versions, invocations and results into a store."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def project(self, project: str = PROJECT) -> None:
        with self.store.begin() as session:
            session.add(
                m.Project(project=project, folder=project, claimed_at=CLOCK_START, last_seen_at=CLOCK_START)
            )

    def node(
        self,
        unique_id: str,
        checksum: Optional[str],
        project: str = PROJECT,
        resource_type: str = "model",
    ) -> None:
        """A node; with a checksum, also its version, first seen at T0."""
        with self.store.begin() as session:
            session.add(
                m.Node(
                    project=project,
                    unique_id=unique_id,
                    resource_type=resource_type,
                    package_name=project,
                    name=unique_id.rsplit(".", 1)[-1],
                    checksum=checksum,
                    first_seen_at=T0,
                    last_seen_at=T0,
                )
            )
        if checksum is not None:
            self.version(unique_id, checksum, T0, project=project)

    def version(self, unique_id: str, checksum: str, first_seen_at: datetime, project: str = PROJECT) -> None:
        with self.store.begin() as session:
            session.add(
                m.NodeVersion(
                    project=project,
                    unique_id=unique_id,
                    checksum=checksum,
                    first_seen_at=first_seen_at,
                    last_seen_at=first_seen_at,
                )
            )

    def invocation(
        self,
        invocation_id: str,
        start_time: datetime,
        command: str = "build",
        job_id: Optional[str] = None,
        project: str = PROJECT,
    ) -> None:
        """A finished invocation, ending one minute after it started."""
        with self.store.begin() as session:
            if job_id is not None:
                session.add(
                    m.Job(
                        job_id=job_id,
                        project=project,
                        trigger="external",
                        requested_at=start_time,
                        status="finished",
                    )
                )
                session.flush()
            session.add(
                m.Invocation(
                    invocation_id=invocation_id,
                    job_id=job_id,
                    project=project,
                    command=command,
                    start_time=start_time,
                    end_time=start_time + timedelta(minutes=1),
                    result="success",
                    dbt_version="1.12.5",
                    adapter_type="duckdb",
                    target="dev",
                    args={},
                )
            )

    def result(
        self,
        invocation_id: str,
        unique_id: str,
        checksum: Optional[str],
        completed_at: Optional[datetime],
        status: str = "success",
        execution_time: float = 1.5,
        project: str = PROJECT,
    ) -> None:
        with self.store.begin() as session:
            session.add(
                m.NodeResult(
                    invocation_id=invocation_id,
                    project=project,
                    unique_id=unique_id,
                    status=status,
                    checksum=checksum,
                    started_at=completed_at,
                    completed_at=completed_at,
                    execution_time=execution_time,
                )
            )


@pytest.fixture
def history(store: Store) -> History:
    """`store`, with the project `proj` claimed."""
    builder = History(store)
    builder.project()
    return builder


def runs_of(document: dict, unique_id: str) -> list[dict]:
    return document["nodes"][unique_id]["runs"]


# --------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------


def test_a_nodes_runs_come_newest_first_with_their_fields(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    history.invocation("inv-1", at(0), job_id="job-1")
    history.result("inv-1", "model.proj.a", "v1", at(0), status="error", execution_time=2.0)
    history.invocation("inv-2", at(10))
    history.result("inv-2", "model.proj.a", "v1", at(10))

    document = build_document(history.store, site, PROJECT)

    assert runs_of(document, "model.proj.a") == [
        {
            "status": "success",
            "execution_time": 1.5,
            "last_compiled_at": None,
            "last_ran_at": iso(at(10)),
            "checksum": "v1",
            "run_invocation_id": "inv-2",
            "run_generated_at": iso(at(11)),
            "job_id": None,
            "updated_at": iso(T0),
            "stale": False,
        },
        {
            "status": "error",
            "execution_time": 2.0,
            "last_compiled_at": None,
            "last_ran_at": iso(at(0)),
            "checksum": "v1",
            "run_invocation_id": "inv-1",
            "run_generated_at": iso(at(1)),
            "job_id": "job-1",
            "updated_at": iso(T0),
            "stale": False,
        },
    ]


def test_the_document_has_the_sidecars_shape(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v1"})

    document = build_document(history.store, site, PROJECT)

    assert document["dbtx_schema_version"] == sidecar.SIDECAR_SCHEMA_VERSION
    assert document["history_limit"] == 3
    assert document["docs_invocation_id"] == "docs-invocation"
    assert document["nodes"] == {}


def test_history_is_capped_at_the_default_limit(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    for number in range(1, 5):
        history.invocation(f"inv-{number}", at(number * 10))
        history.result(f"inv-{number}", "model.proj.a", "v1", at(number * 10))

    document = build_document(history.store, site, PROJECT)

    invocations = [run["run_invocation_id"] for run in runs_of(document, "model.proj.a")]
    assert invocations == ["inv-4", "inv-3", "inv-2"]


def test_the_sidecars_history_limit_is_used(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v1"})
    limit_only = {"dbtx_schema_version": sidecar.SIDECAR_SCHEMA_VERSION, "history_limit": 2, "nodes": {}}
    (site / sidecar.SIDECAR_FILENAME).write_text(json.dumps(limit_only), encoding="utf-8")
    history.node("model.proj.a", "v1")
    for number in range(1, 4):
        history.invocation(f"inv-{number}", at(number * 10))
        history.result(f"inv-{number}", "model.proj.a", "v1", at(number * 10))

    document = build_document(history.store, site, PROJECT)

    assert document["history_limit"] == 2
    invocations = [run["run_invocation_id"] for run in runs_of(document, "model.proj.a")]
    assert invocations == ["inv-3", "inv-2"]


def test_runs_are_ordered_by_when_the_invocation_started(history: History, site: Path) -> None:
    """A skipped node has no completion time; its run still sits where its invocation
    does."""
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))
    history.invocation("inv-2", at(10))
    history.result("inv-2", "model.proj.a", "v1", None, status="skipped", execution_time=0.0)

    document = build_document(history.store, site, PROJECT)

    runs = runs_of(document, "model.proj.a")
    assert [(run["run_invocation_id"], run["status"], run["last_ran_at"]) for run in runs] == [
        ("inv-2", "skipped", None),
        ("inv-1", "success", iso(at(0))),
    ]


# --------------------------------------------------------------------------------------
# Updated at, stale
# --------------------------------------------------------------------------------------


def test_updated_at_is_when_the_runs_version_first_appeared(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v2"})
    history.node("model.proj.a", "v1")
    history.version("model.proj.a", "v2", at(5))
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))
    history.invocation("inv-2", at(10))
    history.result("inv-2", "model.proj.a", "v2", at(10))

    document = build_document(history.store, site, PROJECT)

    runs = runs_of(document, "model.proj.a")
    assert [(run["checksum"], run["updated_at"]) for run in runs] == [("v2", iso(at(5))), ("v1", iso(T0))]


def test_a_run_without_a_checksum_has_no_updated_at(history: History, site: Path) -> None:
    write_docs_site(site, {"seed.proj.s": None})
    history.node("seed.proj.s", None, resource_type="seed")
    history.invocation("inv-1", at(0))
    history.result("inv-1", "seed.proj.s", None, at(0))

    document = build_document(history.store, site, PROJECT)

    [run] = runs_of(document, "seed.proj.s")
    assert (run["checksum"], run["updated_at"], run["stale"]) == (None, None, False)


def test_a_run_of_an_older_version_is_stale(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v2"})
    history.node("model.proj.a", "v1")
    history.version("model.proj.a", "v2", at(5))
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))
    history.invocation("inv-2", at(10))
    history.result("inv-2", "model.proj.a", "v2", at(10))

    document = build_document(history.store, site, PROJECT)

    runs = runs_of(document, "model.proj.a")
    assert [(run["run_invocation_id"], run["stale"]) for run in runs] == [("inv-2", False), ("inv-1", True)]


def test_a_generic_test_with_an_empty_checksum_is_not_stale(history: History, site: Path) -> None:
    """dbt gives generic tests an empty checksum. The sidecar records the node's name
    instead (DocsCatalog's fallback), which is also what the docs manifest compares
    against; the database holds the empty string. Found by the live test."""
    unique_id = "test.proj.not_null_a_id.1a2b3c"
    write_docs_site(site, {unique_id: ""})
    history.node(unique_id, "", resource_type="test")
    history.invocation("inv-1", at(0))
    history.result("inv-1", unique_id, "", at(0), status="pass")

    document = build_document(history.store, site, PROJECT)

    [run] = runs_of(document, unique_id)
    assert (run["checksum"], run["stale"], run["updated_at"]) == (unique_id, False, iso(T0))


# --------------------------------------------------------------------------------------
# Which results
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["build", "run", "test", "seed", "snapshot"])
def test_commands_that_execute_nodes_are_shown(history: History, site: Path, command: str) -> None:
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    history.invocation("inv-1", at(0), command=command)
    history.result("inv-1", "model.proj.a", "v1", at(0))

    document = build_document(history.store, site, PROJECT)

    assert [run["run_invocation_id"] for run in runs_of(document, "model.proj.a")] == ["inv-1"]


@pytest.mark.parametrize("command", ["compile", "source freshness", "run-operation", "parse", "docs generate"])
def test_other_commands_are_not_runs(history: History, site: Path, command: str) -> None:
    """A node is compiled by every run and by `dbtx compile` on its own: a compile is
    not a run."""
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))
    history.invocation("inv-2", at(10), command=command)
    history.result("inv-2", "model.proj.a", "v1", at(10))

    document = build_document(history.store, site, PROJECT)

    assert [run["run_invocation_id"] for run in runs_of(document, "model.proj.a")] == ["inv-1"]


def test_another_projects_results_are_not_shown(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    history.project(OTHER_PROJECT)
    history.node("model.proj.a", "v1", project=OTHER_PROJECT)
    history.invocation("inv-other", at(10), project=OTHER_PROJECT)
    history.result("inv-other", "model.proj.a", "v1", at(10), project=OTHER_PROJECT)
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))

    document = build_document(history.store, site, PROJECT)

    assert [run["run_invocation_id"] for run in runs_of(document, "model.proj.a")] == ["inv-1"]


def test_nodes_the_docs_do_not_describe_are_left_out(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    history.node("model.proj.gone", "g1")
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))
    history.result("inv-1", "model.proj.gone", "g1", at(0))

    document = build_document(history.store, site, PROJECT)

    assert sorted(document["nodes"]) == ["model.proj.a"]


def test_a_node_that_never_ran_has_no_entry(history: History, site: Path) -> None:
    write_docs_site(site, {"model.proj.a": "v1", "model.proj.b": "b1"})
    history.node("model.proj.a", "v1")
    history.node("model.proj.b", "b1")
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))

    document = build_document(history.store, site, PROJECT)

    assert sorted(document["nodes"]) == ["model.proj.a"]


# --------------------------------------------------------------------------------------
# Reports, the project
# --------------------------------------------------------------------------------------


def test_a_rendered_report_on_disk_is_attached(history: History, site: Path) -> None:
    write_docs_site(site, {}, exposures=("exposure.proj.weekly",))
    reports = site / "reports"
    reports.mkdir()
    (reports / "weekly.html").write_text("<html></html>", encoding="utf-8")
    (reports / "weekly.csv").write_text("a\n1\n", encoding="utf-8")

    document = build_document(history.store, site, PROJECT)

    report = document["nodes"]["exposure.proj.weekly"]["report"]
    assert (report["html"], report["csv"]) == ("reports/weekly.html", "reports/weekly.csv")


def test_the_project_is_the_docs_manifests_project_name(site: Path) -> None:
    write_docs_site(site, {})
    assert project_of(site) == PROJECT


def test_a_docs_manifest_without_a_project_name_is_refused(site: Path) -> None:
    write_docs_site(site, {}, project=None)
    with pytest.raises(HistoryError, match="project_name"):
        project_of(site)


def test_a_docs_site_without_a_manifest_is_refused(site: Path) -> None:
    site.mkdir()
    with pytest.raises(HistoryError, match="manifest.json"):
        project_of(site)


# --------------------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------------------


class FakeMonotonic:
    """Stands in for `store_history.monotonic`: moves only when told."""

    def __init__(self) -> None:
        self.current = 1000.0

    def monotonic(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + seconds


@pytest.fixture
def probe_clock(monkeypatch: pytest.MonkeyPatch) -> FakeMonotonic:
    fake = FakeMonotonic()
    monkeypatch.setattr(store_history, "monotonic", fake.monotonic)
    return fake


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Counts the documents built (one entry per build, the project's name)."""
    calls: list[str] = []
    real_build_document = store_history.build_document

    def counting_build_document(store: Store, docs_loc: Path, project: str) -> dict:
        calls.append(project)
        return real_build_document(store, docs_loc, project)

    monkeypatch.setattr(store_history, "build_document", counting_build_document)
    return calls


@pytest.fixture
def one_run(history: History, site: Path) -> History:
    """`history` with one build of `model.proj.a`, and a docs site describing it."""
    write_docs_site(site, {"model.proj.a": "v1"})
    history.node("model.proj.a", "v1")
    history.invocation("inv-1", at(0))
    history.result("inv-1", "model.proj.a", "v1", at(0))
    return history


def served_invocations(cache: HistoryCache, unique_id: str = "model.proj.a") -> list[str]:
    document = json.loads(cache.document())
    entry = document["nodes"].get(unique_id, {"runs": []})
    return [run["run_invocation_id"] for run in entry["runs"]]


def add_second_run(history: History) -> None:
    history.invocation("inv-2", at(10))
    history.result("inv-2", "model.proj.a", "v1", at(10))


def bump_modification_time(path: Path) -> None:
    """Moves the file's modification time on by a second, so a change is visible
    however coarse the file system's clock is."""
    later = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(later, later))


def drop_every_table(store: Store) -> None:
    m.Base.metadata.drop_all(store.engine)


def test_the_first_request_builds_the_document(one_run: History, site: Path, probe_clock: FakeMonotonic) -> None:
    cache = HistoryCache(one_run.store, site)
    served = json.loads(cache.document())
    built = build_document(one_run.store, site, PROJECT)
    # The time each document was made differs; everything else is the same.
    del served["updated_at"]
    del built["updated_at"]
    assert served == built


def test_within_the_probe_interval_the_kept_document_is_served(
    one_run: History, site: Path, probe_clock: FakeMonotonic, builds: list[str]
) -> None:
    cache = HistoryCache(one_run.store, site)
    first = cache.document()
    add_second_run(one_run)
    probe_clock.advance(PROBE_INTERVAL_SECONDS - 0.1)

    assert cache.document() == first
    assert builds == [PROJECT]


def test_a_new_run_is_served_after_the_probe_interval(
    one_run: History, site: Path, probe_clock: FakeMonotonic, builds: list[str]
) -> None:
    cache = HistoryCache(one_run.store, site)
    assert served_invocations(cache) == ["inv-1"]
    add_second_run(one_run)
    probe_clock.advance(PROBE_INTERVAL_SECONDS)

    assert served_invocations(cache) == ["inv-2", "inv-1"]
    assert builds == [PROJECT, PROJECT]


def test_nothing_changed_is_not_rebuilt(
    one_run: History, site: Path, probe_clock: FakeMonotonic, builds: list[str]
) -> None:
    cache = HistoryCache(one_run.store, site)
    first = cache.document()
    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    second = cache.document()
    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    third = cache.document()

    assert first == second == third
    assert builds == [PROJECT]


def test_another_projects_run_does_not_rebuild(
    one_run: History, site: Path, probe_clock: FakeMonotonic, builds: list[str]
) -> None:
    cache = HistoryCache(one_run.store, site)
    cache.document()
    one_run.project(OTHER_PROJECT)
    one_run.invocation("inv-other", at(10), project=OTHER_PROJECT)
    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    cache.document()

    assert builds == [PROJECT]


def test_a_regenerated_docs_manifest_is_picked_up(
    one_run: History, site: Path, probe_clock: FakeMonotonic
) -> None:
    cache = HistoryCache(one_run.store, site)
    assert json.loads(cache.document())["nodes"]["model.proj.a"]["runs"][0]["stale"] is False
    write_docs_site(site, {"model.proj.a": "v2"})
    bump_modification_time(site / "manifest.json")
    probe_clock.advance(PROBE_INTERVAL_SECONDS)

    assert json.loads(cache.document())["nodes"]["model.proj.a"]["runs"][0]["stale"] is True


def test_a_new_history_limit_in_the_sidecar_is_picked_up(
    one_run: History, site: Path, probe_clock: FakeMonotonic
) -> None:
    add_second_run(one_run)
    cache = HistoryCache(one_run.store, site)
    assert served_invocations(cache) == ["inv-2", "inv-1"]
    limit_only = {"dbtx_schema_version": sidecar.SIDECAR_SCHEMA_VERSION, "history_limit": 1, "nodes": {}}
    (site / sidecar.SIDECAR_FILENAME).write_text(json.dumps(limit_only), encoding="utf-8")
    probe_clock.advance(PROBE_INTERVAL_SECONDS)

    assert served_invocations(cache) == ["inv-2"]


def test_a_new_report_is_picked_up(history: History, site: Path, probe_clock: FakeMonotonic) -> None:
    write_docs_site(site, {}, exposures=("exposure.proj.weekly",))
    cache = HistoryCache(history.store, site)
    assert json.loads(cache.document())["nodes"] == {}
    reports = site / "reports"
    reports.mkdir()
    (reports / "weekly.html").write_text("<html></html>", encoding="utf-8")
    probe_clock.advance(PROBE_INTERVAL_SECONDS)

    report = json.loads(cache.document())["nodes"]["exposure.proj.weekly"]["report"]
    assert report["html"] == "reports/weekly.html"


def test_a_re_rendered_report_is_picked_up(history: History, site: Path, probe_clock: FakeMonotonic) -> None:
    write_docs_site(site, {}, exposures=("exposure.proj.weekly",))
    reports = site / "reports"
    reports.mkdir()
    html = reports / "weekly.html"
    html.write_text("<html></html>", encoding="utf-8")
    cache = HistoryCache(history.store, site)
    before = json.loads(cache.document())["nodes"]["exposure.proj.weekly"]["report"]["generated_at"]
    html.write_text("<html><body>new</body></html>", encoding="utf-8")
    bump_modification_time(html)
    probe_clock.advance(PROBE_INTERVAL_SECONDS)

    after = json.loads(cache.document())["nodes"]["exposure.proj.weekly"]["report"]["generated_at"]
    assert after != before


def test_a_database_error_keeps_the_last_document_and_warns_once(
    one_run: History, site: Path, probe_clock: FakeMonotonic, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = HistoryCache(one_run.store, site)
    first = cache.document()
    drop_every_table(one_run.store)

    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    assert cache.document() == first
    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    assert cache.document() == first

    err = capsys.readouterr().err
    assert err.count("warning") == 1
    assert "cannot read the run history from the database" in err


def test_a_database_error_without_a_document_is_unavailable(
    one_run: History, site: Path, probe_clock: FakeMonotonic
) -> None:
    cache = HistoryCache(one_run.store, site)
    drop_every_table(one_run.store)

    with pytest.raises(HistoryUnavailable, match="cannot read the run history from the database"):
        cache.document()


def test_the_cache_recovers_when_the_database_does(
    one_run: History, site: Path, probe_clock: FakeMonotonic, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = HistoryCache(one_run.store, site)
    assert served_invocations(cache) == ["inv-1"]
    drop_every_table(one_run.store)
    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    assert served_invocations(cache) == ["inv-1"]

    m.Base.metadata.create_all(one_run.store.engine)
    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    assert served_invocations(cache) == []

    drop_every_table(one_run.store)
    probe_clock.advance(PROBE_INTERVAL_SECONDS)
    cache.document()
    assert capsys.readouterr().err.count("warning") == 2


def test_the_project_is_read_when_the_cache_is_made(history: History, site: Path) -> None:
    write_docs_site(site, {}, project=None)
    with pytest.raises(HistoryError, match="project_name"):
        HistoryCache(history.store, site)
