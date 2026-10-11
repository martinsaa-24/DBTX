"""Reports (`meta.dbtx.report` exposures) and their renders (plan 5.4).

`sync` follows the node sync's rules (see `repositories/nodes.py`): sighting times
merge, the other columns follow the newest manifest, removals are marked against the
project's newest manifest. The render rows record one report's lifecycle in one
invocation; their times come from `dbtx.store.clock`. The caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.reports import Report
from dbtx.store import clock
from dbtx.store import models as m
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.enums import RenderStatus
from dbtx.store.repositories.nodes import SEEN_MERGE, newest_sighting

FINISHED_STATUSES = (RenderStatus.RENDERED.value, RenderStatus.SKIPPED.value, RenderStatus.ERROR.value)


def report_config(report: Report) -> dict[str, Any]:
    return report.config.model_dump(mode="json")


def config_hash(config: dict[str, Any]) -> str:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def report_row(project: str, report: Report) -> dict[str, Any]:
    config = report_config(report)
    return {
        "project": project,
        "exposure_id": report.exposure_id,
        "relation": report.relation,
        "formats": list(report.config.formats),
        "template": report.config.template_path or report.config.template,
        "config": config,
        "config_hash": config_hash(config),
    }


def sync(
    session: Session, adapter: StoreAdapter, project: str, reports: dict[str, Report], seen_at: datetime
) -> tuple[int, int]:
    """Store the manifest's reports. Returns (stored, newly removed)."""
    report_table = m.Report.__table__
    rows = []
    for exposure_id in sorted(reports):
        rows.append(report_row(project, reports[exposure_id]))

    if rows:
        values = []
        for row in rows:
            values.append({**row, "first_seen_at": seen_at, "last_seen_at": seen_at, "removed_at": None})
        session.execute(
            adapter.upsert(report_table, values, keys=["project", "exposure_id"], update=[], merge=SEEN_MERGE)
        )
        update_columns = (
            sa.update(report_table)
            .where(report_table.c.project == sa.bindparam("key_project"))
            .where(report_table.c.exposure_id == sa.bindparam("key_exposure_id"))
            .where(report_table.c.last_seen_at == seen_at)
            .values(
                relation=sa.bindparam("new_relation"),
                formats=sa.bindparam("new_formats", type_=report_table.c.formats.type),
                template=sa.bindparam("new_template"),
                config=sa.bindparam("new_config", type_=report_table.c.config.type),
                config_hash=sa.bindparam("new_config_hash"),
                removed_at=None,
            )
        )
        parameters = []
        for row in rows:
            parameters.append(
                {
                    "key_project": row["project"],
                    "key_exposure_id": row["exposure_id"],
                    "new_relation": row["relation"],
                    "new_formats": row["formats"],
                    "new_template": row["template"],
                    "new_config": row["config"],
                    "new_config_hash": row["config_hash"],
                }
            )
        session.execute(update_columns, parameters)

    newest = max(newest_sighting(session, project) or seen_at, seen_at)
    removed = session.execute(
        sa.update(report_table)
        .where(report_table.c.project == project)
        .where(report_table.c.removed_at.is_(None))
        .where(report_table.c.last_seen_at < newest)
        .values(removed_at=newest)
    ).rowcount
    return len(rows), removed


def insert_pending_renders(
    session: Session, adapter: StoreAdapter, invocation_id: str, project: str, reports: dict[str, Report]
) -> None:
    """A `pending` render row for each report in the invocation (again: no change)."""
    rows = []
    for exposure_id in sorted(reports):
        rows.append(
            {
                "invocation_id": invocation_id,
                "project": project,
                "exposure_id": exposure_id,
                "config_hash": config_hash(report_config(reports[exposure_id])),
                "status": RenderStatus.PENDING.value,
                "detail": None,
                "paths": None,
                "started_at": None,
                "completed_at": None,
            }
        )
    if rows:
        session.execute(
            adapter.upsert(
                m.ReportRender.__table__, rows, keys=["invocation_id", "project", "exposure_id"], update=[]
            )
        )


def render_key(invocation_id: str, project: str, exposure_id: str) -> list[sa.ColumnElement[bool]]:
    table = m.ReportRender.__table__
    return [
        table.c.invocation_id == invocation_id,
        table.c.project == project,
        table.c.exposure_id == exposure_id,
    ]


def mark_render_running(session: Session, invocation_id: str, project: str, exposure_id: str) -> None:
    session.execute(
        sa.update(m.ReportRender.__table__)
        .where(*render_key(invocation_id, project, exposure_id))
        .values(status=RenderStatus.RUNNING.value, started_at=clock.now())
    )


def mark_render_finished(
    session: Session,
    invocation_id: str,
    project: str,
    exposure_id: str,
    *,
    status: str,
    detail: Optional[str],
    paths: Optional[dict[str, str]],
) -> None:
    if status not in FINISHED_STATUSES:
        raise ValueError(f"{status!r} is not a finished render status; use one of {', '.join(FINISHED_STATUSES)}")
    session.execute(
        sa.update(m.ReportRender.__table__)
        .where(*render_key(invocation_id, project, exposure_id))
        .values(status=status, detail=detail, paths=paths, completed_at=clock.now())
    )


def render_paths(paths: list[Path]) -> dict[str, str]:
    """Rendered files → {format: path}; the format is the file's suffix."""
    result = {}
    for path in paths:
        result[path.suffix.lstrip(".")] = str(path)
    return result
