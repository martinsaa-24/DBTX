"""The `dbtx_runtime.json` sidecar: accumulated run data living beside the docs.

The sidecar is the accumulator for run data across dbt invocations. It is owned
entirely by dbtx and never written by dbt, so it survives `dbt docs generate`
and `dbt run` -- which is the whole reason the run data is not injected into
`manifest.json`, a dbt-owned artifact that every invocation overwrites.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from dbtx.docs.catalog import DEFAULT_HISTORY_LIMIT, DocsCatalog, NodeResult

SIDECAR_FILENAME: str = "dbtx_runtime.json"
SIDECAR_SCHEMA_VERSION: int = 1


class SidecarError(Exception):
    """Raised when a sidecar file cannot be used as-is."""


def sidecar_path(docs_loc: Path) -> Path:
    return Path(docs_loc) / SIDECAR_FILENAME


def read_sidecar(docs_loc: Path) -> dict:
    """Loads the sidecar, returning an empty document when it does not exist yet."""
    path = sidecar_path(docs_loc)
    if not path.exists():
        return {
            'dbtx_schema_version': SIDECAR_SCHEMA_VERSION,
            'history_limit': DEFAULT_HISTORY_LIMIT,
            'nodes': {},
        }

    try:
        dat = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise SidecarError(f"{path} is not valid JSON: {exc}") from exc

    version = dat.get('dbtx_schema_version')
    if version != SIDECAR_SCHEMA_VERSION:
        raise SidecarError(
            f"{path} has dbtx_schema_version {version!r}, this build writes "
            f"{SIDECAR_SCHEMA_VERSION}")
    dat.setdefault('nodes', {})
    dat.setdefault('history_limit', DEFAULT_HISTORY_LIMIT)
    return dat


def resolve_history_limit(docs_loc: Path, requested: Optional[int] = None) -> int:
    """History cap, as an explicit request, else the sidecar's own, else the default.

    Persisting the cap means a sequence of patches keeps a consistent depth
    without the caller having to pass `--history` every time.
    """
    if requested is not None:
        return max(requested, 1)
    try:
        return max(int(read_sidecar(docs_loc).get('history_limit', DEFAULT_HISTORY_LIMIT)), 1)
    except SidecarError:
        return DEFAULT_HISTORY_LIMIT


def hydrate(catalog: DocsCatalog, docs_loc: Path) -> int:
    """Loads previously accumulated run history onto an already-parsed catalog.

    This is the step that makes a patch additive: without it a catalog holds
    only the run just parsed, and persisting that would discard every earlier
    run's data for nodes the new run did not touch.

    Entries for nodes no longer in the docs manifest are dropped. Returns the
    number of nodes hydrated.
    """
    if not catalog.parsed:
        raise SidecarError("parse_manifest() must run before hydrate()")

    dat = read_sidecar(docs_loc)
    hydrated = 0
    for unique_id, entry in dat['nodes'].items():
        doc_node = catalog.nodes.get(unique_id)
        if doc_node is None:
            continue
        runs = [NodeResult.from_dict(unique_id, r) for r in entry.get('runs', [])]
        doc_node.run_history = runs[:catalog.history_limit]
        if runs:
            hydrated += 1

    catalog.recompute_staleness()
    return hydrated


def write(catalog: DocsCatalog, docs_loc: Path) -> Path:
    """Serializes the catalog's accumulated run history to the sidecar, atomically.

    Written via a temp file and `os.replace` so an interrupted write cannot
    truncate what is the only copy of the accumulated history.
    """
    nodes: Dict[str, dict] = {}
    for unique_id, doc_node in catalog.nodes.items():
        if not doc_node.run_history:
            continue
        nodes[unique_id] = {'runs': [r.to_dict() for r in doc_node.run_history]}

    dat = {
        'dbtx_schema_version': SIDECAR_SCHEMA_VERSION,
        'history_limit': catalog.history_limit,
        'docs_invocation_id': catalog.metadata.get('invocation_id'),
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'nodes': nodes,
    }

    path = sidecar_path(docs_loc)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(dat, indent=2), encoding='utf-8')
    os.replace(tmp, path)
    return path
