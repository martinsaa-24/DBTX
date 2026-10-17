"""The docs overlay's run history, read from the database (plan Phase 7).

The overlay loads one document, `dbtx_runtime.json`. In platform mode with the backend
enabled, `dbtx docs serve` answers that request from here instead of from the sidecar
file: the same JSON shape, built from `node_result`.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import sqlalchemy as sa

from dbtx.docs import exposure_reports, sidecar
from dbtx.docs.catalog import DocsCatalog, NodeResult
from dbtx.runs import COMMANDS_THAT_EXECUTE_NODES
from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store.startup import error_reason

# How often, at most, the cache asks the database whether anything changed.
PROBE_INTERVAL_SECONDS = 5.0


class HistoryError(Exception):
    """The docs site cannot be served from the database (no project name, ...)."""


class HistoryUnavailable(Exception):
    """The database failed and there is no earlier document to fall back on."""


def monotonic() -> float:
    """The cache's clock (seconds). Tests replace it."""
    return time.monotonic()


def project_of(docs_loc: Path) -> str:
    """The dbt project the docs site describes: its manifest's `metadata.project_name`."""
    manifest_file = Path(docs_loc) / "manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HistoryError(f"no manifest.json in {docs_loc}; run `dbtx docs generate` first") from None
    except (OSError, ValueError) as exc:
        raise HistoryError(f"cannot read {manifest_file}: {exc}") from None
    project = (manifest.get("metadata") or {}).get("project_name")
    if not project:
        raise HistoryError(f"{manifest_file} has no metadata.project_name")
    return str(project)


def iso(moment: Optional[datetime]) -> Optional[str]:
    """An aware UTC time as the sidecar writes times: ISO 8601 with a `Z`."""
    if moment is None:
        return None
    return moment.isoformat().replace("+00:00", "Z")


def build_document(store: Store, docs_loc: Path, project: str) -> dict:
    """The overlay's document for the docs site at `docs_loc`, from the database.

    The node set comes from the docs site's manifest; each node's runs are its newest
    results from invocations that execute nodes, newest first, up to the history limit.
    """
    docs_loc = Path(docs_loc)
    limit = sidecar.resolve_history_limit(docs_loc)
    catalog = DocsCatalog(history_limit=limit)
    catalog.parse_manifest(str(docs_loc / "manifest.json"))

    results = m.NodeResult.__table__
    invocations = m.Invocation.__table__
    versions = m.NodeVersion.__table__
    # Newest first by when the invocation started: a skipped node has no completion
    # time, and the backends sort NULLs differently.
    rank = (
        sa.func.row_number()
        .over(
            partition_by=results.c.unique_id,
            order_by=(invocations.c.start_time.desc(), invocations.c.invocation_id.desc()),
        )
        .label("rank")
    )
    ranked = (
        sa.select(
            results.c.unique_id,
            results.c.status,
            results.c.execution_time,
            results.c.checksum,
            results.c.completed_at,
            invocations.c.invocation_id,
            invocations.c.end_time,
            invocations.c.job_id,
            versions.c.first_seen_at,
            rank,
        )
        .join(invocations, invocations.c.invocation_id == results.c.invocation_id)
        .outerjoin(
            versions,
            sa.and_(
                versions.c.project == results.c.project,
                versions.c.unique_id == results.c.unique_id,
                versions.c.checksum == results.c.checksum,
            ),
        )
        .where(results.c.project == project)
        .where(invocations.c.command.in_(COMMANDS_THAT_EXECUTE_NODES))
        .subquery()
    )
    query = sa.select(ranked).where(ranked.c.rank <= limit).order_by(ranked.c.unique_id, ranked.c.rank)

    with store.session() as session:
        rows = session.execute(query).all()

    for row in rows:
        doc_node = catalog.nodes.get(row.unique_id)
        if doc_node is None:
            continue
        checksum = row.checksum
        if checksum == "":
            # dbt gives generic tests an empty checksum; the catalog (and so the docs
            # manifest it compares against) uses the node's name instead.
            checksum = catalog._node_checksum(row.unique_id, {})
        doc_node.run_history.append(
            NodeResult(
                node_name=row.unique_id,
                node_status=row.status,
                execution_time=row.execution_time,
                compile_time=None,
                run_time=iso(row.completed_at),
                checksum=checksum,
                run_invocation_id=row.invocation_id,
                run_generated_at=iso(row.end_time),
                job_id=row.job_id,
                updated_at=iso(row.first_seen_at),
            )
        )
    catalog.recompute_staleness()

    # Reports are found on disk, as the sidecar finds them.
    found = exposure_reports.discover(catalog.nodes, docs_loc)
    for unique_id, asset in found.items():
        catalog.nodes[unique_id].report = asset

    return sidecar.document(catalog)


