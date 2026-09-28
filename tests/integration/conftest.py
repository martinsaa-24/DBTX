from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from dbtx.cli import main
from tests.conftest import EXAMPLE_PROJECT

INTEGRATION_DIR = Path(__file__).parent

# dbt's anonymous usage tracking makes network calls on every invocation (~0.4s each,
# and dbtx makes three per build). Set before any dbt call, and inherited by subprocesses.
os.environ["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"

# Seeds are loaded once by `built_project`; tests that rebuild models skip reloading them.
NO_SEEDS = ("--exclude-resource-type", "seed")


def pytest_collection_modifyitems(items):
    for item in items:
        if INTEGRATION_DIR in item.path.parents:
            item.add_marker(pytest.mark.integration)


@contextmanager
def chdir(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class Project:
    """A throwaway copy of examples/jaffle_shop; `dbtx(...)` runs inside it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.reports = root / "target" / "reports"
        self.compiled_sql = root / "target" / "compiled" / "jaffle_shop" / "reports"
        self.run_sql = root / "target" / "run" / "jaffle_shop" / "reports"

    def dbtx(self, *args: str) -> int:
        with chdir(self.root):
            return main([*args, "--no-use-colors"])

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def report_files(self) -> set[str]:
        return _names(self.reports)

    def compiled_sql_files(self) -> set[str]:
        return _names(self.compiled_sql)

    def run_sql_files(self) -> set[str]:
        return _names(self.run_sql)

    def clear_reports(self) -> None:
        for folder in (self.reports, self.compiled_sql, self.run_sql):
            shutil.rmtree(folder, ignore_errors=True)


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def copy_project(source: Path, dest: Path) -> Project:
    # Keeps target/partial_parse.msgpack: dbt re-parses only files a test changes,
    # which cuts each parse from ~2s to ~0.2s.
    ignore = shutil.ignore_patterns("logs", "dbt_packages")
    shutil.copytree(source, dest, ignore=ignore)
    return Project(dest)


def _build_example(root: Path) -> Project:
    project = Project(root)
    ignore = shutil.ignore_patterns("target", "logs", "dbt_packages", "*.duckdb*", ".user.yml")
    shutil.copytree(EXAMPLE_PROJECT, project.root, ignore=ignore)
    # In a subprocess: dbt-duckdb keeps the database file open for the life of the
    # process, and Windows would then refuse to copy it for the `project` fixture.
    subprocess.run(
        [sys.executable, "-m", "dbtx", "build", "--quiet"], cwd=project.root, check=True
    )
    return project


@pytest.fixture(scope="session")
def built_project(tmp_path_factory) -> Project:
    """The example project after one full `dbtx build`. Treat as read-only; use `project` to modify."""
    return _build_example(tmp_path_factory.mktemp("built") / "jaffle_shop")


@pytest.fixture
def project(built_project, tmp_path) -> Project:
    """A private, already-built copy (warehouse included) that a test may modify."""
    copy = copy_project(built_project.root, tmp_path / "jaffle_shop")
    copy.clear_reports()
    return copy
