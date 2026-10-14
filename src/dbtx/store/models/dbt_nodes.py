"""Store models: the `dbt_nodes` group of the spec."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dbtx.store.enums import NodeResourceType, enum_values
from dbtx.store.models.base import Base
from dbtx.store.types import JSONType, UTCDateTime, in_check


class Node(Base):
    """A node of the project as of the latest manifest (removal is soft: `removed_at`)."""

    __tablename__ = "node"
    __table_args__ = (
        sa.PrimaryKeyConstraint("project", "unique_id"),
        sa.ForeignKeyConstraint(["project"], ["project.project"]),
        sa.CheckConstraint(
            in_check("resource_type", enum_values(NodeResourceType), nullable=False),
            name="resource_type",
        ),
    )

    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    unique_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(), nullable=False)
    package_name: Mapped[str] = mapped_column(sa.String(), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(), nullable=False)
    checksum: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    # Read-only navigation (plan 3.4): never cascades writes; load with selectinload.
    versions: Mapped[list[NodeVersion]] = relationship("NodeVersion", viewonly=True, lazy="raise")
    results: Mapped[list[NodeResult]] = relationship("NodeResult", viewonly=True, lazy="raise")
    # node_edge has two foreign keys to node, so both joins are spelled out. Both sides
    # join on `project` too: another project's edges between the same unique_ids are
    # never followed. Edges marked removed (`removed_at`) are included.
    parents: Mapped[list[Node]] = relationship(
        "Node",
        secondary="node_edge",
        primaryjoin="and_(Node.project == NodeEdge.project, Node.unique_id == NodeEdge.child_id)",
        secondaryjoin="and_(Node.project == NodeEdge.project, Node.unique_id == NodeEdge.parent_id)",
        viewonly=True,
        lazy="raise",
    )
    children: Mapped[list[Node]] = relationship(
        "Node",
        secondary="node_edge",
        primaryjoin="and_(Node.project == NodeEdge.project, Node.unique_id == NodeEdge.parent_id)",
        secondaryjoin="and_(Node.project == NodeEdge.project, Node.unique_id == NodeEdge.child_id)",
        viewonly=True,
        lazy="raise",
    )


class NodeVersion(Base):
    """Every checksum a node has had in any manifest."""

    __tablename__ = "node_version"
    __table_args__ = (
        sa.PrimaryKeyConstraint("project", "unique_id", "checksum"),
        sa.ForeignKeyConstraint(["project", "unique_id"], ["node.project", "node.unique_id"]),
    )

    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    unique_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    checksum: Mapped[str] = mapped_column(sa.String(), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class NodeEdge(Base):
    """A direct dependency between two nodes of one project (manifest `parent_map`)."""

    __tablename__ = "node_edge"
    __table_args__ = (
        sa.PrimaryKeyConstraint("project", "parent_id", "child_id"),
        sa.ForeignKeyConstraint(["project", "parent_id"], ["node.project", "node.unique_id"]),
        sa.ForeignKeyConstraint(["project", "child_id"], ["node.project", "node.unique_id"]),
        sa.Index(None, "project", "child_id"),
    )

    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    parent_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    child_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class NodeResult(Base):
    """The result of one node in one invocation."""

    __tablename__ = "node_result"
    __table_args__ = (
        sa.PrimaryKeyConstraint("invocation_id", "project", "unique_id"),
        sa.ForeignKeyConstraint(["invocation_id"], ["invocation.invocation_id"]),
        sa.ForeignKeyConstraint(["project", "unique_id"], ["node.project", "node.unique_id"]),
        sa.Index(None, "project", "unique_id", "completed_at"),
    )

    invocation_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    project: Mapped[str] = mapped_column(sa.String(), nullable=False)
    unique_id: Mapped[str] = mapped_column(sa.String(), nullable=False)
    # dbt's raw status: app-enforced (enums.NODE_RESULT_STATUSES), so no CHECK.
    status: Mapped[str] = mapped_column(sa.String(), nullable=False)
    checksum: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    execution_time: Mapped[float] = mapped_column(sa.Double(), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    message: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    failures: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    rows_affected: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    adapter_response: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    # The compiled SQL dbt ran and the relation it built or tested (spec v3).
    compiled_code: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
    relation_name: Mapped[str | None] = mapped_column(sa.String(), nullable=True)