def file_version(path: Path) -> Optional[int]:
    """The file's modification time (ns), or None when it does not exist."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def reports_version(docs_loc: Path) -> tuple[tuple[str, int], ...]:
    """Every file in the docs site's reports folder, with its modification time."""
    reports_dir = docs_loc / exposure_reports.REPORTS_SUBDIR
    if not reports_dir.is_dir():
        return ()
    entries = []
    for path in sorted(reports_dir.iterdir()):
        modified = file_version(path)
        if modified is not None:
            entries.append((path.name, modified))
    return tuple(entries)


def database_version(store: Store, project: str) -> tuple[int, Optional[datetime]]:
    """The project's invocation count and newest end time: either moves when a run
    starts, finishes or is loaded. A run's results are written with its end time."""
    invocations = m.Invocation.__table__
    query = sa.select(sa.func.count(), sa.func.max(invocations.c.end_time)).where(
        invocations.c.project == project
    )
    with store.session() as session:
        count, newest_end_time = session.execute(query).one()
    return count, newest_end_time


class HistoryCache:
    """The built document, rebuilt only when the database or the docs site changed.

    At most once per probe interval it compares a version of everything the document
    is built from (the project's invocations, the docs manifest, the sidecar file with
    its history limit, the reports folder) with the version it was built at. Used from
    the server's worker threads, so one lock covers the check and the rebuild.
    """

    def __init__(self, store: Store, docs_loc: Path) -> None:
        self.store = store
        self.docs_loc = Path(docs_loc)
        # Read once: `dbtx docs generate` for the same project keeps its name.
        self.project = project_of(self.docs_loc)
        self.lock = threading.Lock()
        self.kept: Optional[bytes] = None
        self.kept_version: Optional[tuple] = None
        self.checked_at: Optional[float] = None
        # True while the database is failing and the warning has been printed.
        self.warned = False

    def version(self) -> tuple:
        return (
            database_version(self.store, self.project),
            file_version(self.docs_loc / "manifest.json"),
            file_version(sidecar.sidecar_path(self.docs_loc)),
            reports_version(self.docs_loc),
        )

    def document(self) -> bytes:
        """The document as JSON bytes. HistoryUnavailable when the database fails before
        any document was built."""
        with self.lock:
            now = monotonic()
            if self.kept is not None and self.checked_at is not None:
                if now - self.checked_at < PROBE_INTERVAL_SECONDS:
                    return self.kept

            try:
                version = self.version()
                if self.kept is None or version != self.kept_version:
                    built = build_document(self.store, self.docs_loc, self.project)
                    self.kept = json.dumps(built).encode("utf-8")
                    self.kept_version = version
            except sa.exc.SQLAlchemyError as exc:
                reason = f"cannot read the run history from the database {self.store.masked_uri()}: {error_reason(exc)}"
                if self.kept is None:
                    raise HistoryUnavailable(reason) from None
                if not self.warned:
                    print(f"dbtx docs: warning: {reason}; serving the last one read", file=sys.stderr)
                    self.warned = True
                # Not asked again before the next interval, while it is failing.
                self.checked_at = now
                return self.kept

            self.checked_at = now
            self.warned = False
            return self.kept
