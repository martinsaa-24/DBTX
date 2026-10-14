"""The manifest loader (`dbtx.store.loaders.manifest`, plan 5.1).

Behaviour: `manifest_rows(manifest)` turns a dbt Manifest into what `sync_manifest`
stores; `read_manifest(path)` reads a `manifest.json` into a Manifest (dbt checks its
schema version), so the post-hoc and in-process paths share one mapping.
* `project`, `adapter_type`: the manifest's metadata. `seen_at`: `metadata.generated_at`
  (naive UTC in dbt 1.12) as an aware UTC datetime
* nodes: from `nodes`, `sources`, `exposures` and `unit_tests`, only the stored resource
  types (model, source, snapshot, seed, exposure, test, unit_test); analyses, hooks
  (operations), macros and the rest are dropped. `checksum` is the node's
  `checksum.checksum`, or None for a node without one (sources, exposures)
* node versions: one per node with a checksum
* edges: each stored node's `depends_on.nodes` (what dbt builds `parent_map` from),
  only where both ends are stored nodes
* rows are sorted by key, so the same manifest always gives the same rows
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dbt.contracts.graph.manifest import Manifest

from dbtx.store.loaders.manifest import ManifestRows, manifest_rows, read_manifest
from tests.conftest import FROZEN_MANIFEST

GENERATED_AT = datetime(2026, 1, 1, 0, 0, 0)  # naive, as dbt 1.12 writes it


def rows_of(manifest: Manifest) -> ManifestRows:
    manifest.metadata.generated_at = GENERATED_AT
    return manifest_rows(manifest)


def write_manifest(data: dict, path: Path) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def frozen_json() -> dict:
    data = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    data["metadata"]["generated_at"] = "2026-01-01T00:00:00Z"
    return data


def test_project_adapter_type_and_seen_at(manifest: Manifest) -> None:
    rows = rows_of(manifest)
    assert rows.project == "jaffle_shop"
    assert rows.adapter_type == "duckdb"
    assert rows.seen_at == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_node_counts_per_resource_type(manifest: Manifest) -> None:
    rows = rows_of(manifest)
    counts = Counter(node["resource_type"] for node in rows.nodes)
    assert counts == {"test": 26, "model": 11, "exposure": 7, "seed": 5, "snapshot": 1, "source": 1}


def test_node_rows(manifest: Manifest) -> None:
    rows = rows_of(manifest)
    by_id = {node["unique_id"]: node for node in rows.nodes}
    customers = manifest.nodes["model.jaffle_shop.customers"]
    assert by_id["model.jaffle_shop.customers"] == {
        "project": "jaffle_shop",
        "unique_id": "model.jaffle_shop.customers",
        "resource_type": "model",
        "package_name": "jaffle_shop",
        "name": "customers",
        "checksum": customers.checksum.checksum,
    }
    assert by_id["exposure.jaffle_shop.top_customers"]["checksum"] is None


def test_a_version_for_every_node_with_a_checksum(manifest: Manifest) -> None:
    rows = rows_of(manifest)
    with_checksum = [node for node in rows.nodes if node["checksum"] is not None]
    assert len(with_checksum) == 43
    assert rows.versions == [
        {"project": "jaffle_shop", "unique_id": node["unique_id"], "checksum": node["checksum"]}
        for node in with_checksum
    ]


def test_edges_match_the_manifests_parent_map(manifest: Manifest) -> None:
    rows = rows_of(manifest)
    parent_map = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))["parent_map"]
    expected = sorted(
        (parent, child)
        for child, parents in parent_map.items()
        for parent in parents
    )
    assert [(edge["parent_id"], edge["child_id"]) for edge in rows.edges] == expected
    assert {edge["project"] for edge in rows.edges} == {"jaffle_shop"}


def test_rows_are_sorted_by_key(manifest: Manifest) -> None:
    rows = rows_of(manifest)
    assert [node["unique_id"] for node in rows.nodes] == sorted(node["unique_id"] for node in rows.nodes)
    assert [(e["parent_id"], e["child_id"]) for e in rows.edges] == sorted(
        (e["parent_id"], e["child_id"]) for e in rows.edges
    )


def test_read_manifest_from_a_file(tmp_path: Path) -> None:
    path = write_manifest(frozen_json(), tmp_path / "manifest.json")
    rows = manifest_rows(read_manifest(path))
    assert rows.seen_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert len(rows.nodes) == 51


def test_unstored_resource_types_and_their_edges_are_dropped(tmp_path: Path) -> None:
    data = frozen_json()
    model = data["nodes"]["model.jaffle_shop.customers"]
    # An analysis that depends on a model, and a test that depends on the analysis.
    analysis = copy.deepcopy(model)
    analysis.update(
        unique_id="analysis.jaffle_shop.revenue_check",
        resource_type="analysis",
        name="revenue_check",
        fqn=["jaffle_shop", "analysis", "revenue_check"],
    )
    analysis["depends_on"]["nodes"] = ["model.jaffle_shop.customers"]
    data["nodes"][analysis["unique_id"]] = analysis
    hook = copy.deepcopy(model)
    hook.update(
        unique_id="operation.jaffle_shop.jaffle_shop-on-run-end-0",
        resource_type="operation",
        name="jaffle_shop-on-run-end-0",
        fqn=["jaffle_shop", "hooks", "jaffle_shop-on-run-end-0"],
    )
    hook["depends_on"]["nodes"] = []
    data["nodes"][hook["unique_id"]] = hook
    test_id = "test.jaffle_shop.not_null_customers_customer_id.5c9bf9911d"
    data["nodes"][test_id]["depends_on"]["nodes"].append(analysis["unique_id"])

    rows = manifest_rows(read_manifest(write_manifest(data, tmp_path / "manifest.json")))

    stored = {node["unique_id"] for node in rows.nodes}
    assert analysis["unique_id"] not in stored
    assert hook["unique_id"] not in stored
    assert len(rows.nodes) == 51
    for edge in rows.edges:
        assert edge["parent_id"] in stored and edge["child_id"] in stored
    assert (analysis["unique_id"], test_id) not in {(e["parent_id"], e["child_id"]) for e in rows.edges}
