from tests.integration.test_build import ALL_REPORT_SQL


def test_compile_writes_report_sql_to_compiled_only(project, capsys):
    assert project.dbtx("compile") == 0

    assert project.compiled_sql_files() == ALL_REPORT_SQL
    assert project.run_sql_files() == set()  # like dbt: compile never writes target/run
    assert project.report_files() == set()  # and never renders
    sql = (project.compiled_sql / "jaffle_mix.sql").read_text(encoding="utf-8")
    assert sql == (
        'select * from "jaffle_shop"."main"."product_mix"'
        " where product_type = 'jaffle' order by units_sold desc\n"
    )
    assert "dbtx: 6 report definition(s) valid, 6 compiled" in capsys.readouterr().out


def test_compile_follows_selection_but_validates_everything(project, capsys):
    assert project.dbtx("compile", "-s", "+exposure:store_scorecard") == 0
    assert project.compiled_sql_files() == {"store_scorecard.sql"}

    project.clear_reports()
    assert project.dbtx("compile", "-s", "customers") == 0
    assert project.compiled_sql_files() == set()
    assert "6 report definition(s) valid, 0 compiled" in capsys.readouterr().out


def test_compile_fails_on_invalid_definitions_before_dbt_runs(project, capsys):
    exposures = project.root / "models/marts/_exposures.yml"
    exposures.write_text(
        exposures.read_text(encoding="utf-8").replace("template: table ", "template: tabel "),
        encoding="utf-8",
    )

    assert project.dbtx("compile") == 2
    assert "top_customers: unknown template 'tabel'" in capsys.readouterr().out
    assert project.compiled_sql_files() == set()


def test_compile_catches_broken_templates(project, capsys):
    project.write("report_templates/scorecard_template.html.j2", "{% block content %}{% if %}")

    assert project.dbtx("compile", "-s", "customers") == 2  # validation ignores selection
    out = capsys.readouterr().out
    assert "scorecard_template.html.j2, line 1" in out


def test_compile_passthrough_modes(project, capsys):
    assert project.dbtx("compile", "--no-reports") == 0
    assert project.dbtx("compile", "--inline", "select 1 as x") == 0
    assert project.compiled_sql_files() == set()
    assert "dbtx:" not in capsys.readouterr().out
