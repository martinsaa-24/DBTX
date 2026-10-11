"""Store models: the `dbtx_actions` group of the spec."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dbtx.store.enums import RenderStatus, enum_values
from dbtx.store.models.base import Base
from dbtx.store.types import JSONType, UTCDateTime, in_check

if TYPE_CHECKING:
    from dbtx.store.models.dbt_nodes import Node


class Report(Base):
    """The current `meta.dbtx.report` configuration of an exposure."""

    __tablename__ = "report"
    __table_args__ = (
        sa.PrimaryKeyConstraint("project", "exposure_id"),
        sa.ForeignKeyConstraint(["project", "exposure_id"], ["node.project", "node.unique_id"]),
    )

    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    exposure_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    relation: Mapped[str] = mapped_column(sa.String(), nullable=False)
    formats: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    template: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    config_hash: Mapped[str] = mapped_column(sa.String(), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    # Read-only navigation (plan 3.4): never cascades writes; load with selectinload.
    node: Mapped[Node] = relationship("Node", viewonly=True, lazy="raise")
    renders: Mapped[list[ReportRender]] = relationship("ReportRender", viewonly=True, lazy="raise")


class ReportRender(Base):
    """One report's render in one invocation."""

    __tablename__ = "report_render"
    __table_args__ = (
        sa.PrimaryKeyConstraint("invocation_id", "project", "exposure_id"),
        sa.ForeignKeyConstraint(["invocation_id"], ["invocation.invocation_id"]),
        sa.ForeignKeyConstraint(
            ["project", "exposure_id"], ["report.project", "report.exposure_id"]
        ),
        sa.Index(None, "project", "exposure_id", "completed_at"),
        sa.CheckConstraint(
            in_check("status", enum_values(RenderStatus), nullable=False), name="status"
        ),
    )

    invocation_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    exposure_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    config_hash: Mapped[str] = mapped_column(sa.String(), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(), nullable=False)
    detail: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    # {format: path}, one per rendered format (plan 5.4).
    paths: Mapped[dict[str, str] | None] = mapped_column(JSONType, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
