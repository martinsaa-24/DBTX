"""`dbtx db load [--run-loc DIR]`: the post-hoc import (plan 6.7, Phase 4).

Behaviour (platform mode, a project folder, the backend enabled and DB_URI set, like
every `dbtx db` command that uses the database):
* reads `<run-loc>/manifest.json` (required) and `run_results.json` and/or
  `sources.json` (at least one); `--run-loc` defaults to `<project>/target`
* the manifest must belong to the project folder's dbt project (`name:`); guard G2
  claims or confirms the name for the folder, as a run does
* in one transaction: the manifest sync; the report sync (reports discovered with the
  project's templates; invalid report definitions → a warning, reports not synced);
  for each artifact its invocation and node results
* `manifest_hash` (sha256 of `manifest.json`) goes on the invocation the manifest was
  written by (its `metadata.invocation_id`); the invocation's result comes from its
  node statuses; `run_by` / `host` stay NULL (the artifacts do not say)
* an artifact from a command that is not recorded (`dbt ls`) → a warning, not loaded
* prints what it loaded; exit 0. Loading again gives identical rows (done when: the
  example's artifacts loaded twice → identical row counts, no errors)
* problems → exit 2 with the reason, and nothing written: a missing directory or
  manifest, no run artifact, another project's manifest, another folder's claim, an
  unknown node status in strict mode
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa

from dbtx import cli
from dbtx.store import Store
from dbtx.store import models as m
from tests.conftest import EXAMPLE_PROJECT, FROZEN_MANIFEST, FROZEN_RUN_RESULTS, FROZEN_SOURCES
from tests.unit.conftest import LayoutBuilder

from .conftest import CLOCK_START, ON_SQLITE_FILE_AND_POSTGRES

RUN_ID = "00000000-0000-4000-8000-000000000001"
FRESHNESS_ID = "00000000-0000-4000-8000-000000000002"
COUNTED = (m.Project, m.Job, m.Node, m.NodeVersion, m.NodeEdge, m.Invocation, m.NodeResult, m.Report, m.ReportRender)


def db_flags(store: Store) -> list[str]:
    flags = ["--db-uri", store.uri]
    if store.schema is not None:
        flags += ["--db-schema", store.schema]
    return flags


@pytest.fixture
def example_platform(layout: LayoutBuilder) -> Path:
    """A platform root whose `jaffle-shop` folder is a copy of the example project
    (templates included, so its reports resolve)."""
    root = layout.platform_root(sample="backend_without_uri.conf")
    shutil.copytree(
        EXAMPLE_PROJECT,
        root / "jaffle-shop",
        ignore=shutil.ignore_patterns("target", "logs", "*.duckdb", "*.duckdb.wal"),
    )
    return root


def write_artifacts(
    directory: Path, run_results: bool = True, sources: bool = False, manifest_invocation: str = RUN_ID
) -> Path:
    """The frozen artifacts in `directory`; the manifest as written by `manifest_invocation`."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    manifest["metadata"]["generated_at"] = "2025-12-31T23:59:00Z"
    manifest["metadata"]["invocation_id"] = manifest_invocation
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if run_results:
        shutil.copyfile(FROZEN_RUN_RESULTS, directory / "run_results.json")
    if sources:
        shutil.copyfile(FROZEN_SOURCES, directory / "sources.json")
    return directory


def row_counts(store: Store) -> dict[str, int]:
    counts = {}
    with store.session() as session:
        for model in COUNTED:
            counts[model.__tablename__] = session.scalar(sa.select(sa.func.count()).select_from(model))
    return counts


def all_rows(store: Store) -> dict[str, list[tuple]]:
    rows = {}
    with store.session() as session:
        for model in COUNTED:
            rows[model.__tablename__] = sorted(
                (tuple(str(value) for value in row) for row in session.execute(sa.select(model.__table__)).all())
            )
    return rows


