import importlib.util

from tests.conftest import FROZEN_MANIFEST, REPO


def _freeze_module():
    spec = importlib.util.spec_from_file_location("freeze_manifest", REPO / "scripts" / "freeze_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_manifest_matches_example_project():
    freeze = _freeze_module()
    current = freeze.dumps(freeze.frozen_manifest())
    assert current == FROZEN_MANIFEST.read_text(encoding="utf-8"), (
        "tests/fixtures/manifest.json is stale; run `python scripts/freeze_manifest.py`"
    )
