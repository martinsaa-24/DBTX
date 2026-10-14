"""Node results (plan 5.3): one row per node per invocation.

The caller owns the transaction; the invocation row must exist.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.store import models as m
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.enums import NODE_RESULT_STATUSES
from dbtx.store.repositories.nodes import batches
from dbtx.store.validation import check_known

logger = logging.getLogger("dbtx.store")

KEYS = ["invocation_id", "project", "unique_id"]
VALUE_COLUMNS = [
    "status",
    "checksum",
    "started_at",
    "completed_at",
    "execution_time",
    "thread_id",
    "message",
    "failures",
    "rows_affected",
    "adapter_response",
    "compiled_code",
    "relation_name",
]


def stored_node_ids(session: Session, project: str, unique_ids: list[str]) -> set[str]:
    node_table = m.Node.__table__
    found = set()
    for start in range(0, len(unique_ids), 500):
        chunk = unique_ids[start : start + 500]
        found.update(
            session.execute(
                sa.select(node_table.c.unique_id)
                .where(node_table.c.project == project)
                .where(node_table.c.unique_id.in_(chunk))
            ).scalars()
        )
    return found


def load(
    session: Session, adapter: StoreAdapter, project: str, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    """Upsert the rows whose node is stored. Returns (stored, skipped).

    A result without a stored node (an on-run hook, an analysis, `operation.*`) is
    skipped: its foreign key would fail.
    """
    known = stored_node_ids(session, project, sorted({row["unique_id"] for row in rows}))
    kept = []
    for row in rows:
        if row["unique_id"] not in known:
            continue
        check_known("node_result.status", row["status"], NODE_RESULT_STATUSES)
        kept.append(row)
    skipped = len(rows) - len(kept)
    if skipped > 0:
        logger.debug(f"node results: skipped {skipped} without a stored node in {project}")
    for batch in batches(kept):
        session.execute(adapter.upsert(m.NodeResult.__table__, batch, keys=KEYS, update=VALUE_COLUMNS))
    return len(kept), skipped
