"""Store models: the `platform` group of the spec."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from dbtx.store.models.base import Base
from dbtx.store.types import UTCDateTime


class Project(Base):
    """One row per dbt project name in this database: the folder that owns the name."""

    __tablename__ = "project"
    __table_args__ = (
        sa.PrimaryKeyConstraint("project"),
        sa.Index(None, "folder"),
    )

    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    folder: Mapped[str] = mapped_column(sa.String(), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    claimed_by_host: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
