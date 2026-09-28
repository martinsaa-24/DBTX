"""Freeze examples/jaffle_shop's manifest into tests/fixtures/manifest.json.

    python scripts/freeze_manifest.py

Run this after changing the example project. The frozen file is scrubbed of anything
machine- or run-specific (absolute paths, timestamps, user ids, Windows separators) and of
dbt's built-in macros, so it is small, diffable and identical on every machine.
`tests/integration/test_frozen_manifest.py` fails when it drifts from the project.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROJECT_DIR = REPO / "examples" / "jaffle_shop"
FROZEN_PATH = REPO / "tests" / "fixtures" / "manifest.json"
PROJECT_NAME = "jaffle_shop"

_PATH_KEYS = {"original_file_path", "path", "patch_path"}
_VOLATILE_METADATA = {
    "generated_at", "invocation_id", "invocation_started_at", "run_started_at", "user_id", "env"
}


def parse_manifest() -> dict[str, Any]:
    """`dbt parse` the example project into a throwaway target dir and return the raw JSON."""
    from dbt.cli.main import dbtRunner

    with tempfile.TemporaryDirectory() as tmp:
        result = dbtRunner().invoke(
            [
                "parse",
                "--project-dir", str(PROJECT_DIR),
                "--profiles-dir", str(PROJECT_DIR),
                "--target-path", tmp,
                "--no-partial-parse",
                "--quiet",
            ]
        )
        if not result.success:
            raise RuntimeError(f"dbt parse failed: {result.exception}")
        return json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))


def scrub(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["metadata"] = {
        k: v for k, v in manifest["metadata"].items() if k not in _VOLATILE_METADATA
    }
    manifest["metadata"]["send_anonymous_usage_stats"] = False
    manifest["macros"] = {
        uid: m for uid, m in manifest["macros"].items() if m["package_name"] == PROJECT_NAME
    }
    manifest["docs"] = {
        uid: d for uid, d in manifest["docs"].items() if d["package_name"] == PROJECT_NAME
    }

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            if obj.get("name") == "path" and isinstance(obj.get("checksum"), str):
                # Seeds too large to hash are "checksummed" by their (OS-specific) path.
                return {**obj, "checksum": obj["checksum"].replace("\\", "/")}
            out = {}
            for key, value in obj.items():
                if key == "root_path":
                    value = "."
                elif key == "created_at":
                    value = 0
                elif key in _PATH_KEYS and isinstance(value, str):
                    value = value.replace("\\", "/")
                out[key] = walk(value)
            return out
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj

    return walk(manifest)


def frozen_manifest() -> dict[str, Any]:
    return scrub(parse_manifest())


def dumps(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=1, sort_keys=True) + "\n"


def main() -> None:
    # Parsing writes partial-parse state under the project; keep the example clean.
    shutil.rmtree(PROJECT_DIR / "logs", ignore_errors=True)
    FROZEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    FROZEN_PATH.write_text(dumps(frozen_manifest()), encoding="utf-8", newline="\n")
    print(f"wrote {FROZEN_PATH.relative_to(REPO)} ({FROZEN_PATH.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
