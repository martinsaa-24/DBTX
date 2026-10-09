"""Exposure reports: catalog coverage, discovery beside the docs, and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbtx.docs import exposure_reports, sidecar
from dbtx.docs.catalog import DocsCatalog
from dbtx.docs.exposure_reports import REPORTS_SUBDIR, ReportAsset
from dbtx.docs.patch import patch, status

EXPOSURE = "exposure.proj.beverage_leaderboard"
MODEL = "model.proj.product_mix"


def _manifest() -> dict:
    """A docs manifest with one model and two exposures, as dbt writes it:
    exposures in their own top-level section, never under `nodes`."""
    return {
        "metadata": {"invocation_id": "docs-inv", "generated_at": "2026-01-01T00:00:00Z"},
        "nodes": {
            MODEL: {
                "resource_type": "model",
                "name": "product_mix",
                "checksum": {"name": "sha256", "checksum": "sum-product_mix"},
            }
        },
        "exposures": {
            EXPOSURE: {
                "resource_type": "exposure",
                "name": "beverage_leaderboard",
                "label": "Best-Selling Beverages",
            },
            "exposure.proj.store_scorecard": {
                "resource_type": "exposure",
                "name": "store_scorecard",
            },
        },
    }


def _run_results() -> dict:
    """dbt reports an exposure in run_results like any other node, as a no-op."""
    return {
        "metadata": {"invocation_id": "run-1", "generated_at": "2026-01-02T00:00:00Z", "env": {}},
        "args": {"which": "build", "vars": {}},
        "results": [
            {
                "unique_id": MODEL,
                "status": "success",
                "execution_time": 1.0,
                "timing": [{"name": "execute", "completed_at": "2026-01-02T00:00:01Z"}],
            },
            {
                "unique_id": EXPOSURE,
                "status": "no-op",
                "execution_time": 0.0,
                "timing": [],
            },
        ],
    }


@pytest.fixture
def docs_loc(tmp_path: Path) -> Path:
    loc = tmp_path / "target"
    loc.mkdir()
    (loc / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (loc / "index.html").write_text("<html><body>manifest.json</body></html>", encoding="utf-8")
    return loc


@pytest.fixture
def run_loc(tmp_path: Path) -> Path:
    loc = tmp_path / "run"
    loc.mkdir()
    (loc / "run_results.json").write_text(json.dumps(_run_results()), encoding="utf-8")
    (loc / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    return loc


def _write_report(docs_loc: Path, name: str, csv: bool = False) -> Path:
    reports = docs_loc / REPORTS_SUBDIR
    reports.mkdir(exist_ok=True)
    html = reports / f"{name}.html"
    html.write_text("<!doctype html><html><body><table></table></body></html>", encoding="utf-8")
    if csv:
        (reports / f"{name}.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return html


def _catalog(docs_loc: Path) -> DocsCatalog:
    catalog = DocsCatalog()
    catalog.parse_manifest(str(docs_loc / "manifest.json"))
    return catalog


def test_catalog_includes_exposures(docs_loc: Path):
    """Exposures live outside `nodes` in the manifest but share its keyspace here,
    so run results and reports can be hung on them."""
    nodes = _catalog(docs_loc).nodes
    assert EXPOSURE in nodes
    assert MODEL in nodes


def test_exposure_run_results_are_no_longer_ignored(docs_loc: Path, run_loc: Path):
    result = patch(docs_loc=docs_loc, run_loc=run_loc)
    assert EXPOSURE in result.tally.patched
    assert EXPOSURE not in result.tally.ignored


def test_exposure_is_never_stale(docs_loc: Path, run_loc: Path):
    """An exposure has no SQL, so it has no checksum on either side and must not
    be flagged against the name-derived fallback."""
    patch(docs_loc=docs_loc, run_loc=run_loc)
    entry = json.loads((docs_loc / sidecar.SIDECAR_FILENAME).read_text())["nodes"][EXPOSURE]
    assert entry["runs"][0]["stale"] is False


def test_discovery_finds_html_and_optional_csv(docs_loc: Path):
    _write_report(docs_loc, "beverage_leaderboard", csv=True)
    _write_report(docs_loc, "store_scorecard", csv=False)

    found = exposure_reports.discover(_catalog(docs_loc).nodes, docs_loc)

    assert found[EXPOSURE].html == f"{REPORTS_SUBDIR}/beverage_leaderboard.html"
    assert found[EXPOSURE].csv == f"{REPORTS_SUBDIR}/beverage_leaderboard.csv"
    assert found[EXPOSURE].size_bytes > 0
    assert found[EXPOSURE].generated_at is not None
    assert found["exposure.proj.store_scorecard"].csv is None


def test_discovery_ignores_files_not_matching_an_exposure(docs_loc: Path):
    """Looked up per exposure by name, so a stray file is never attributed to one."""
    _write_report(docs_loc, "beverage_leaderboard")
    _write_report(docs_loc, "something_else")

    found = exposure_reports.discover(_catalog(docs_loc).nodes, docs_loc)

    assert set(found) == {EXPOSURE}


def test_discovery_skips_models_and_a_missing_directory(docs_loc: Path):
    assert exposure_reports.discover(_catalog(docs_loc).nodes, docs_loc) == {}
    _write_report(docs_loc, "product_mix")  # a model's name, not an exposure's
    assert exposure_reports.discover(_catalog(docs_loc).nodes, docs_loc) == {}


def test_patch_persists_the_report_for_the_overlay(docs_loc: Path, run_loc: Path):
    _write_report(docs_loc, "beverage_leaderboard", csv=True)

    result = patch(docs_loc=docs_loc, run_loc=run_loc)

    assert result.reports_found == 1
    entry = json.loads((docs_loc / sidecar.SIDECAR_FILENAME).read_text())["nodes"][EXPOSURE]
    assert entry["report"]["html"] == f"{REPORTS_SUBDIR}/beverage_leaderboard.html"
    assert entry["report"]["csv"] == f"{REPORTS_SUBDIR}/beverage_leaderboard.csv"


def test_a_deleted_report_disappears_from_the_sidecar(docs_loc: Path, run_loc: Path):
    """Reports are rediscovered each patch rather than hydrated, so the sidecar
    never advertises a file the overlay would 404 on."""
    html = _write_report(docs_loc, "beverage_leaderboard")
    patch(docs_loc=docs_loc, run_loc=run_loc)

    html.unlink()
    result = patch(docs_loc=docs_loc, run_loc=run_loc, force=True)

    assert result.reports_found == 0
    entry = json.loads((docs_loc / sidecar.SIDECAR_FILENAME).read_text())["nodes"][EXPOSURE]
    assert "report" not in entry
    assert entry["runs"], "run history must survive the report going away"


def test_status_counts_reports(docs_loc: Path, run_loc: Path):
    _write_report(docs_loc, "beverage_leaderboard")
    patch(docs_loc=docs_loc, run_loc=run_loc)
    assert status(docs_loc).reports_found == 1


def test_report_asset_round_trips():
    asset = ReportAsset(html="reports/x.html", csv="reports/x.csv",
                        generated_at="2026-01-01T00:00:00Z", size_bytes=12)
    assert ReportAsset.from_dict(asset.to_dict()) == asset
