"""Store models: the `events` group of the spec."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dbtx.store.enums import InvocationCommand, JobStatus, RunResult, enum_values
from dbtx.store.models.base import Base
from dbtx.store.types import JSONType, UTCDateTime, in_check

if TYPE_CHECKING:
    from dbtx.store.models.dbt_nodes import NodeResult
    from dbtx.store.models.dbtx_actions import ReportRender


class Job(Base):
    """A request to run a project, queued until a worker claims it."""

    __tablename__ = "job"
    __table_args__ = (
        sa.PrimaryKeyConstraint("job_id"),
        sa.ForeignKeyConstraint(["project"], ["project.project"]),
        sa.Index(None, "status", "requested_at"),
        sa.CheckConstraint(in_check("status", enum_values(JobStatus), nullable=False), name="status"),
        sa.CheckConstraint(in_check("result", enum_values(RunResult), nullable=True), name="result"),
    )

    job_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    project: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    trigger: Mapped[str] = mapped_column(sa.String(), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(), nullable=False)
    result: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    # Read-only navigation (plan 3.4): never cascades writes; load with selectinload.
    invocations: Mapped[list[Invocation]] = relationship("Invocation", viewonly=True, lazy="raise")


class Invocation(Base):
    """One dbt command run by dbtx."""

    __tablename__ = "invocation"
    __table_args__ = (
        sa.PrimaryKeyConstraint("invocation_id"),
        sa.ForeignKeyConstraint(["job_id"], ["job.job_id"]),
        sa.ForeignKeyConstraint(["project"], ["project.project"]),
        sa.Index(None, "job_id"),
        sa.Index(None, "project", "start_time"),
        sa.CheckConstraint(
            in_check("command", enum_values(InvocationCommand), nullable=False), name="command"
        ),
        sa.CheckConstraint(in_check("result", enum_values(RunResult), nullable=True), name="result"),
    )

    invocation_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    job_id: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    command: Mapped[str] = mapped_column(sa.String(), nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    result: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    dbt_version: Mapped[str] = mapped_column(sa.String(), nullable=False)
    adapter_type: Mapped[str] = mapped_column(sa.String(), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(), nullable=False)
    selector: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    args: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    manifest_hash: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    run_by: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    host: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    # The run's folder under LOG_ROOT_LOCATION (spec v3); None for `dbtx db load`.
    run_folder: Mapped[str | None] = mapped_column(sa.String(), nullable=True)

    # Read-only navigation (plan 3.4): never cascades writes; load with selectinload.
    job: Mapped[Job | None] = relationship("Job", viewonly=True, lazy="raise")
    node_results: Mapped[list[NodeResult]] = relationship("NodeResult", viewonly=True, lazy="raise")
    report_renders: Mapped[list[ReportRender]] = relationship("ReportRender", viewonly=True, lazy="raise")
