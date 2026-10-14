"""The docs site in platform mode (plan Phase 6, docs/flows/docs_refresh.md).

The docs site is the project's own target folder (`config.project_target_dir`), which
runs no longer write to. Behaviour:

After a platform command that executes nodes (build, run, test, seed, snapshot):
* the run folder's `reports/` (when the run rendered any) is copied into
  `<docs site>/reports/` (merged: other reports stay), whether or not a docs site exists
* the docs site is patched with the run (`dbtx docs patch --docs-loc <site> --run-loc
  <run folder>`, run internally), whatever dbt's exit code
* no docs site yet (no `index.html` or `manifest.json`), or no `run_results.json` in the
  run folder: a note, nothing patched
* a patch that fails is a warning on stderr: the exit code stays dbt's

After `dbtx docs generate` (platform): `index.html`, `manifest.json` and `catalog.json`
are copied from the run folder into the docs site, with a note to run `dbtx docs
install`. Only when dbt succeeded.

`dbtx docs install`, `status`, `serve` (platform): without `--docs-loc`, the docs site
of the project named by `--project-dir`; with `--docs-loc`, no project is needed.
`dbtx docs patch` from the command line keeps its explicit `--docs-loc` / `--run-loc`.
Standalone mode: none of this; `--docs-loc` is required as before.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from dbtx import cli
from tests.conftest import FROZEN_MANIFEST, FROZEN_RUN_RESULTS
from tests.unit.conftest import RUN_JOB_ID, initialise_database, LayoutBuilder

RUN = ["run", "--project-dir", "jaffle-shop"]


def run_folder(platform: Path) -> Path:
    return platform / ".dbtx" / "logs" / "jaffle_shop" / "2026-10-12" / "14" / RUN_JOB_ID


class ArtifactWritingDbt:
    """Stands in for dbtRunner in dbtx.cli: writes what dbt would into --target-path."""

    writes: dict[str, bytes] = {}
    success = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def invoke(self, args: list[str]) -> Any:
        target = Path(args[args.index("--target-path") + 1])
        for relative, content in ArtifactWritingDbt.writes.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        class Result:
            success = ArtifactWritingDbt.success
            exception = None
            result = None

        return Result()


@pytest.fixture
def dbt_writes(monkeypatch: pytest.MonkeyPatch, fixed_run: None) -> type[ArtifactWritingDbt]:
    ArtifactWritingDbt.writes = {
        "run_results.json": FROZEN_RUN_RESULTS.read_bytes(),
        "manifest.json": FROZEN_MANIFEST.read_bytes(),
    }
    ArtifactWritingDbt.success = True
    monkeypatch.setattr(cli, "dbtRunner", ArtifactWritingDbt)
    return ArtifactWritingDbt


def docs_site(platform: Path) -> Path:
    """A generated docs site in the project's target folder."""
    site = platform / "jaffle-shop" / "target"
    site.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FROZEN_MANIFEST, site / "manifest.json")
    (site / "index.html").write_text(
        '<html><head></head><body><script>fetch("manifest.json")</script></body></html>', encoding="utf-8"
    )
    return site


@pytest.fixture(autouse=True)
def database(request: pytest.FixtureRequest) -> None:
    """A test on the standard platform starts from an initialised database, as a user's
    first run does after `dbtx db init` (schema changes are deliberate)."""
    if "platform" in request.fixturenames:
        initialise_database(request.getfixturevalue("platform"))


# --------------------------------------------------------------------------------------
# After a node-executing run
# --------------------------------------------------------------------------------------


