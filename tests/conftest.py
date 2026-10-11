from __future__ import annotations

import copy
from pathlib import Path

import pytest
from dbt.contracts.graph.manifest import Manifest, WritableManifest

from dbtx.reports import Report, discover_reports
from dbtx.templates import TemplateEngine, TemplateRegistry

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_PROJECT = REPO / "examples" / "jaffle_shop"
FROZEN_MANIFEST = Path(__file__).parent / "fixtures" / "manifest.json"
# A real `dbt build` of the example project, scrubbed (scripts/freeze_manifest.py).
FROZEN_RUN_RESULTS = Path(__file__).parent / "fixtures" / "run_results.json"
# Hand-written: the example project has no source freshness (plan 8).
FROZEN_SOURCES = Path(__file__).parent / "fixtures" / "sources.json"


@pytest.fixture(autouse=True)
def store_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole suite runs the store in strict mode (plan 8, D9): an app-enforced
    value dbtx does not know raises instead of being stored with a warning."""
    monkeypatch.setenv("DBTX_DB_STRICT", "1")


def pytest_addoption(parser: pytest.Parser) -> None:
    # Registered here, in the top-level conftest, so pytest accepts the option whichever
    # tests are selected. Used by tests/unit/store/conftest.py (`postgres_uri`).
    parser.addoption(
        "--postgres-uri",
        default=None,
        help="Run the Postgres store tests against this server instead of a throwaway "
        "pgserver, e.g. postgresql+psycopg://user:password@localhost:5432/dbtx_test",
    )


def check_postgres_uri_option(value: str | None) -> None:
    """A blank --postgres-uri is a mistake (for example an unset CI variable), never a
    request for the default: stop the run instead of silently using pgserver."""
    if value is None:
        return
    if value.strip() == "":
        raise pytest.UsageError(
            "--postgres-uri is empty; pass a Postgres URI, or leave the option out to use "
            "a throwaway pgserver"
        )


def pytest_configure(config: pytest.Config) -> None:
    check_postgres_uri_option(config.getoption("postgres_uri"))


@pytest.fixture(scope="session")
def _frozen_manifest() -> Manifest:
    writable = WritableManifest.read_and_check_versions(str(FROZEN_MANIFEST))
    return Manifest.from_writable_manifest(writable)


@pytest.fixture
def manifest(_frozen_manifest: Manifest) -> Manifest:
    """The example project's manifest, loaded from the frozen JSON. Safe to mutate."""
    return copy.deepcopy(_frozen_manifest)


@pytest.fixture
def templates() -> TemplateEngine:
    """Templates as resolved for the example project (its report_templates + integrated)."""
    return TemplateEngine(TemplateRegistry.for_project(EXAMPLE_PROJECT))


@pytest.fixture
def reports(manifest: Manifest, templates: TemplateEngine) -> dict[str, Report]:
    """Reports keyed by name."""
    return {r.name: r for r in discover_reports(manifest, templates, EXAMPLE_PROJECT).values()}
