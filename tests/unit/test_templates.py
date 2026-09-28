from pathlib import Path

import pytest

from dbtx.templates import (
    INTEGRATED_DIR,
    TemplateEngine,
    TemplateError,
    TemplateRegistry,
    TemplateSource,
    project_template_folders,
    template_name,
)
from tests.conftest import EXAMPLE_PROJECT

PROJECT, INTEGRATED, EXPOSURE = TemplateSource.PROJECT, TemplateSource.INTEGRATED, TemplateSource.EXPOSURE


def write(folder: Path, name: str, content: str = "<p>{{ report.title }}</p>") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("finance_template.html.j2", "finance"),
        ("finance_template.html", "finance"),
        ("sales_by_region_template.html", "sales_by_region"),
        ("finance.html.j2", None),
        ("finance_template.j2", None),
        ("finance_template.html.bak", None),
        ("_template.html", None),
    ],
)
def test_template_name(filename, expected):
    assert template_name(filename) == expected


def test_integrated_templates_ship_with_dbtx():
    registry = TemplateRegistry.build([])
    table = registry.get("table")
    assert table.source is INTEGRATED
    assert table.path == (INTEGRATED_DIR / "table_template.html.j2").resolve()


def test_rows_expose_name_source_and_uri(tmp_path):
    finance = write(tmp_path / "t", "finance_template.html")
    registry = TemplateRegistry.build([tmp_path / "t"])
    assert registry.rows() == [
        ("finance", "project", finance.resolve().as_uri()),
        ("table", "integrated", (INTEGRATED_DIR / "table_template.html.j2").resolve().as_uri()),
    ]
    assert all(uri.startswith("file:///") for _, _, uri in registry.rows())


def test_project_template_replaces_integrated_one(tmp_path):
    override = write(tmp_path / "t", "table_template.html.j2")
    entry = TemplateRegistry.build([tmp_path / "t"]).get("table")
    assert (entry.source, entry.path) == (PROJECT, override.resolve())


def test_distinct_names_across_project_folders_combine(tmp_path):
    write(tmp_path / "a", "finance_template.html")
    write(tmp_path / "b", "sales_template.html.j2")
    registry = TemplateRegistry.build([tmp_path / "a", tmp_path / "b"])
    assert [e.name for e in registry] == ["finance", "sales", "table"]


def test_same_name_in_two_project_folders_is_an_error(tmp_path):
    first = write(tmp_path / "a", "finance_template.html")
    second = write(tmp_path / "b", "finance_template.html.j2")  # different suffix, same name
    with pytest.raises(TemplateError, match="'finance' is defined in two project template folders") as exc:
        TemplateRegistry.build([tmp_path / "a", tmp_path / "b"])
    assert str(first.resolve()) in str(exc.value) and str(second.resolve()) in str(exc.value)


def test_same_name_twice_in_one_folder_is_an_error(tmp_path):
    write(tmp_path / "t", "finance_template.html")
    write(tmp_path / "t", "finance_template.html.j2")
    with pytest.raises(TemplateError, match="'finance' is defined twice in"):
        TemplateRegistry.build([tmp_path / "t"])


def test_missing_or_repeated_folders_are_errors(tmp_path):
    with pytest.raises(TemplateError, match="template folder not found"):
        TemplateRegistry.build([tmp_path / "nope"])
    (tmp_path / "t").mkdir()
    with pytest.raises(TemplateError, match="template folder listed twice"):
        TemplateRegistry.build([tmp_path / "t", tmp_path / "t" / ".." / "t"])


def test_only_matching_files_directly_in_the_folder_count(tmp_path):
    write(tmp_path / "t", "notes.html")
    write(tmp_path / "t", "finance.html.j2")
    write(tmp_path / "t" / "nested", "deep_template.html")
    assert [e.name for e in TemplateRegistry.build([tmp_path / "t"])] == ["table"]


def test_project_folders_come_from_dbt_project_vars(tmp_path):
    assert project_template_folders(EXAMPLE_PROJECT) == [EXAMPLE_PROJECT / "report_templates"]

    write(tmp_path, "dbt_project.yml", "name: p\n")
    assert project_template_folders(tmp_path) == []

    write(tmp_path, "dbt_project.yml", "name: p\nvars:\n  dbtx:\n    template_paths: shared\n")
    with pytest.raises(TemplateError, match="must be a list of folder paths"):
        project_template_folders(tmp_path)


def test_example_project_registry():
    registry = TemplateRegistry.for_project(EXAMPLE_PROJECT)
    assert [(e.name, e.source) for e in registry] == [
        ("finance", PROJECT),
        ("jaffle_base", PROJECT),
        ("leaderboard", PROJECT),
        ("scorecard", PROJECT),
        ("table", INTEGRATED),
    ]


class TestEngine:
    @pytest.fixture
    def engine(self, tmp_path):
        write(tmp_path / "t", "finance_template.html", "finance {{ report }}")
        return TemplateEngine(TemplateRegistry.build([tmp_path / "t"]))

    def test_resolves_names_from_the_registry(self, engine):
        assert engine.resolve("finance", None).source is PROJECT
        assert engine.resolve("table", None).source is INTEGRATED

    def test_exact_path_wins_over_name(self, engine, tmp_path):
        exact = write(tmp_path / "one_off", "anything.html", "exact {{ report }}")
        entry = engine.resolve("finance", exact)
        assert (entry.source, entry.path) == (EXPOSURE, exact.resolve())
        assert engine.load(entry).render(report="r") == "exact r"

    def test_unknown_name_lists_what_is_available(self, engine):
        with pytest.raises(TemplateError, match=r"unknown template 'nope'; available: finance \(project\), table \(integrated\)"):
            engine.resolve("nope", None)

    def test_missing_exact_path(self, engine, tmp_path):
        with pytest.raises(TemplateError, match="template_path not found"):
            engine.resolve(None, tmp_path / "missing.html")

    def test_syntax_errors_name_the_file_and_line(self, tmp_path):
        broken = write(tmp_path / "t", "broken_template.html", "<p>\n{% if %}\n")
        engine = TemplateEngine(TemplateRegistry.build([tmp_path / "t"]))
        with pytest.raises(TemplateError) as exc:
            engine.load(engine.resolve("broken", None))
        assert f"{broken.resolve()}, line 2" in str(exc.value)

    def test_project_templates_can_extend_integrated_ones(self, tmp_path):
        write(tmp_path / "t", "branded_template.html", '{% extends "table" %}{% block footer %}BRAND{% endblock %}')
        engine = TemplateEngine(TemplateRegistry.build([tmp_path / "t"]))
        report = type("R", (), {"title": "T", "description": "", "owner": ""})()
        html = engine.load(engine.resolve("branded", None)).render(
            report=report, columns=["a"], rows=[(1,)], records=[{"a": 1}], generated_at="now"
        )
        assert "<title>T</title>" in html and "BRAND" in html

    def test_undefined_variables_fail_loudly(self, tmp_path):
        write(tmp_path / "t", "typo_template.html", "{{ reprot.title }}")
        engine = TemplateEngine(TemplateRegistry.build([tmp_path / "t"]))
        with pytest.raises(Exception, match="'reprot' is undefined"):
            engine.load(engine.resolve("typo", None)).render(report=None)
