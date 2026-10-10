"""Models on a real database (`dbtx.store.models`), on every backend.

* `Base.metadata.create_all()` creates every spec table, and what the database reports
  back (columns, keys, indexes, CHECK names) matches the spec.
* Navigation is read-only: each relationship in the plan (section 3.4) exists, is
  `viewonly` (the ORM never cascades writes) and `lazy="raise"` (an unloaded
  relationship fails loudly instead of running a hidden query).
* Each relationship, loaded with `selectinload`, returns exactly the related rows, and
  never rows of another project that happen to share a `unique_id`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from dbtx.store import models as m

from .conftest import load_spec, reflection_schema, spec_tables

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

PROJECT = "jaffle_shop"
OTHER_PROJECT = "other_shop"
CUSTOMERS = "model.jaffle_shop.customers"
ORDERS = "model.jaffle_shop.orders"
ORDERS_TEST = "test.jaffle_shop.not_null_orders_order_id"
ORDERS_REPORT = "exposure.jaffle_shop.orders_report"

# (class name, relationship name, target class name, is a collection)
RELATIONSHIPS = [
    ("Job", "invocations", "Invocation", True),
    ("Invocation", "job", "Job", False),
    ("Invocation", "node_results", "NodeResult", True),
    ("Invocation", "report_renders", "ReportRender", True),
    ("Node", "versions", "NodeVersion", True),
    ("Node", "results", "NodeResult", True),
    ("Node", "parents", "Node", True),
    ("Node", "children", "Node", True),
    ("Report", "node", "Node", False),
    ("Report", "renders", "ReportRender", True),
]


def relationship(class_name: str, name: str):
    """The mapped relationship attribute, or a failed assertion when it does not exist."""
    mapper = sa.inspect(getattr(m, class_name))
    assert name in mapper.relationships, f"{class_name}.{name} is not a relationship"
    return getattr(getattr(m, class_name), name)


@pytest.fixture
def tables(engine):
    """The engine, with every store table created and empty."""
    m.Base.metadata.create_all(engine)
    return engine


# --------------------------------------------------------------------------------------
# create_all
# --------------------------------------------------------------------------------------


def test_create_all_creates_every_spec_table(tables):
    inspector = sa.inspect(tables)

    assert sorted(inspector.get_table_names(schema=reflection_schema(tables))) == sorted(
        spec_tables(load_spec())
    )


def test_created_tables_match_the_spec(tables):
    inspector = sa.inspect(tables)
    schema = reflection_schema(tables)

    for table_name, (_, table_spec) in spec_tables(load_spec()).items():
        columns = inspector.get_columns(table_name, schema=schema)
        assert [column["name"] for column in columns] == [
            item["field_name"] for item in table_spec["attributes"]
        ], table_name
        expected_nullable = {
            item["field_name"]: item.get("nullable", False) for item in table_spec["attributes"]
        }
        assert {column["name"]: column["nullable"] for column in columns} == expected_nullable, table_name

        primary_key = inspector.get_pk_constraint(table_name, schema=schema)
        assert primary_key["constrained_columns"] == table_spec["primary_key"], table_name
        assert primary_key["name"] in (f"pk_{table_name}", None), table_name  # SQLite may omit it

        expected_foreign_keys = set()
        for foreign_key in table_spec.get("foreign_keys", []):
            referred_table, referred_columns = foreign_key["references"].rstrip(")").split("(")
            expected_foreign_keys.add(
                (
                    tuple(foreign_key["columns"]),
                    referred_table,
                    tuple(column.strip() for column in referred_columns.split(",")),
                )
            )
        actual_foreign_keys = set()
        for foreign_key in inspector.get_foreign_keys(table_name, schema=schema):
            actual_foreign_keys.add(
                (
                    tuple(foreign_key["constrained_columns"]),
                    foreign_key["referred_table"],
                    tuple(foreign_key["referred_columns"]),
                )
            )
        assert actual_foreign_keys == expected_foreign_keys, table_name

        expected_indexes = {tuple(index["columns"]) for index in table_spec.get("indexes", [])}
        actual_indexes = {
            tuple(index["column_names"]) for index in inspector.get_indexes(table_name, schema=schema)
        }
        assert actual_indexes == expected_indexes, table_name

        expected_checks = {
            f"ck_{table_name}_{item['field_name']}"
            for item in table_spec["attributes"]
            if item.get("enforced_by") == "db"
        }
        actual_checks = {
            check["name"] for check in inspector.get_check_constraints(table_name, schema=schema)
        }
        assert actual_checks == expected_checks, table_name


# --------------------------------------------------------------------------------------
# Relationship configuration
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "class_name, name, target, is_collection",
    RELATIONSHIPS,
    ids=[f"{class_name}.{name}" for class_name, name, _, _ in RELATIONSHIPS],
)
def test_relationship_is_read_only_and_raises_when_not_loaded(class_name, name, target, is_collection):
    relationship(class_name, name)
    configured = sa.inspect(getattr(m, class_name)).relationships[name]

    assert configured.mapper.class_ is getattr(m, target)
    assert configured.viewonly is True
    assert configured.lazy == "raise"
    assert configured.uselist is is_collection


def test_models_have_no_other_relationships():
    expected = sorted(f"{class_name}.{name}" for class_name, name, _, _ in RELATIONSHIPS)

    actual = []
    for mapper in m.Base.registry.mappers:
        for name in mapper.relationships.keys():
            actual.append(f"{mapper.class_.__name__}.{name}")

    assert sorted(actual) == expected


# --------------------------------------------------------------------------------------
# Loading relationships
# --------------------------------------------------------------------------------------


def node(project, unique_id, resource_type, checksum):
    return m.Node(
        project=project,
        unique_id=unique_id,
        resource_type=resource_type,
        package_name=project,
        name=unique_id.split(".")[-1],
        checksum=checksum,
        first_seen_at=T0,
        last_seen_at=T0,
    )


def edge(project, parent_id, child_id):
    return m.NodeEdge(
        project=project, parent_id=parent_id, child_id=child_id, first_seen_at=T0, last_seen_at=T0
    )


def node_result(invocation_id, unique_id):
    return m.NodeResult(
        invocation_id=invocation_id,
        project=PROJECT,
        unique_id=unique_id,
        status="success",
        execution_time=1.5,
    )


def invocation(invocation_id, job_id, command):
    return m.Invocation(
        invocation_id=invocation_id,
        job_id=job_id,
        project=PROJECT,
        command=command,
        start_time=T0,
        dbt_version="1.12.5",
        adapter_type="duckdb",
        target="dev",
        args={"which": command},
    )


@pytest.fixture
def history(tables):
    """Two projects that share `unique_id`s, with their edges reversed in the other one.

    jaffle_shop:  customers -> orders -> {not_null test, orders_report exposure}
    other_shop:   orders -> customers
    """
    with Session(tables) as session:
        session.add_all(
            [
                m.Project(project=PROJECT, folder="jaffle-shop", claimed_at=T0, last_seen_at=T0),
                m.Project(project=OTHER_PROJECT, folder="other-shop", claimed_at=T0, last_seen_at=T0),
            ]
        )
        session.flush()
        session.add(
            m.Job(job_id="job-1", project=PROJECT, trigger="manual", requested_at=T0, status="finished")
        )
        session.flush()
        session.add_all([invocation("inv-1", "job-1", "build"), invocation("inv-2", None, "run")])
        session.flush()
        session.add_all(
            [
                node(PROJECT, CUSTOMERS, "model", "c2"),
                node(PROJECT, ORDERS, "model", "o1"),
                node(PROJECT, ORDERS_TEST, "test", "t1"),
                node(PROJECT, ORDERS_REPORT, "exposure", None),
                node(OTHER_PROJECT, CUSTOMERS, "model", "x1"),
                node(OTHER_PROJECT, ORDERS, "model", "x2"),
            ]
        )
        session.flush()
        session.add_all(
            [
                edge(PROJECT, CUSTOMERS, ORDERS),
                edge(PROJECT, ORDERS, ORDERS_TEST),
                edge(PROJECT, ORDERS, ORDERS_REPORT),
                edge(OTHER_PROJECT, ORDERS, CUSTOMERS),
                m.NodeVersion(project=PROJECT, unique_id=CUSTOMERS, checksum="c1", first_seen_at=T0, last_seen_at=T0),
                m.NodeVersion(project=PROJECT, unique_id=CUSTOMERS, checksum="c2", first_seen_at=T0, last_seen_at=T0),
                m.NodeVersion(project=OTHER_PROJECT, unique_id=CUSTOMERS, checksum="x1", first_seen_at=T0, last_seen_at=T0),
                node_result("inv-1", CUSTOMERS),
                node_result("inv-1", ORDERS),
                node_result("inv-2", CUSTOMERS),
                m.Report(
                    project=PROJECT,
                    exposure_id=ORDERS_REPORT,
                    relation='"main"."orders"',
                    formats=["html"],
                    config={"formats": ["html"]},
                    config_hash="h1",
                    first_seen_at=T0,
                    last_seen_at=T0,
                ),
            ]
        )
        session.flush()
        session.add(
            m.ReportRender(
                invocation_id="inv-1",
                project=PROJECT,
                exposure_id=ORDERS_REPORT,
                config_hash="h1",
                status="rendered",
                paths=["target/reports/orders_report.html"],
            )
        )
        session.commit()
    return tables


def load_node(session, project, unique_id, *options):
    statement = sa.select(m.Node).where(m.Node.project == project, m.Node.unique_id == unique_id)
    return session.execute(statement.options(*options)).scalar_one()


def test_job_invocations(history):
    with Session(history) as session:
        job = session.execute(
            sa.select(m.Job).options(selectinload(relationship("Job", "invocations")))
        ).scalar_one()

        assert [item.invocation_id for item in job.invocations] == ["inv-1"]


def test_invocation_job(history):
    with Session(history) as session:
        invocations = session.execute(
            sa.select(m.Invocation)
            .options(selectinload(relationship("Invocation", "job")))
            .order_by(m.Invocation.invocation_id)
        ).scalars().all()

        assert invocations[0].job.job_id == "job-1"
        assert invocations[1].job is None


def test_invocation_node_results_and_report_renders(history):
    with Session(history) as session:
        first = session.execute(
            sa.select(m.Invocation)
            .where(m.Invocation.invocation_id == "inv-1")
            .options(
                selectinload(relationship("Invocation", "node_results")),
                selectinload(relationship("Invocation", "report_renders")),
            )
        ).scalar_one()

        assert sorted(item.unique_id for item in first.node_results) == [CUSTOMERS, ORDERS]
        assert [item.exposure_id for item in first.report_renders] == [ORDERS_REPORT]


def test_node_versions_and_results_stay_within_the_project(history):
    with Session(history) as session:
        customers = load_node(
            session,
            PROJECT,
            CUSTOMERS,
            selectinload(relationship("Node", "versions")),
            selectinload(relationship("Node", "results")),
        )

        assert sorted(item.checksum for item in customers.versions) == ["c1", "c2"]
        assert sorted(item.invocation_id for item in customers.results) == ["inv-1", "inv-2"]


def test_node_parents_and_children(history):
    with Session(history) as session:
        orders = load_node(
            session,
            PROJECT,
            ORDERS,
            selectinload(relationship("Node", "parents")),
            selectinload(relationship("Node", "children")),
        )

        assert [(item.project, item.unique_id) for item in orders.parents] == [(PROJECT, CUSTOMERS)]
        assert sorted((item.project, item.unique_id) for item in orders.children) == [
            (PROJECT, ORDERS_REPORT),
            (PROJECT, ORDERS_TEST),
        ]


def test_node_edges_of_another_project_are_never_followed(history):
    # other_shop has orders -> customers, the reverse of jaffle_shop.
    with Session(history) as session:
        customers = load_node(
            session,
            PROJECT,
            CUSTOMERS,
            selectinload(relationship("Node", "parents")),
            selectinload(relationship("Node", "children")),
        )
        other_customers = load_node(
            session,
            OTHER_PROJECT,
            CUSTOMERS,
            selectinload(relationship("Node", "parents")),
            selectinload(relationship("Node", "children")),
        )

        assert customers.parents == []
        assert [(item.project, item.unique_id) for item in customers.children] == [(PROJECT, ORDERS)]
        assert [(item.project, item.unique_id) for item in other_customers.parents] == [
            (OTHER_PROJECT, ORDERS)
        ]
        assert other_customers.children == []


def test_report_node_and_renders(history):
    with Session(history) as session:
        report = session.execute(
            sa.select(m.Report).options(
                selectinload(relationship("Report", "node")),
                selectinload(relationship("Report", "renders")),
            )
        ).scalar_one()

        assert (report.node.project, report.node.unique_id) == (PROJECT, ORDERS_REPORT)
        assert report.node.resource_type == "exposure"
        assert [item.invocation_id for item in report.renders] == ["inv-1"]


@pytest.mark.parametrize(
    "class_name, name",
    [(class_name, name) for class_name, name, _, _ in RELATIONSHIPS],
    ids=[f"{class_name}.{name}" for class_name, name, _, _ in RELATIONSHIPS],
)
def test_unloaded_relationship_raises_instead_of_querying(history, class_name, name):
    relationship(class_name, name)
    with Session(history) as session:
        instance = session.execute(sa.select(getattr(m, class_name)).limit(1)).scalar_one()

        with pytest.raises(sa.exc.InvalidRequestError, match="lazy='raise'"):
            getattr(instance, name)
