from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import dbt.adapters.factory
from dbt.cli.main import cli as dbt_cli

from dbtx.runner import (
    OutputPaths,
    argv_for,
    explicit_params,
    gate_tests,
    query_and_render,
    usage_error,
)
from tests.conftest import EXAMPLE_PROJECT


def test_explicit_params_keeps_only_user_set_options():
    params = explicit_params(
        "build", ["-s", "+exposure:top_customers", "--full-refresh", "--vars", "{a: 1}", "--threads", "8"]
    )
    assert params == {
        "select": ("+exposure:top_customers",),
        "full_refresh": True,
        "vars": {"a": 1},
        "threads": 8,
    }


def test_explicit_params_ignores_environment_variables(monkeypatch):
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    monkeypatch.setenv("DBT_TARGET", "prod")
    assert explicit_params("build", ["-s", "customers"]) == {"select": ("customers",)}


def test_argv_for_drops_options_the_command_does_not_accept():
    params = {"select": ("x",), "full_refresh": True, "vars": {"a": 1}, "target": "dev"}
    assert argv_for("list", params) == [
        "--select", "x", "--vars", "{a: 1}", "--target", "dev",
    ]
    # ls takes no --full-refresh, and parse takes neither that nor --select.
    assert argv_for("parse", params) == ["--vars", "{a: 1}", "--target", "dev"]


def test_argv_for_repeats_multi_options_and_negates_flags():
    params = {"select": ("a", "b"), "exclude": ("c",), "use_colors": False, "threads": 8}
    assert argv_for("build", params) == [
        "--select", "a", "--select", "b", "--exclude", "c", "--no-use-colors", "--threads", "8",
    ]


def test_argv_for_omits_a_flag_that_has_no_negation():
    assert argv_for("build", {"full_refresh": True}) == ["--full-refresh"]
    assert argv_for("build", {"full_refresh": False}) == []


def test_argv_for_round_trips_through_dbts_own_parser():
    """The tokens must parse back to the params they came from -- `--vars` especially,
    which has to stay a single argv token (flow style), not block YAML."""
    args = [
        "--select", "+beverage_leaderboard",
        "--project-dir", str(EXAMPLE_PROJECT),
        "--profiles-dir", str(EXAMPLE_PROJECT),
        "--vars", "{job_id: test123, n: 3, nested: {a: [1, 2]}}",
        "--threads", "2",
    ]
    params = explicit_params("build", args)
    ctx = dbt_cli.commands["build"].make_context(
        "build", argv_for("build", params), resilient_parsing=True
    )
    assert {k: ctx.params[k] for k in params} == params


def test_usage_error_names_the_option_the_user_got_wrong():
    """A resilient parse drops a rejected option, so the failure would otherwise be
    reported against that option's default instead of the value given."""
    problem = usage_error("build", ["--select", "x", "--profiles-dir", "does/not/exist"])
    assert problem is not None
    assert "--profiles-dir" in problem and "does/not/exist" in problem


def test_usage_error_is_none_for_a_usable_invocation():
    args = ["--select", "x", "--project-dir", str(EXAMPLE_PROJECT), "--profiles-dir", str(EXAMPLE_PROJECT)]
    assert usage_error("build", args) is None


def test_gate_tests_are_tests_on_direct_parents_within_the_run(manifest, reports):
    report = reports["top_customers"]
    everything = set(manifest.nodes)

    gates = gate_tests(manifest, report, everything)

    assert {g.split(".")[2] for g in gates} == {
        "unique_customers_customer_id",
        "not_null_customers_customer_id",
        "accepted_values_customers_customer_type__prospect__new__returning",
        "relationships_orders_customer_id__customer_id__ref_customers_",
        "warn_high_frequency_customers",
    }
    # Tests outside the run never gate a report.
    assert gate_tests(manifest, report, everything - gates) == set()


def test_gate_tests_cover_every_parent(manifest, reports):
    gates = gate_tests(manifest, reports["jaffle_mix"], set(manifest.nodes))
    names = {g.split(".")[2] for g in gates}
    assert {"unique_product_mix_product_name", "unique_orders_order_id"} <= names
    assert "assert_order_totals_reconcile" in names


def test_output_paths_mirror_dbt_target_layout():
    paths = OutputPaths.for_project({"project_dir": str(EXAMPLE_PROJECT)}, "jaffle_shop")
    target = EXAMPLE_PROJECT / "target"
    assert paths.reports == target / "reports"
    assert paths.compiled == target / "compiled" / "jaffle_shop" / "reports"
    assert paths.run == target / "run" / "jaffle_shop" / "reports"

    custom = OutputPaths.for_project({"project_dir": "p", "target_path": "out"}, "shop")
    assert custom.run == Path("p/out/run/shop/reports")


class FakeAdapter:
    """Records what exists on disk at each step of a report query."""

    def __init__(self, paths, name):
        self.compiled = paths.compiled / f"{name}.sql"
        self.run = paths.run / f"{name}.sql"
        self.connection_open = False
        self.seen = []

    @contextmanager
    def connection_named(self, _name):
        self.seen.append(("open", self.run.exists()))
        self.connection_open = True
        yield
        self.connection_open = False

    def execute(self, sql, fetch):
        assert self.connection_open
        self.seen.append(("execute", self.compiled.read_text(), self.run.read_text()))
        return None, SimpleNamespace(column_names=["customer_name"], rows=[("Ann",)])


def test_build_writes_compiled_then_run_sql_right_before_executing(reports, templates, tmp_path, monkeypatch):
    report = reports["top_customers"]
    paths = OutputPaths(reports=tmp_path / "r", compiled=tmp_path / "c", run=tmp_path / "x")
    adapter = FakeAdapter(paths, report.name)
    monkeypatch.setattr(dbt.adapters.factory, "get_adapter_by_type", lambda _: adapter)

    query_and_render("duckdb", report, paths, templates)

    sql = report.sql() + "\n"
    assert adapter.seen == [
        ("open", False),  # no run file until the connection is in hand
        ("execute", sql, sql),  # both files exist, identical to what is executed
    ]
