"""Guard G2: the `project` registry binds each dbt project name to one folder (plan 6.5.4).

The first run of a project name claims it for its folder. Later runs from the same
folder bump `last_seen_at`; a run from any other folder is refused, on any host. A
claim moves only through `rebind_project` (`dbtx db projects rebind`).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.store import clock
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.errors import StoreError
from dbtx.store.models import Project


@dataclass(frozen=True)
class Claim:
    project: str
    folder: str
    claimed_at: datetime
    claimed_by_host: Optional[str]
    last_seen_at: datetime


@dataclass(frozen=True)
class ClaimResult:
    claim: Claim
    # True when this call made the claim (the name was unclaimed).
    is_new: bool
    warnings: tuple[str, ...]


def current_host() -> Optional[str]:
    """This machine's host name, recorded with a new claim (tests replace this)."""
    return socket.gethostname()


def _claim_of(row: Project) -> Claim:
    return Claim(
        project=row.project,
        folder=row.folder,
        claimed_at=row.claimed_at,
        claimed_by_host=row.claimed_by_host,
        last_seen_at=row.last_seen_at,
    )


def _load(session: Session, project: str) -> Optional[Project]:
    """The stored row, read fresh from the database (never a stale session copy)."""
    statement = (
        sa.select(Project)
        .where(Project.project == project)
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).first()


def find_claim(session: Session, project: str) -> Optional[Claim]:
    """The claim on a project name, or None."""
    row = _load(session, project)
    if row is None:
        return None
    return _claim_of(row)


def list_claims(session: Session) -> list[Claim]:
    """Every claim, by project name."""
    rows = session.scalars(sa.select(Project).order_by(Project.project)).all()
    claims = []
    for row in rows:
        claims.append(_claim_of(row))
    return claims


def claim_project(session: Session, adapter: StoreAdapter, project: str, folder: str) -> ClaimResult:
    """Claim `project` for `folder`, or confirm the claim. StoreError if another folder
    owns the name.

    Race-safe through the primary key: the insert does nothing when the name is
    already claimed (also by a concurrent run that committed first), and the claim is
    then read back and compared.
    """
    now = clock.now()
    insert = adapter.insert(Project.__table__).values(
        project=project,
        folder=folder,
        claimed_at=now,
        claimed_by_host=current_host(),
        last_seen_at=now,
    )
    inserted = session.execute(
        insert.on_conflict_do_nothing(index_elements=["project"]).returning(Project.project)
    ).first()
    is_new = inserted is not None

    row = _load(session, project)
    assert row is not None

    if row.folder != folder:
        claimed_on = row.claimed_at.date().isoformat()
        if row.claimed_by_host:
            claimed = f"claimed {claimed_on} by host {row.claimed_by_host}"
        else:
            claimed = f"claimed {claimed_on}"
        raise StoreError(
            f"project name {project!r} is owned by folder {row.folder!r} in this database "
            f"({claimed}). Folder {folder!r} cannot use it. If the project moved, run "
            f"`dbtx db projects rebind {project} --folder {folder}`"
        )

    warnings = []
    if is_new:
        # The folder's dbt project was renamed: its old claims stay (with their history).
        statement = (
            sa.select(Project.project)
            .where(Project.folder == folder)
            .where(Project.project != project)
            .order_by(Project.project)
        )
        for old_name in session.scalars(statement).all():
            warnings.append(f"folder {folder!r} previously recorded project {old_name!r}")
    else:
        row.last_seen_at = now
        session.flush()

    return ClaimResult(claim=_claim_of(row), is_new=is_new, warnings=tuple(warnings))


def rebind_project(session: Session, project: str, folder: str) -> tuple[Claim, Claim]:
    """Move the claim on `project` to `folder`. Returns (before, after).

    The new owner gets a new `claimed_at` and host; `last_seen_at` is kept. Moving to
    the folder that already owns the name changes nothing.
    """
    row = _load(session, project)
    if row is None:
        raise StoreError(f"project name {project!r} has no claim in this database")
    before = _claim_of(row)
    if row.folder == folder:
        return before, before
    row.folder = folder
    row.claimed_at = clock.now()
    row.claimed_by_host = current_host()
    session.flush()
    return before, _claim_of(row)
