"""Test fixtures shared by the unit tests.

State that dbtx reads (environment variables, the current directory, files on disk,
module constants) is built here, per test. Production code takes no extra parameters
for tests (docs/contributing/guidelines.md, "Test fixtures").
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from dbtx import cli, config
from dbtx.store import adapters

CONFIG_SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# --------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test depends on the developer's shell: clear what dbtx and dbt read.
    `DBTX_DB_STRICT`, the test switch `store_strict_mode` sets, stays."""
    for name in list(os.environ):
        if name.startswith("DBTX_") and name != "DBTX_DB_STRICT":
            monkeypatch.delenv(name)
    monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)


@pytest.fixture(autouse=True)
def no_adapter_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test depends on store adapter plugins installed on the machine."""
    monkeypatch.setattr(adapters, "_entry_points", lambda: [])


@pytest.fixture
def adapter_plugins(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, str]], None]:
    """Install fake `dbtx.store_adapters` entry points: `adapter_plugins({name: value})`,
    where value is `module:Class` (see tests/unit/fake_store_adapters.py)."""
    installed: list[EntryPoint] = []

    def install(entries: dict[str, str]) -> None:
        for name, value in entries.items():
            installed.append(EntryPoint(name=name, value=value, group=adapters.ENTRY_POINT_GROUP))

    monkeypatch.setattr(adapters, "_entry_points", lambda: list(installed))
    return install


@pytest.fixture
def enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behaviour after plan Phase 5: database-motivated problems exit 2."""
    monkeypatch.setattr(config, "ENFORCE_DB_REQUIREMENTS", True)


# --------------------------------------------------------------------------------------
# Files and directory layouts
# --------------------------------------------------------------------------------------


def config_sample(name: str) -> Path:
    """A checked-in sample config file from tests/fixtures/config."""
    path = CONFIG_SAMPLES / name
    assert path.is_file(), f"missing sample config file {path}"
    return path


class LayoutBuilder:
    """Builds platform roots and project folders inside one test's tmp_path."""

    def __init__(self, base: Path) -> None:
        self.base = base

    def directory(self, relative: str) -> Path:
        path = self.base / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def platform_root(self, relative: str = "platform", sample: str | None = None) -> Path:
        """A platform root. `sample` is copied in as its dbtx.conf; None means no file."""
        root = self.directory(relative)
        if sample is not None:
            shutil.copyfile(config_sample(sample), root / "dbtx.conf")
        return root

    def project(
        self,
        parent: Path,
        folder: str,
        name: str,
        with_profiles: bool = True,
        dbt_project_text: str | None = None,
    ) -> Path:
        """A dbt project folder: dbt_project.yml (name: `name`) and a profiles.yml."""
        project = parent / folder
        project.mkdir(parents=True)
        if dbt_project_text is None:
            dbt_project_text = f"name: {name}\nprofile: {name}\n"
        (project / "dbt_project.yml").write_text(dbt_project_text)
        if with_profiles:
            (project / "profiles.yml").write_text(
                f"{name}:\n  target: dev\n  outputs: {{}}\n"
            )
        return project

    def config_file(self, directory: Path, sample: str, filename: str = "dbtx.conf") -> Path:
        """Copy a sample config file into `directory` under `filename`."""
        path = directory / filename
        shutil.copyfile(config_sample(sample), path)
        return path


@pytest.fixture
def layout(tmp_path: Path) -> LayoutBuilder:
    return LayoutBuilder(tmp_path.resolve())


@pytest.fixture
def platform(layout: LayoutBuilder) -> Path:
    """The standard platform: a SQLite DB_URI and two projects with distinct names."""
    root = layout.platform_root(sample="platform_sqlite.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    layout.project(root, "other-shop", "other_shop")
    return root


@pytest.fixture
def lone_project(layout: LayoutBuilder) -> Path:
    """A dbt project that is not under any platform root."""
    return layout.project(layout.directory("checkout"), "jaffle-shop", "jaffle_shop")


# --------------------------------------------------------------------------------------
# dbt
# --------------------------------------------------------------------------------------


class FakeResult:
    success = True
    exception = None
    result = None


class FakeDbtRunner:
    """Stands in for dbtRunner in dbtx.cli and records every invoke."""

    calls: list[list[str]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def invoke(self, args: list[str]) -> FakeResult:
        FakeDbtRunner.calls.append(list(args))
        return FakeResult()


@pytest.fixture
def fake_dbt(monkeypatch: pytest.MonkeyPatch) -> type[FakeDbtRunner]:
    FakeDbtRunner.calls = []
    monkeypatch.setattr(cli, "dbtRunner", FakeDbtRunner)
    return FakeDbtRunner
