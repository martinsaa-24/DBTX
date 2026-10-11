import csv
from pathlib import Path

import pytest
from dbt.contracts.graph.manifest import Manifest

from dbtx.reports import Report, ReportDefinitionError, discover_reports, render
from dbtx.templates import TemplateEngine, TemplateSource
from tests.conftest import EXAMPLE_PROJECT


def test_discovers_only_exposures_with_dbtx_meta(reports: dict[str, Report]) -> None:
    assert set(reports) == {
        "store_revenue_monthly",
        "top_customers",
        "jaffle_mix",
        "customer_leaderboard",
        "beverage_leaderboard",
        "store_scorecard",
    }


def test_reads_both_config_meta_and_top_level_meta(reports: dict[str, Report]) -> None:
    # store_revenue_monthly uses `config.meta`, top_customers uses top-level `meta`.
    assert reports["store_revenue_monthly"].config.formats == ["html", "csv"]
    assert reports["top_customers"].config.limit == 25


def test_single_parent_is_the_report_source(reports: dict[str, Report]) -> None:
    report = reports["store_revenue_monthly"]
    assert report.relation == '"jaffle_shop"."main"."store_revenue_monthly"'
    assert report.title == "Monthly Revenue by Store"
    assert report.owner == "Finance Analytics"


def test_explicit_model_picks_among_several_parents(reports: dict[str, Report]) -> None:
    report = reports["jaffle_mix"]
    assert report.relation == '"jaffle_shop"."main"."product_mix"'
    assert report.parents == {"model.jaffle_shop.product_mix", "model.jaffle_shop.orders"}


def test_sql_applies_columns_filter_order_and_limit(reports: dict[str, Report]) -> None:
    assert reports["top_customers"].sql() == (
        "select customer_name, order_count, lifetime_spend, last_ordered_at"
        ' from "jaffle_shop"."main"."customers"'
        " order by lifetime_spend desc, customer_name limit 25"
    )
    assert reports["jaffle_mix"].sql() == (
        'select * from "jaffle_shop"."main"."product_mix"'
        " where product_type = 'jaffle' order by units_sold desc"
    )


def _report_meta(manifest: Manifest, name: str) -> dict:
    return manifest.exposures[f"exposure.jaffle_shop.{name}"].config.meta["dbtx"]["report"]


def discover(manifest: Manifest, templates: TemplateEngine) -> dict[str, Report]:
    return {r.name: r for r in discover_reports(manifest, templates, EXAMPLE_PROJECT).values()}


def test_ambiguous_source_requires_model(manifest: Manifest, templates: TemplateEngine) -> None:
    del _report_meta(manifest, "jaffle_mix")["model"]
    with pytest.raises(ReportDefinitionError, match="jaffle_mix: exposure depends on 2 models"):
        discover(manifest, templates)


def test_model_must_be_a_dependency(manifest: Manifest, templates: TemplateEngine) -> None:
    _report_meta(manifest, "jaffle_mix")["model"] = "customers"
    with pytest.raises(ReportDefinitionError, match="'customers' is not in the exposure's depends_on"):
        discover(manifest, templates)


def test_unknown_keys_and_bad_values_are_rejected(
    manifest: Manifest,
    templates: TemplateEngine,
) -> None:
    meta = _report_meta(manifest, "store_revenue_monthly")
    meta["colums"] = ["typo"]
    meta["formats"] = ["pdf"]
    with pytest.raises(ReportDefinitionError) as exc:
        discover(manifest, templates)
    assert "colums" in str(exc.value)
    assert "formats" in str(exc.value)


def test_templates_resolve_by_name_or_exact_path(reports: dict[str, Report]) -> None:
    finance = reports["store_revenue_monthly"].template
    assert (finance.name, finance.source) == ("finance", TemplateSource.PROJECT)
    table = reports["top_customers"].template
    assert (table.name, table.source) == ("table", TemplateSource.INTEGRATED)
    exact = reports["jaffle_mix"].template
    assert exact.source is TemplateSource.EXPOSURE
    assert exact.path == (EXAMPLE_PROJECT / "exposure_templates" / "jaffle_mix.html").resolve()


def test_html_output_requires_a_template(manifest: Manifest, templates: TemplateEngine) -> None:
    del _report_meta(manifest, "store_revenue_monthly")["template"]
    with pytest.raises(ReportDefinitionError, match="store_revenue_monthly: .*there is no default template"):
        discover(manifest, templates)


def test_csv_only_reports_need_no_template(manifest: Manifest, templates: TemplateEngine) -> None:
    meta = _report_meta(manifest, "store_revenue_monthly")
    meta["formats"] = ["csv"]
    del meta["template"]
    assert discover(manifest, templates)["store_revenue_monthly"].template is None


def test_unknown_template_name_is_a_definition_error(
    manifest: Manifest,
    templates: TemplateEngine,
) -> None:
    _report_meta(manifest, "store_revenue_monthly")["template"] = "marketing"
    with pytest.raises(ReportDefinitionError, match="store_revenue_monthly: unknown template 'marketing'"):
        discover(manifest, templates)


def test_template_path_takes_precedence_over_template(
    manifest: Manifest,
    templates: TemplateEngine,
) -> None:
    meta = _report_meta(manifest, "jaffle_mix")
    meta["template"] = "finance"
    assert discover(manifest, templates)["jaffle_mix"].template.source is TemplateSource.EXPOSURE


def test_missing_template_path_is_a_definition_error(
    manifest: Manifest,
    templates: TemplateEngine,
) -> None:
    _report_meta(manifest, "jaffle_mix")["template_path"] = "exposure_templates/nope.html"
    with pytest.raises(ReportDefinitionError, match="jaffle_mix: template_path not found"):
        discover(manifest, templates)


def test_render_writes_each_format(
    reports: dict[str, Report],
    templates: TemplateEngine,
    tmp_path: Path,
) -> None:
    report = reports["store_revenue_monthly"]
    rows = [("Brooklyn", "2025-01-01", 10, 123.45), ("<script>", None, 0, 0)]

    paths = render(
        report, ["store_name", "order_month", "order_count", "revenue"], rows, tmp_path, templates
    )

    assert [p.name for p in paths] == ["store_revenue_monthly.html", "store_revenue_monthly.csv"]
    with paths[1].open(newline="") as f:
        assert list(csv.reader(f)) == [
            ["store_name", "order_month", "order_count", "revenue"],
            ["Brooklyn", "2025-01-01", "10", "123.45"],
            ["<script>", "", "0", "0"],
        ]
    html = paths[0].read_text(encoding="utf-8")
    assert "<title>Monthly Revenue by Store</title>" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "2 rows" in html
    # The project `finance` template extends the integrated `table` one.
    assert "Finance · internal" in html
    assert "<td>Total</td><td></td><td>10</td><td>123.45</td>" in html


def test_template_params_reach_the_template(reports: dict[str, Report]) -> None:
    params = reports["customer_leaderboard"].config.template_params
    assert params == {
        "label": "customer_name",
        "value": "lifetime_spend",
        "detail": "order_count",
        "prefix": "$",
    }
    assert reports["top_customers"].config.template_params == {}
