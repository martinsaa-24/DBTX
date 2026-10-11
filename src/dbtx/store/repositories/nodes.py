"""The manifest sync: nodes, node versions and edges (plan 5.1).

Out-of-order loads (post-hoc) must come out the same as in-order ones, so:
* `first_seen_at` / `last_seen_at` merge with least / greatest
* a node's attributes, and clearing `removed_at`, follow the newest manifest loaded:
  they are updated only on rows whose `last_seen_at` is this manifest's time after the
  merge (this manifest is the newest that has seen them)
* removals are marked against the project's newest manifest time (the latest
  `last_seen_at` of its nodes), not this manifest's: loaded in order the two are the
  same, and an older manifest loaded later cannot undo a removal

The caller owns the transaction; the project's claim row must exist (foreign key).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.store import models as m
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.loaders.manifest import ManifestRows

# Rows per statement: well under SQLite's bound-parameter limit.
BATCH_SIZE = 500

SEEN_MERGE = {"first_seen_at": "least", "last_seen_at": "greatest"}


@dataclass(frozen=True)
class SyncCounts:
    nodes: int
    versions: int
    edges: int
    # Newly marked removed by this sync.
    removed_nodes: int
    removed_edges: int


def batches(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    result = []
    for start in range(0, len(rows), BATCH_SIZE):
        result.append(rows[start : start + BATCH_SIZE])
    return result


def sync_manifest(session: Session, adapter: StoreAdapter, rows: ManifestRows) -> SyncCounts:
    node_table = m.Node.__table__
    version_table = m.NodeVersion.__table__
    edge_table = m.NodeEdge.__table__
    seen_at = rows.seen_at

    # 1. Nodes: a new node is inserted whole; a known one merges its sighting times.
    for batch in batches(rows.nodes):
        values = []
        for row in batch:
            values.append({**row, "first_seen_at": seen_at, "last_seen_at": seen_at, "removed_at": None})
        session.execute(
            adapter.upsert(node_table, values, keys=["project", "unique_id"], update=[], merge=SEEN_MERGE)
        )
    # Attributes follow the newest manifest: only rows this manifest is the newest for.
    update_attributes = (
        sa.update(node_table)
        .where(node_table.c.project == sa.bindparam("key_project"))
        .where(node_table.c.unique_id == sa.bindparam("key_unique_id"))
        .where(node_table.c.last_seen_at == seen_at)
        .values(
            resource_type=sa.bindparam("new_resource_type"),
            package_name=sa.bindparam("new_package_name"),
            name=sa.bindparam("new_name"),
            checksum=sa.bindparam("new_checksum"),
            removed_at=None,
        )
    )
    for batch in batches(rows.nodes):
        parameters = []
        for row in batch:
            parameters.append(
                {
                    "key_project": row["project"],
                    "key_unique_id": row["unique_id"],
                    "new_resource_type": row["resource_type"],
                    "new_package_name": row["package_name"],
                    "new_name": row["name"],
                    "new_checksum": row["checksum"],
                }
            )
        session.execute(update_attributes, parameters)

    # 2. Node versions: one row per (node, checksum); a reverted checksum bumps its row.
    for batch in batches(rows.versions):
        values = []
        for row in batch:
            values.append({**row, "first_seen_at": seen_at, "last_seen_at": seen_at})
        session.execute(
            adapter.upsert(
                version_table, values, keys=["project", "unique_id", "checksum"], update=[], merge=SEEN_MERGE
            )
        )

    # 3. Edges, then clear `removed_at` on the edges this manifest is the newest for.
    for batch in batches(rows.edges):
        values = []
        for row in batch:
            values.append({**row, "first_seen_at": seen_at, "last_seen_at": seen_at, "removed_at": None})
        session.execute(
            adapter.upsert(
                edge_table, values, keys=["project", "parent_id", "child_id"], update=[], merge=SEEN_MERGE
            )
        )
    session.execute(
        sa.update(edge_table)
        .where(edge_table.c.project == rows.project)
        .where(edge_table.c.last_seen_at == seen_at)
        .where(edge_table.c.removed_at.is_not(None))
        .values(removed_at=None)
    )

    # 4. Removals, against the project's newest manifest.
    newest = newest_sighting(session, rows.project)
    removed_nodes = session.execute(
        sa.update(node_table)
        .where(node_table.c.project == rows.project)
        .where(node_table.c.removed_at.is_(None))
        .where(node_table.c.last_seen_at < newest)
        .values(removed_at=newest)
    ).rowcount
    removed_edges = session.execute(
        sa.update(edge_table)
        .where(edge_table.c.project == rows.project)
        .where(edge_table.c.removed_at.is_(None))
        .where(edge_table.c.last_seen_at < newest)
        .values(removed_at=newest)
    ).rowcount

    return SyncCounts(
        nodes=len(rows.nodes),
        versions=len(rows.versions),
        edges=len(rows.edges),
        removed_nodes=removed_nodes,
        removed_edges=removed_edges,
    )


def newest_sighting(session: Session, project: str) -> datetime:
    """The time of the newest manifest loaded for the project."""
    node_table = m.Node.__table__
    newest = session.execute(
        sa.select(sa.func.max(node_table.c.last_seen_at)).where(node_table.c.project == project)
    ).scalar_one()
    return newest
