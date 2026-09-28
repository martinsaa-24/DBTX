"""The example project's extended templates, rendered with fixed rows (no warehouse)."""

import re
from decimal import Decimal

import pytest

from dbtx.reports import render

BRAND = "Jaffle Shop · Analytics"


@pytest.fixture
def html(reports, templates, tmp_path):
    def _render(name, columns, rows):
        [path] = [p for p in render(reports[name], columns, rows, tmp_path, templates) if p.suffix == ".html"]
        return path.read_text(encoding="utf-8")

    return _render


def test_jaffle_base_style_is_inherited_by_every_child(html):
    leaderboard = html("customer_leaderboard", ["customer_name", "order_count", "lifetime_spend"], [("Ann", 2, Decimal("10"))])
    scorecard = html("store_scorecard", ["store_name", "order_count", "revenue"], [("A", 1, Decimal("1"))])
    finance = html("store_revenue_monthly", ["store_name", "revenue"], [("A", Decimal("1"))])

    for page in (leaderboard, scorecard, finance):
        assert BRAND in page  # jaffle_base header block
        assert "source: dbt exposure" in page  # jaffle_base footer block
        assert "--accent: #c2410c" in page  # jaffle_base style block, kept via super()
        assert "border-collapse: collapse" in page  # integrated table style, two levels up


def test_integrated_table_alone_has_no_house_style(html):
    page = html("top_customers", ["customer_name"], [("Ann",)])
    assert BRAND not in page and "--accent: #c2410c" not in page


def test_finance_adds_banner_and_configured_totals(html):
    page = html(
        "store_revenue_monthly",
        ["store_name", "order_month", "order_count", "revenue", "tax_paid"],
        [("A", "2025-01-01", 3, Decimal("10.50"), Decimal("0.63")), ("B", "2025-01-01", 4, Decimal("20.25"), Decimal("1.22"))],
    )
    assert page.index(BRAND) < page.index("Finance · internal")  # super() first, then banner
    assert "<td>Total</td><td></td><td>7</td><td>30.75</td><td>1.85</td>" in page


def test_leaderboard_ranks_and_scales_bars(html):
    page = html(
        "customer_leaderboard",
        ["customer_name", "order_count", "lifetime_spend"],
        [("Ann", 12, Decimal("2000.00")), ("Bob", 5, Decimal("500.50")), ("Cy", 1, Decimal("0"))],
    )
    assert "<table>" not in page  # content block replaced
    assert re.findall(r'class="name">([^<]+)', page) == ["Ann", "Bob", "Cy"]
    assert re.findall(r'style="width: ([\d.]+)%', page) == ["100.0", "25.0", "0.0"]
    assert re.findall(r'class="val">([^<]+)', page) == ["$2,000.00", "$500.50", "$0.00"]
    assert "<small>order count: 12</small>" in page


def test_leaderboard_is_reused_with_different_params(html):
    page = html("beverage_leaderboard", ["product_name", "product_type", "units_sold", "revenue"], [("tangaroo", "beverage", 1950, Decimal("11700"))])
    assert re.findall(r'class="val">([^<]+)', page) == ["1,950"]  # no prefix, 0 decimals
    assert "<small>" not in page  # no detail column configured


def test_leaderboard_handles_no_rows(html):
    page = html("customer_leaderboard", ["customer_name", "order_count", "lifetime_spend"], [])
    assert '<ol class="board">' in page and "<li>" not in page


def test_scorecard_tiles_sit_above_the_inherited_table(html):
    page = html(
        "store_scorecard",
        ["store_name", "order_count", "revenue"],
        [("A", 1200, Decimal("24000.50")), ("B", 800, Decimal("16000.25"))],
    )
    tiles = dict(re.findall(r'<div class="k">([^<]+)</div>\s*<div class="v">([^<]+)</div>', page))
    assert tiles == {
        "Stores": "2",
        "Orders": "2,000",
        "Revenue": "$40,000.75",
        "Best store revenue": "$24,000.50",
    }
    assert page.index('class="tiles"') < page.index("<table>")  # super() renders the table after
