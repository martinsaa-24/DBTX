import csv
import re

from tests.integration.conftest import NO_SEEDS

ALL_REPORT_FILES = {
    "store_revenue_monthly.html",
    "store_revenue_monthly.csv",
    "top_customers.html",
    "jaffle_mix.csv",
    "jaffle_mix.html",
    "customer_leaderboard.html",
    "beverage_leaderboard.html",
    "store_scorecard.html",
    "store_scorecard.csv",
}


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


ALL_REPORT_SQL = {
    "store_revenue_monthly.sql",
    "top_customers.sql",
    "jaffle_mix.sql",
    "customer_leaderboard.sql",
    "beverage_leaderboard.sql",
    "store_scorecard.sql",
}


def test_full_build_renders_every_report(built_project):
    assert built_project.report_files() == ALL_REPORT_FILES

    monthly = read_csv(built_project.reports / "store_revenue_monthly.csv")
    assert len(monthly) == 138  # 5 stores x 24 months + Los Angeles from 2025-03
    assert list(monthly[0]) == ["store_name", "order_month", "order_count", "revenue", "tax_paid"]
    assert sum(int(r["order_count"]) for r in monthly) == 8_000

    mix = read_csv(built_project.reports / "jaffle_mix.csv")
    assert [r["product_name"] for r in mix][:1] == ["nutellaphone who dis?"]
    assert {r["product_type"] for r in mix} == {"jaffle"}

    html = (built_project.reports / "top_customers.html").read_text(encoding="utf-8")
    assert html.count("<tr>") == 26  # header + 25 rows


def test_each_report_uses_its_template(built_project):
    def html(name):
        return (built_project.reports / f"{name}.html").read_text(encoding="utf-8")

    # project `finance` template (extends integrated `table`): banner and exact totals
    assert "Finance · internal" in html("store_revenue_monthly")
    assert "<td>Total</td><td></td><td>8000</td><td>154212.00</td><td>9068.70</td>" in html(
        "store_revenue_monthly"
    )
    # integrated `table` template, no finance additions
    assert "Finance" not in html("top_customers").replace("Finance Analytics", "")
    # exact template_path
    assert html("jaffle_mix").count('class="item"') == 5
    assert "1. nutellaphone who dis?" in html("jaffle_mix")


def test_extended_templates_render_real_data(built_project):
    def html(name):
        return (built_project.reports / f"{name}.html").read_text(encoding="utf-8")

    brand = "Jaffle Shop · Analytics"
    for name in ("store_revenue_monthly", "customer_leaderboard", "beverage_leaderboard", "store_scorecard"):
        assert brand in html(name), name  # all extend jaffle_base
    assert brand not in html("top_customers")  # plain integrated table

    board = html("customer_leaderboard")
    assert board.count("<li>") == 10
    assert re.findall(r'class="name">([^<]+)', board)[:2] == ["Jennifer Miller", "Paul Flores"]
    assert re.findall(r'class="val">([^<]+)', board)[0] == "$10,192.19"

    drinks = html("beverage_leaderboard")
    assert drinks.count("<li>") == 5
    assert re.findall(r'class="name">([^<]+)', drinks)[:2] == ["adele-ade", "tangaroo"]
    assert re.findall(r'class="val">([^<]+)', drinks)[:2] == ["2,538", "1,898"]

    tiles = dict(re.findall(r'<div class="k">([^<]+)</div>\s*<div class="v">([^<]+)</div>', html("store_scorecard")))
    assert tiles == {
        "Stores": "6",
        "Orders": "8,000",
        "Revenue": "$154,212.00",
        "Best store revenue": "$28,351.00",
    }


def test_build_writes_compiled_and_run_sql_like_dbt(built_project):
    assert built_project.compiled_sql_files() == ALL_REPORT_SQL
    assert built_project.run_sql_files() == ALL_REPORT_SQL
    for name in ALL_REPORT_SQL:
        compiled = (built_project.compiled_sql / name).read_text(encoding="utf-8")
        assert compiled == (built_project.run_sql / name).read_text(encoding="utf-8")
    assert (built_project.run_sql / "top_customers.sql").read_text(encoding="utf-8") == (
        "select customer_name, order_count, lifetime_spend, last_ordered_at"
        ' from "jaffle_shop"."main"."customers"'
        " order by lifetime_spend desc, customer_name limit 25\n"
    )
    # Deliverables stay free of SQL.
    assert not any(name.endswith(".sql") for name in built_project.report_files())


def test_selecting_an_exposure_builds_its_upstream_and_renders_only_it(project, capsys):
    assert project.dbtx("build", "-s", "+exposure:top_customers", *NO_SEEDS) == 0
    assert project.report_files() == {"top_customers.html"}
    assert project.run_sql_files() == {"top_customers.sql"}
    assert "dbtx: 1 report(s)" in capsys.readouterr().out


def test_exposure_alone_renders_against_existing_tables(project):
    assert project.dbtx("build", "-s", "exposure:jaffle_mix") == 0
    assert project.report_files() == {"jaffle_mix.csv", "jaffle_mix.html"}