def load(store: Store, *extra: str) -> int:
    return cli.main(["db", "load", "--project-dir", "jaffle-shop", *extra, *db_flags(store)])


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_load_the_example_artifacts(
    store: Store,
    example_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_loc = write_artifacts(example_platform / "artifacts")
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 0
    assert capsys.readouterr().out == (
        f"loaded the manifest of jaffle_shop from {run_loc}: 49 nodes, 42 versions, 53 edges, "
        f"0 newly removed\n"
        f"loaded 6 reports\n"
        f"loaded invocation {RUN_ID} (build, warning): 48 node results\n"
    )
    assert row_counts(store) == {
        "project": 1, "job": 0, "node": 49, "node_version": 42, "node_edge": 53, "invocation": 1,
        "node_result": 48, "report": 6, "report_render": 0,
    }
    with store.session() as session:
        invocation = session.get(m.Invocation, RUN_ID)
        claim = session.get(m.Project, "jaffle_shop")
    manifest_hash = hashlib.sha256((run_loc / "manifest.json").read_bytes()).hexdigest()
    assert (invocation.command, invocation.result, invocation.manifest_hash) == ("build", "warning", manifest_hash)
    assert (invocation.adapter_type, invocation.target, invocation.run_by, invocation.host) == (
        "duckdb", "unknown", None, None
    )
    assert claim.folder == "jaffle-shop"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_loading_twice_gives_identical_rows(
    store: Store,
    example_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_artifacts(example_platform / "artifacts", sources=True)
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 0
    first_counts = row_counts(store)
    first_rows = all_rows(store)
    assert load(store, "--run-loc", "artifacts") == 0
    assert row_counts(store) == first_counts
    assert all_rows(store) == first_rows
    assert "warning" not in capsys.readouterr().err


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_load_sources_json(
    store: Store,
    example_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_artifacts(example_platform / "artifacts", run_results=False, sources=True)
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 0
    # The example project has no sources: both results are skipped.
    assert f"loaded invocation {FRESHNESS_ID} (source freshness, failure): 0 node results, 2 skipped " \
        f"(no stored node)\n" in capsys.readouterr().out
    with store.session() as session:
        invocation = session.get(m.Invocation, FRESHNESS_ID)
    assert (invocation.command, invocation.manifest_hash, invocation.args) == ("source freshness", None, {})


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_run_loc_defaults_to_the_projects_target(
    store: Store,
    example_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_artifacts(example_platform / "jaffle-shop" / "target")
    monkeypatch.chdir(example_platform)
    assert load(store) == 0
    assert row_counts(store)["invocation"] == 1


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_an_unrecorded_command_is_not_loaded(
    store: Store,
    example_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_loc = write_artifacts(example_platform / "artifacts")
    run_results = json.loads((run_loc / "run_results.json").read_text(encoding="utf-8"))
    run_results["args"]["which"] = "ls"
    (run_loc / "run_results.json").write_text(json.dumps(run_results), encoding="utf-8")
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 0
    assert "dbtx: warning: run_results.json is from `dbt ls`, which dbtx does not record" in capsys.readouterr().err
    assert row_counts(store)["invocation"] == 0
    assert row_counts(store)["node"] == 49


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_invalid_report_definitions_skip_the_report_sync(
    store: Store,
    example_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_artifacts(example_platform / "artifacts")
    shutil.rmtree(example_platform / "jaffle-shop" / "exposure_templates")
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 0
    assert "dbtx: warning: reports not loaded: " in capsys.readouterr().err
    counts = row_counts(store)
    assert (counts["report"], counts["node"], counts["invocation"]) == (0, 49, 1)


# --------------------------------------------------------------------------------------
# Refusals: exit 2, nothing written
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_missing_run_loc(
    store: Store, example_platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "nowhere") == 2
    assert f"dbtx: {example_platform / 'nowhere'} has no manifest.json" in capsys.readouterr().err
    assert row_counts(store)["node"] == 0


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_no_run_artifact(
    store: Store, example_platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_loc = write_artifacts(example_platform / "artifacts", run_results=False)
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 2
    assert f"dbtx: {run_loc} has no run_results.json or sources.json; nothing to load" in capsys.readouterr().err
    assert row_counts(store)["node"] == 0


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_another_projects_manifest(
    store: Store, example_platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_loc = write_artifacts(example_platform / "artifacts")
    manifest = json.loads((run_loc / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["project_name"] = "other_shop"
    (run_loc / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 2
    assert (
        f"dbtx: {run_loc / 'manifest.json'} belongs to project 'other_shop', but jaffle-shop "
        f"is project 'jaffle_shop'"
    ) in capsys.readouterr().err
    assert row_counts(store)["node"] == 0


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_another_folders_claim(
    store: Store, example_platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with store.begin() as session:
        session.add(m.Project(project="jaffle_shop", folder="elsewhere", claimed_at=CLOCK_START, last_seen_at=CLOCK_START))
    write_artifacts(example_platform / "artifacts")
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 2
    assert "project name 'jaffle_shop' is owned by folder 'elsewhere'" in capsys.readouterr().err
    assert row_counts(store)["node"] == 0


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_an_unknown_status_in_strict_mode_writes_nothing(
    store: Store, example_platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_loc = write_artifacts(example_platform / "artifacts")
    run_results = json.loads((run_loc / "run_results.json").read_text(encoding="utf-8"))
    run_results["results"][0]["status"] = "exploded"
    (run_loc / "run_results.json").write_text(json.dumps(run_results), encoding="utf-8")
    monkeypatch.chdir(example_platform)
    assert load(store, "--run-loc", "artifacts") == 2
    assert "unknown value 'exploded'" in capsys.readouterr().err
    counts = row_counts(store)
    assert (counts["node"], counts["invocation"], counts["node_result"]) == (0, 0, 0)


def test_unknown_arguments(
    example_platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(example_platform)
    uri = f"sqlite:///{tmp_path / 'dbtx.db'}"
    assert cli.main(["db", "load", "--project-dir", "jaffle-shop", "--nope", "--db-uri", uri]) == 2
    assert capsys.readouterr().err.endswith("dbtx: usage: dbtx db load --project-dir DIR [--run-loc DIR]\n")