def test_a_run_patches_the_docs_site(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = docs_site(platform)
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    sidecar = json.loads((site / "dbtx_runtime.json").read_text(encoding="utf-8"))
    [latest] = sidecar["nodes"]["model.jaffle_shop.customers"]["runs"]
    assert latest["status"] == "success"
    out = capsys.readouterr().out
    assert f"dbtx docs: patched the docs site {site} with this run" in out


def test_rendered_reports_are_copied_into_the_docs_site(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
) -> None:
    site = docs_site(platform)
    (site / "reports").mkdir()
    (site / "reports" / "older.html").write_text("an earlier run's report")
    dbt_writes.writes["reports/top_customers.html"] = b"<table></table>"
    dbt_writes.writes["reports/top_customers.csv"] = b"a,b\n"
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    assert sorted(path.name for path in (site / "reports").iterdir()) == [
        "older.html", "top_customers.csv", "top_customers.html"
    ]
    assert (site / "reports" / "top_customers.html").read_bytes() == b"<table></table>"


def test_reports_are_copied_even_without_a_docs_site(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
    capsys: pytest.CaptureFixture[str],
) -> None:
    dbt_writes.writes["reports/top_customers.html"] = b"<table></table>"
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    site = platform / "jaffle-shop" / "target"
    assert (site / "reports" / "top_customers.html").is_file()
    assert (
        f"dbtx docs: no docs site in {site} to patch; run `dbtx docs generate`, then `dbtx docs install`"
        in capsys.readouterr().out
    )


def test_a_failed_run_still_patches(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
) -> None:
    site = docs_site(platform)
    dbt_writes.success = False
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 1
    assert (site / "dbtx_runtime.json").is_file()


def test_no_run_results_means_no_patch(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = docs_site(platform)
    dbt_writes.writes = {}  # --no-write-json, or dbt stopped before running anything
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    assert not (site / "dbtx_runtime.json").exists()
    assert (
        f"dbtx docs: no run_results.json in {run_folder(platform)}; the docs site was not patched"
        in capsys.readouterr().out
    )


def test_a_failing_patch_is_a_warning(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = docs_site(platform)
    (site / "manifest.json").write_text("{not json")
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    err = capsys.readouterr().err
    assert f"dbtx: warning: the docs site {site} was not patched: " in err


@pytest.mark.parametrize("argv", [["parse"], ["run-operation", "noop"], ["source", "freshness"]])
def test_commands_that_execute_no_nodes_do_not_patch(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt], argv: list[str],
) -> None:
    site = docs_site(platform)
    monkeypatch.chdir(platform)
    assert cli.main([*argv, "--project-dir", "jaffle-shop"]) == 0
    assert not (site / "dbtx_runtime.json").exists()


# --------------------------------------------------------------------------------------
# After dbtx docs generate
# --------------------------------------------------------------------------------------


def test_docs_generate_copies_the_site(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
    capsys: pytest.CaptureFixture[str],
) -> None:
    dbt_writes.writes = {
        "index.html": b"<html>docs</html>", "manifest.json": b"{}", "catalog.json": b"{}",
        "run_results.json": b"{}", "graph.gpickle": b"x",
    }
    monkeypatch.chdir(platform)
    assert cli.main(["docs", "generate", "--project-dir", "jaffle-shop"]) == 0
    site = platform / "jaffle-shop" / "target"
    assert sorted(path.name for path in site.iterdir()) == ["catalog.json", "index.html", "manifest.json"]
    assert (site / "index.html").read_bytes() == b"<html>docs</html>"
    assert capsys.readouterr().out.endswith(
        f"dbtx docs: copied index.html, manifest.json, catalog.json to {site}; run `dbtx docs install` "
        f"to add dbtx's overlay\n"
    )


def test_a_failed_docs_generate_copies_nothing(
    platform: Path, monkeypatch: pytest.MonkeyPatch, dbt_writes: type[ArtifactWritingDbt],
) -> None:
    dbt_writes.writes = {"index.html": b"<html>docs</html>"}
    dbt_writes.success = False
    monkeypatch.chdir(platform)
    assert cli.main(["docs", "generate", "--project-dir", "jaffle-shop"]) == 1
    assert not (platform / "jaffle-shop" / "target" / "index.html").exists()


# --------------------------------------------------------------------------------------
# --docs-loc defaults
# --------------------------------------------------------------------------------------


@pytest.fixture
def docs_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_docs_command(args: list[str]) -> int:
        calls.append(list(args))
        return 0

    monkeypatch.setattr(cli, "docs_command", fake_docs_command)
    return calls


@pytest.mark.parametrize("sub", ["install", "status", "serve"])
def test_docs_loc_defaults_to_the_projects_docs_site(
    platform: Path, monkeypatch: pytest.MonkeyPatch, docs_calls: list[list[str]], sub: str
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["docs", sub, "--project-dir", "jaffle-shop"]) == 0
    assert docs_calls == [[sub, "--docs-loc", str(platform / "jaffle-shop" / "target")]]


def test_an_explicit_docs_loc_needs_no_project(
    platform: Path, monkeypatch: pytest.MonkeyPatch, docs_calls: list[list[str]]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["docs", "serve", "--docs-loc", "/srv/site", "--port", "9000"]) == 0
    assert docs_calls == [["serve", "--docs-loc", "/srv/site", "--port", "9000"]]


def test_without_docs_loc_a_project_is_needed(
    platform: Path, monkeypatch: pytest.MonkeyPatch, docs_calls: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["docs", "status"]) == 2
    assert docs_calls == []
    assert "choose a project with --project-dir" in capsys.readouterr().err


def test_docs_patch_keeps_its_explicit_parameters(
    platform: Path, monkeypatch: pytest.MonkeyPatch, docs_calls: list[list[str]]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["docs", "patch", "--docs-loc", "site", "--run-loc", "run"]) == 0
    assert docs_calls == [["patch", "--docs-loc", "site", "--run-loc", "run"]]


def test_standalone_keeps_requiring_docs_loc(
    lone_project: Path, monkeypatch: pytest.MonkeyPatch, docs_calls: list[list[str]]
) -> None:
    monkeypatch.chdir(lone_project)
    assert cli.main(["docs", "status", "--standalone-mode"]) == 0
    assert docs_calls == [["status"]]  # the docs command reports the missing --docs-loc


def test_standalone_runs_touch_no_docs_site(
    lone_project: Path, layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch, fake_dbt: Any
) -> None:
    (lone_project / "target").mkdir()
    (lone_project / "target" / "manifest.json").write_text("{}")
    monkeypatch.chdir(lone_project)
    assert cli.main(["run", "--standalone-mode"]) == 0
    assert sorted(path.name for path in (lone_project / "target").iterdir()) == ["manifest.json"]