def test_models_without_their_exposure_render_nothing(project, capsys):
    assert project.dbtx("build", "-s", "customers") == 0
    assert project.report_files() == set()
    assert "dbtx:" not in capsys.readouterr().out


def test_no_reports_flag(project):
    assert project.dbtx("build", "--no-reports", "-s", "+exposure:top_customers", *NO_SEEDS) == 0
    assert project.report_files() == set()


def test_failing_upstream_test_blocks_only_its_reports(project, capsys):
    project.write(
        "tests/assert_no_customers.sql",
        "select * from {{ ref('customers') }}",
    )

    assert project.dbtx("build", *NO_SEEDS) == 1

    # Only the reports built on `customers` are held back.
    assert project.report_files() == ALL_REPORT_FILES - {
        "top_customers.html",
        "customer_leaderboard.html",
    }
    out = capsys.readouterr().out
    assert "SKIPPED   top_customers  upstream test fail: test.jaffle_shop.assert_no_customers" in out
    assert "SKIPPED   customer_leaderboard  upstream test fail" in out
    # Like a skipped dbt node, a skipped report is neither compiled nor run.
    skipped = {"top_customers.sql", "customer_leaderboard.sql"}
    assert project.compiled_sql_files() == ALL_REPORT_SQL - skipped
    assert project.run_sql_files() == ALL_REPORT_SQL - skipped


def test_failing_model_skips_downstream_reports(project, capsys):
    project.write("models/marts/product_mix.sql", "select * from missing_table")

    assert project.dbtx("build", *NO_SEEDS) == 1

    assert project.report_files() == ALL_REPORT_FILES - {
        "jaffle_mix.csv",
        "jaffle_mix.html",
        "beverage_leaderboard.html",
    }
    out = capsys.readouterr().out
    assert "SKIPPED   jaffle_mix  exposure skipped (upstream failure)" in out
    assert "SKIPPED   beverage_leaderboard  exposure skipped (upstream failure)" in out


def test_report_query_error_fails_the_run(project, capsys):
    exposures = project.root / "models/marts/_exposures.yml"
    exposures.write_text(
        exposures.read_text(encoding="utf-8").replace(
            "where: product_type = 'jaffle'", "where: no_such_column = 1"
        ),
        encoding="utf-8",
    )

    assert project.dbtx("build", "-s", "+exposure:jaffle_mix", *NO_SEEDS) == 1

    out = capsys.readouterr().out
    assert "ERROR     jaffle_mix" in out
    assert "no_such_column" in out
    # The failing query is left in target/run for debugging.
    assert "where no_such_column = 1" in (project.run_sql / "jaffle_mix.sql").read_text(encoding="utf-8")


def test_invalid_report_definition_is_a_usage_error(project, capsys):
    exposures = project.root / "models/marts/_exposures.yml"
    exposures.write_text(
        exposures.read_text(encoding="utf-8").replace("limit: 25", "limit: -1"), encoding="utf-8"
    )

    assert project.dbtx("build") == 2
    assert "top_customers" in capsys.readouterr().out


def _add_template_folder(project, folder):
    yml = project.root / "dbt_project.yml"
    yml.write_text(
        yml.read_text(encoding="utf-8").replace(
            "template_paths: [report_templates]", f"template_paths: [report_templates, {folder}]"
        ),
        encoding="utf-8",
    )


def test_template_name_in_two_project_folders_stops_before_dbt(project, capsys):
    project.write("more_templates/finance_template.html", "<p>other finance</p>")
    _add_template_folder(project, "more_templates")

    assert project.dbtx("build") == 2

    out = capsys.readouterr().out
    assert "template 'finance' is defined in two project template folders" in out
    assert project.report_files() == set()


def test_project_template_overrides_integrated_one(project):
    project.write("more_templates/table_template.html", "<p>custom table: {{ rows | length }}</p>")
    _add_template_folder(project, "more_templates")

    assert project.dbtx("build", "-s", "exposure:top_customers") == 0

    html = (project.reports / "top_customers.html").read_text(encoding="utf-8")
    assert html == "<p>custom table: 25</p>"


def test_unknown_template_is_a_usage_error(project, capsys):
    exposures = project.root / "models/marts/_exposures.yml"
    exposures.write_text(
        exposures.read_text(encoding="utf-8").replace("template: table ", "template: tabel "),
        encoding="utf-8",
    )

    assert project.dbtx("build") == 2
    assert "top_customers: unknown template 'tabel'" in capsys.readouterr().out


def test_templates_command_lists_resolved_templates(project, capsys):
    assert project.dbtx("templates") == 0
    header, *rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert header == ["NAME", "SOURCE", "URI"]
    assert [(name, source) for name, source, _ in rows] == [
        ("finance", "project"),
        ("jaffle_base", "project"),
        ("leaderboard", "project"),
        ("scorecard", "project"),
        ("table", "integrated"),
    ]
    assert rows[0][2].endswith("/report_templates/finance_template.html.j2")


def test_other_commands_pass_through_to_dbt(project):
    assert project.dbtx("ls", "-s", "exposure:*", "--quiet") == 0
    assert project.dbtx("not-a-command") == 2
