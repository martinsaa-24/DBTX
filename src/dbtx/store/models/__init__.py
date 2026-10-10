"""Store models, one class per spec entity (plan section 3.4).

Import through the module, never with a star import: `dbtx.docs.catalog` already has a
`NodeResult`.

    from dbtx.store import models as m
    m.NodeResult

Importing this package imports every model module, so `Base.metadata` is complete.
"""

from dbtx.store.models.base import Base
from dbtx.store.models.dbt_nodes import Node, NodeEdge, NodeResult, NodeVersion
from dbtx.store.models.dbtx_actions import Report, ReportRender
from dbtx.store.models.events import Invocation, Job
from dbtx.store.models.platform import Project

__all__ = [
    "Base",
    "Invocation",
    "Job",
    "Node",
    "NodeEdge",
    "NodeResult",
    "NodeVersion",
    "Project",
    "Report",
    "ReportRender",
]
