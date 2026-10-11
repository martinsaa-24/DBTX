"""A dbt manifest → node, node-version and edge rows (plan 5.1).

The in-process path has the Manifest dbt already parsed; the post-hoc path reads
`manifest.json` into one (`read_manifest`). Both then go through `manifest_rows`, so
there is one mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dbt.contracts.graph.manifest import Manifest, WritableManifest

from dbtx.store.enums import NodeResourceType, enum_values

STORED_RESOURCE_TYPES = frozenset(enum_values(NodeResourceType))


@dataclass(frozen=True)
class ManifestRows:
    """What `nodes.sync_manifest` stores from one manifest. Rows are sorted by key."""

    project: str
    adapter_type: str
    # When the manifest was generated (aware UTC): the time every row was seen.
    seen_at: datetime
    # {project, unique_id, resource_type, package_name, name, checksum}
    nodes: list[dict[str, Any]]
    # {project, unique_id, checksum}, for the nodes with a checksum
    versions: list[dict[str, Any]]
    # {project, parent_id, child_id}, both ends in `nodes`
    edges: list[dict[str, Any]]


def read_manifest(path: Path) -> Manifest:
    """A `manifest.json` as a Manifest. dbt checks the artifact's schema version."""
    writable = WritableManifest.read_and_check_versions(str(path))
    return Manifest.from_writable_manifest(writable)


def as_utc(value: datetime) -> datetime:
    """dbt's timestamps are naive UTC; make them aware."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def node_checksum(node: Any) -> Optional[str]:
    """`checksum.checksum`, or None for a node without one (sources, exposures)."""
    checksum = getattr(node, "checksum", None)
    if checksum is None:
        return None
    return checksum.checksum


def node_parents(node: Any) -> list[str]:
    """The node's direct parents: `depends_on.nodes` (what dbt's parent_map is built from)."""
    depends_on = getattr(node, "depends_on", None)
    if depends_on is None:
        return []
    return list(getattr(depends_on, "nodes", None) or [])


def manifest_rows(manifest: Manifest) -> ManifestRows:
    project = manifest.metadata.project_name
    stored = {}
    for collection in (manifest.nodes, manifest.sources, manifest.exposures, manifest.unit_tests):
        for unique_id, node in collection.items():
            resource_type = str(node.resource_type.value)
            if resource_type not in STORED_RESOURCE_TYPES:
                continue
            stored[unique_id] = node

    nodes = []
    versions = []
    edges = []
    for unique_id in sorted(stored):
        node = stored[unique_id]
        checksum = node_checksum(node)
        nodes.append(
            {
                "project": project,
                "unique_id": unique_id,
                "resource_type": str(node.resource_type.value),
                "package_name": node.package_name,
                "name": node.name,
                "checksum": checksum,
            }
        )
        if checksum is not None:
            versions.append({"project": project, "unique_id": unique_id, "checksum": checksum})
        for parent_id in sorted(set(node_parents(node))):
            if parent_id in stored:
                edges.append({"project": project, "parent_id": parent_id, "child_id": unique_id})

    edges.sort(key=lambda edge: (edge["parent_id"], edge["child_id"]))
    return ManifestRows(
        project=project,
        adapter_type=manifest.metadata.adapter_type,
        seen_at=as_utc(manifest.metadata.generated_at),
        nodes=nodes,
        versions=versions,
        edges=edges,
    )
