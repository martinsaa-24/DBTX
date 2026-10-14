"""`load_config` for isolated runs (plan Phase 6): LOG_ROOT_LOCATION, the job id and the
run folder.

Behaviour (platform mode):
* `LOG_ROOT_LOCATION`: `--log-root-location` > `LOG_ROOT_LOCATION=` in dbtx.conf >
  `DBTX_LOG_ROOT_LOCATION`, with its source; a relative path is resolved against the
  layer that set it (the config file's folder; the current directory for the CLI and
  the environment); `~` is expanded
* commands that run in a run folder (the recorded commands: build, compile, run, test,
  seed, snapshot, parse, run-operation, source freshness, docs generate) require it:
  exit 2 naming the ways to set it, whatever DB_BACKEND says. Other commands (ls, deps,
  config, db status, ...) do not
* the job id: `--job-id ID` (a plain folder name: no `/`, not `.` / `..`), else a new
  one (`config.new_job_id()`). `--job-id` with a command that has no run folder → exit 2
* `DBTX_JOB_ID` in `--vars` → exit 2 (dbtx sets the job id); other vars are fine, and
  vars that are not valid YAML are left to dbt
* a user's own `--target-path` / `--log-path` → exit 2 for a command with a run folder
* the run folder: `<root>/<dbt project name>/<YYYY-MM-DD>/<HH>/<job id>` (UTC, from
  `config.run_started_at()`); the folder name when dbt_project.yml has no usable name.
  It already exists → exit 2. `load_config` does not create it
* dbt gets `--target-path <run folder>` and `--log-path <run folder>`
* `dbtx config` shows LOG_ROOT_LOCATION and its source

Standalone mode: `--log-root-location` / `--job-id` → exit 2; `DBTX_LOG_ROOT_LOCATION`
is ignored (listed as ignored); `--vars '{DBTX_JOB_ID: ...}'` is passed to dbt untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbtx import cli
from dbtx.config import ConfigError, load_config
from tests.unit.conftest import RUN_JOB_ID, LayoutBuilder

PROJECT = ["--project-dir", "jaffle-shop"]
BUILD = ["build", *PROJECT]
JOB_ID = RUN_JOB_ID


@pytest.fixture(autouse=True)
def every_run_is_fixed(fixed_run: None) -> None:
    """Every run in this file starts at RUN_STARTED (2026-10-12 14:37 UTC) and, without
    --job-id, gets RUN_JOB_ID."""


@pytest.fixture
def platform_without_log_root(layout: LayoutBuilder) -> Path:
    root = layout.platform_root(sample="platform_without_log_root.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    return root


def run_folder(root: Path, job_id: str = JOB_ID) -> Path:
    return root / ".dbtx" / "logs" / "jaffle_shop" / "2026-10-12" / "14" / job_id


# --------------------------------------------------------------------------------------
# LOG_ROOT_LOCATION: layers and paths
# --------------------------------------------------------------------------------------


def test_log_root_from_the_config_file_is_relative_to_it(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform / "jaffle-shop")
    cfg = load_config([*BUILD, "--platform-dir", str(platform)])
    assert cfg.log_root_location.value == str(platform / ".dbtx" / "logs")
    assert cfg.log_root_location.source == f"file {platform / 'dbtx.conf'}:2"


def test_log_root_from_the_cli_beats_the_file(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--log-root-location", "runs"])
    assert (cfg.log_root_location.value, cfg.log_root_location.source) == (str(platform / "runs"), "cli")


def test_log_root_from_the_file_beats_the_env(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_LOG_ROOT_LOCATION", "/elsewhere")
    assert load_config(BUILD).log_root_location.value == str(platform / ".dbtx" / "logs")


def test_log_root_from_the_env(platform_without_log_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform_without_log_root)
    monkeypatch.setenv("DBTX_LOG_ROOT_LOCATION", "env-runs")
    cfg = load_config(BUILD)
    assert cfg.log_root_location.value == str(platform_without_log_root / "env-runs")
    assert cfg.log_root_location.source == "env DBTX_LOG_ROOT_LOCATION"


def test_log_root_expands_the_home_folder(platform: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--log-root-location", "~/runs"])
    assert cfg.log_root_location.value == str(tmp_path / "home" / "runs")


def test_dbtx_log_root_location_is_a_known_env_var(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_LOG_ROOT_LOCATION", "/x")
    assert not any("DBTX_LOG_ROOT_LOCATION" in warning for warning in load_config(BUILD).warnings)


# --------------------------------------------------------------------------------------
# LOG_ROOT_LOCATION: the requirement
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["build", "compile", "run", "test", "seed", "snapshot", "parse", "run-operation", "source freshness",
     "docs generate"],
)
def test_a_command_with_a_run_folder_requires_it(
    platform_without_log_root: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.chdir(platform_without_log_root)
    with pytest.raises(ConfigError) as info:
        load_config([*command.split(), *PROJECT])
    assert str(info.value) == (
        f"`dbtx {command}` needs a run folder root: set LOG_ROOT_LOCATION (with "
        f"--log-root-location, LOG_ROOT_LOCATION= in {platform_without_log_root / 'dbtx.conf'}, or "
        f"DBTX_LOG_ROOT_LOCATION), or pass --standalone-mode"
    )


def test_required_even_with_the_backend_disabled(platform_without_log_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform_without_log_root)
    with pytest.raises(ConfigError, match="needs a run folder root"):
        load_config([*BUILD, "--db-backend", "disabled"])


@pytest.mark.parametrize("argv", [["ls", *PROJECT], ["deps", *PROJECT], ["config"], ["db", "status"], ["--help"]])
def test_other_commands_do_not_need_it(
    platform_without_log_root: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.chdir(platform_without_log_root)
    cfg = load_config(argv)
    assert (cfg.job_id, cfg.run_folder) == (None, None)


# --------------------------------------------------------------------------------------
# The job id
# --------------------------------------------------------------------------------------


def test_a_job_id_is_generated(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    assert load_config(BUILD).job_id == JOB_ID


@pytest.mark.parametrize("flag", [["--job-id", "nightly-42"], ["--job-id=nightly-42"]])
def test_a_given_job_id(platform: Path, monkeypatch: pytest.MonkeyPatch, flag: list[str]) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, *flag])
    assert cfg.job_id == "nightly-42"
    assert "--job-id" not in " ".join(cfg.argv)  # dbtx's flag, never dbt's


@pytest.mark.parametrize("job_id", ["a/b", "a\\b", ".", ".."])
def test_a_job_id_must_be_a_folder_name(platform: Path, monkeypatch: pytest.MonkeyPatch, job_id: str) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--job-id", job_id])
    assert str(info.value) == f"--job-id {job_id!r} is not a plain folder name"


def test_a_job_id_needs_a_command_with_a_run_folder(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config(["ls", *PROJECT, "--job-id", "x"])
    assert str(info.value) == "--job-id applies only to commands that run in a run folder, not `dbtx ls`"


@pytest.mark.parametrize(
    "vars_args",
    [["--vars", "{DBTX_JOB_ID: mine}"], ["--vars={DBTX_JOB_ID: mine, other: 1}"], ["--vars", '{"DBTX_JOB_ID": "mine"}']],
)
def test_dbtx_job_id_in_vars_is_refused(platform: Path, monkeypatch: pytest.MonkeyPatch, vars_args: list[str]) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, *vars_args])
    assert str(info.value) == (
        "DBTX_JOB_ID in --vars conflicts with platform mode: dbtx sets the job id. Use "
        "--job-id ID instead"
    )


def test_dbtx_job_id_in_vars_is_refused_for_any_dbt_command(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="DBTX_JOB_ID in --vars"):
        load_config(["ls", *PROJECT, "--vars", "{DBTX_JOB_ID: x}"])


@pytest.mark.parametrize("vars_value", ["{job_id: legacy, region: eu}", "::not yaml::", "[1, 2]"])
def test_other_vars_are_passed_on(platform: Path, monkeypatch: pytest.MonkeyPatch, vars_value: str) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--vars", vars_value])
    assert vars_value in cfg.argv


# --------------------------------------------------------------------------------------
# The run folder
# --------------------------------------------------------------------------------------


def test_the_run_folder_layout(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert cfg.run_folder == run_folder(platform)
    assert not cfg.run_folder.exists()  # created at run time, after the start-up checks


def test_the_run_folder_is_named_for_the_given_job_id(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    assert load_config([*BUILD, "--job-id", "nightly-42"]).run_folder == run_folder(platform, "nightly-42")


def test_dbt_gets_the_run_folder_as_target_and_log_path(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    argv = list(load_config(BUILD).argv)
    folder = str(run_folder(platform))
    assert argv[argv.index("--target-path") + 1] == folder
    assert argv[argv.index("--log-path") + 1] == folder


def test_other_commands_get_no_run_folder(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    argv = list(load_config(["ls", *PROJECT]).argv)
    assert "--target-path" not in argv and "--log-path" not in argv


def test_an_existing_run_folder_is_refused(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_folder(platform, "nightly-42").mkdir(parents=True)
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--job-id", "nightly-42"])
    assert str(info.value) == (
        f"run folder {run_folder(platform, 'nightly-42')} already exists: job id 'nightly-42' "
        f"has already run this hour. Every run needs its own job id"
    )


def test_the_folder_name_when_the_project_has_no_name(layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    root = layout.platform_root(sample="platform_sqlite.conf")
    layout.project(root, "nameless", "x", dbt_project_text="profile: x\n")
    monkeypatch.chdir(root)
    cfg = load_config(["build", "--project-dir", "nameless"])
    assert cfg.run_folder == root / ".dbtx" / "logs" / "nameless" / "2026-10-12" / "14" / JOB_ID


@pytest.mark.parametrize("flag", ["--target-path", "--log-path"])
def test_a_users_own_target_or_log_path_is_refused(platform: Path, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, flag, "out"])
    assert str(info.value) == (
        f"{flag} conflicts with platform mode: `dbtx build` writes to its run folder under "
        f"LOG_ROOT_LOCATION"
    )


def test_a_users_own_target_path_is_fine_for_other_commands(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    assert "--target-path=out" in load_config(["ls", *PROJECT, "--target-path=out"]).argv


# --------------------------------------------------------------------------------------
# dbtx config
# --------------------------------------------------------------------------------------


def test_dbtx_config_shows_the_log_root(platform: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert f"  LOG_ROOT_LOCATION  {platform / '.dbtx' / 'logs'}  [file {platform / 'dbtx.conf'}:2]" in out


def test_dbtx_config_shows_a_missing_log_root(
    platform_without_log_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(platform_without_log_root)
    assert cli.main(["config"]) == 0
    assert "  LOG_ROOT_LOCATION  not set" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# Standalone mode
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag, reason",
    [(["--log-root-location", "runs"], "--log-root-location"), (["--job-id", "x"], "--job-id")],
)
def test_run_folder_flags_contradict_standalone_mode(
    lone_project: Path, monkeypatch: pytest.MonkeyPatch, flag: list[str], reason: str
) -> None:
    monkeypatch.chdir(lone_project)
    with pytest.raises(ConfigError) as info:
        load_config(["build", "--standalone-mode", *flag])
    assert str(info.value) == (
        f"{reason} has no effect with --standalone-mode: run folders are a platform feature"
    )


def test_standalone_ignores_the_log_root_env_var(lone_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(lone_project)
    monkeypatch.setenv("DBTX_LOG_ROOT_LOCATION", "/runs")
    cfg = load_config(["build", "--standalone-mode"])
    assert (cfg.log_root_location, cfg.job_id, cfg.run_folder) == (None, None, None)
    assert "DBTX_LOG_ROOT_LOCATION" in cfg.ignored_env_vars
    assert "--target-path" not in cfg.argv


def test_standalone_passes_dbtx_job_id_vars_to_dbt(lone_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(lone_project)
    cfg = load_config(["build", "--standalone-mode", "--vars", "{DBTX_JOB_ID: mine}"])
    assert cfg.argv == ("build", "--vars", "{DBTX_JOB_ID: mine}")
