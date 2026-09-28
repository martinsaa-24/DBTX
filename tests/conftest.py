from __future__ import annotations

import copy
from pathlib import Path

import pytest
from dbt.contracts.graph.manifest import Manifest, WritableManifest

from dbtx.reports import discover_reports
from dbtx.templates import TemplateEngine, TemplateRegistry

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_PROJECT = REPO / "examples" / "jaffle_shop"
FROZEN_MANIFEST = Path(__file__).parent / "fixtures" / "manifest.json"


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
def reports(manifest, templates):
    """Reports keyed by name."""
    return {r.name: r for r in discover_reports(manifest, templates, EXAMPLE_PROJECT).values()}
