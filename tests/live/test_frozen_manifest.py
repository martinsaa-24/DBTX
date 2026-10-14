import importlib.util
from types import ModuleType

from tests.conftest import FROZEN_MANIFEST, FROZEN_RUN_RESULTS, FROZEN_SOURCES, REPO


def _freeze_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("freeze_manifest", REPO / "scripts" / "freeze_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_manifest_matches_example_project() -> None:
    freeze = _freeze_module()
    current = freeze.dumps(freeze.frozen_manifest())
    assert current == FROZEN_MANIFEST.read_text(encoding="utf-8"), (
        "tests/fixtures/manifest.json is stale; run `python scripts/freeze_manifest.py`"
    )


def test_frozen_run_results_match_example_project() -> None:
    freeze = _freeze_module()
    current = freeze.dumps(freeze.frozen_run_results())
    assert current == FROZEN_RUN_RESULTS.read_text(encoding="utf-8"), (
        "tests/fixtures/run_results.json is stale; run `python scripts/freeze_manifest.py`"
    )


def test_frozen_sources_match_example_project() -> None:
    # A real `dbt source freshness` of the example's source (it used to be hand-written).
    freeze = _freeze_module()
    current = freeze.dumps(freeze.frozen_sources())
    assert current == FROZEN_SOURCES.read_text(encoding="utf-8"), (
        "tests/fixtures/sources.json is stale; run `python scripts/freeze_manifest.py`"
    )
