"""`dbtx warm-cache [dbt parse options]` (plan Phase 6): refreshes a project's
partial-parse cache without running anything.

Behaviour (platform mode only):
* runs `dbt parse --no-write-json` on the project, with its own profiles (as every
  platform dbt command): dbt writes only the cache, `partial_parse.msgpack`, into the
  project's own target folder; no manifest.json, so the docs site there is untouched.
  Other dbt parse options (`--target`, `--vars`, ...) are passed on
* not recorded: no database, no run folder, no job id
* prints where the cache is, and exits with dbt's code
* standalone mode → exit 2 (it exists to feed the run folders); a `--target-path` →
  exit 2 (the cache must land in the project's target folder, where runs look for it)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbtx import cli
from tests.unit.conftest import FakeDbtRunner

WARM = ["warm-cache", "--project-dir", "jaffle-shop"]


def test_parses_without_writing_json(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(platform)
    project = str(platform / "jaffle-shop")
    assert cli.main(WARM) == 0
    assert fake_dbt.calls == [["parse", "--no-write-json", "--project-dir", project, "--profiles-dir", project]]
    assert capsys.readouterr().out == (
        f"dbtx: warmed the partial-parse cache {platform / 'jaffle-shop' / 'target' / 'partial_parse.msgpack'}\n"
    )


def test_passes_other_parse_options_on(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main([*WARM, "--target", "prod", "--vars", "{region: eu}"]) == 0
    [call] = fake_dbt.calls
    assert call[:6] == ["parse", "--no-write-json", "--target", "prod", "--vars", "{region: eu}"]


def test_is_not_recorded_and_has_no_run_folder(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(WARM) == 0
    assert fake_dbt.job_ids == [None]
    assert not (platform / ".dbtx").exists()  # no database, no run folders


def test_needs_platform_mode(
    lone_project: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(lone_project)
    assert cli.main(["warm-cache", "--standalone-mode"]) == 2
    assert fake_dbt.calls == []
    assert capsys.readouterr().err == (
        "dbtx: `dbtx warm-cache` needs platform mode: it warms the cache that platform runs "
        "copy into their run folders\n"
    )


def test_refuses_a_target_path(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main([*WARM, "--target-path", "elsewhere"]) == 2
    assert fake_dbt.calls == []
    assert capsys.readouterr().err == (
        "dbtx: --target-path conflicts with `dbtx warm-cache`: the cache goes to the "
        "project's target folder, where runs look for it\n"
    )


def test_needs_a_project(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["warm-cache"]) == 2
    assert "choose a project with --project-dir" in capsys.readouterr().err
