"""The run folder at run time (`dbtx/runs.py`, plan Phase 6), through `cli.main`.

Behaviour, for a platform command with a run folder (its path, job id and the project's
target folder come from the configuration, `test_config_run_folder.py`):
* the folder is created only once the configuration and the start-up checks have
  passed: a failure before dbt starts leaves nothing behind
* the project's warm partial-parse cache (`<project target folder>/partial_parse.msgpack`,
  from `dbtx warm-cache`) is copied in when present; without one dbt parses in full
* the folder appearing between the checks and its creation (another run with the same
  job id) → exit 2, before dbt starts
* while dbt runs it sees the job id as `DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID`; afterwards the
  environment is as it was. dbt caches those variables per process
  (`get_metadata_vars`), so the cache is reset on the way in and out: a second command
  in the same process records its own job id, not the first one's
* the project's target folder: `DBT_TARGET_PATH`, else `target-path` in
  dbt_project.yml, else `target`, relative to the project (as dbt resolves it)
* commands without a run folder, and standalone mode: no folder, no job id for dbt
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dbtx import cli, runs
from dbtx.config import load_config
from tests.unit.conftest import RUN_JOB_ID, initialise_database, FakeDbtRunner, LayoutBuilder

RUN = ["run", "--project-dir", "jaffle-shop"]


def run_folder(platform: Path, job_id: str = RUN_JOB_ID) -> Path:
    return platform / ".dbtx" / "logs" / "jaffle_shop" / "2026-10-12" / "14" / job_id


def warm_cache(platform: Path, target: str = "target", content: bytes = b"warm cache") -> Path:
    path = platform / "jaffle-shop" / target / "partial_parse.msgpack"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture(autouse=True)
def database(request: pytest.FixtureRequest) -> None:
    """A test on the standard platform starts from an initialised database, as a user's
    first run does after `dbtx db init` (schema changes are deliberate)."""
    if "platform" in request.fixturenames:
        initialise_database(request.getfixturevalue("platform"))


# --------------------------------------------------------------------------------------
# The folder and the cache
# --------------------------------------------------------------------------------------


def test_the_run_folder_is_created_with_the_warm_cache(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], fixed_run: None
) -> None:
    warm_cache(platform)
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    assert (run_folder(platform) / "partial_parse.msgpack").read_bytes() == b"warm cache"
    assert sorted(path.name for path in run_folder(platform).iterdir()) == ["partial_parse.msgpack"]


def test_without_a_warm_cache_the_folder_starts_empty(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], fixed_run: None
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    assert list(run_folder(platform).iterdir()) == []


def test_the_cache_comes_from_the_projects_own_target_path(
    layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], fixed_run: None
) -> None:
    root = layout.platform_root(sample="platform_sqlite.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop", dbt_project_text="name: jaffle_shop\ntarget-path: out\n")
    warm_cache(root, target="target", content=b"not this one")
    warm_cache(root, target="out", content=b"this one")
    initialise_database(root)
    monkeypatch.chdir(root)
    assert cli.main(RUN) == 0
    assert (run_folder(root) / "partial_parse.msgpack").read_bytes() == b"this one"


def test_a_failed_start_up_check_leaves_no_folder(
    platform: Path, layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner],
    fixed_run: None,
) -> None:
    blocker = layout.directory("blocked") / "not-a-directory"
    blocker.write_text("a file where the database directory should be")
    monkeypatch.chdir(platform)
    assert cli.main([*RUN, "--db-uri", f"sqlite:///{blocker}/dbtx.db"]) == 2
    assert fake_dbt.calls == []
    assert not (platform / ".dbtx" / "logs").exists()


def test_a_folder_that_appeared_meanwhile_stops_the_run(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fixed_run: None
) -> None:
    monkeypatch.chdir(platform)
    config = load_config(RUN)
    run_folder(platform).mkdir(parents=True)
    with pytest.raises(runs.RunFolderError) as info:
        runs.prepare_run_folder(config)
    assert str(info.value) == (
        f"run folder {run_folder(platform)} already exists: job id {RUN_JOB_ID!r} has "
        f"already run this hour. Every run needs its own job id"
    )


def test_the_cache_is_never_half_copied(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fixed_run: None
) -> None:
    # Copied to a temporary name, then renamed: no partial_parse.msgpack is ever partial.
    warm_cache(platform, content=b"x" * 100_000)
    monkeypatch.chdir(platform)
    config = load_config(RUN)
    renamed: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(source: str, destination: str) -> None:
        renamed.append((Path(source).name, Path(destination).name))
        real_replace(source, destination)

    monkeypatch.setattr(runs.os, "replace", recording_replace)
    runs.prepare_run_folder(config)
    assert renamed == [("partial_parse.msgpack.tmp", "partial_parse.msgpack")]
    assert (run_folder(platform) / "partial_parse.msgpack").stat().st_size == 100_000


# --------------------------------------------------------------------------------------
# The job id for dbt
# --------------------------------------------------------------------------------------


def test_dbt_sees_the_job_id_only_while_it_runs(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], fixed_run: None
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main([*RUN, "--job-id", "nightly-42"]) == 0
    assert fake_dbt.job_ids == ["nightly-42"]
    assert "DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID" not in os.environ


def test_a_previous_value_is_restored(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], fixed_run: None
) -> None:
    monkeypatch.setenv("DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID", "from-the-shell")
    monkeypatch.chdir(platform)
    assert cli.main(RUN) == 0
    assert fake_dbt.job_ids == [RUN_JOB_ID]
    assert os.environ["DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID"] == "from-the-shell"


def test_dbts_cached_metadata_follows_the_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_common.events.functions import get_metadata_vars, reset_metadata_vars

    monkeypatch.delenv("DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID", raising=False)
    reset_metadata_vars()
    try:
        with runs.job_id_for_dbt("first"):
            assert get_metadata_vars().get("DBTX_JOB_ID") == "first"
        # A second run in the same process (dbt cached the first value).
        with runs.job_id_for_dbt("second"):
            assert get_metadata_vars().get("DBTX_JOB_ID") == "second"
        assert "DBTX_JOB_ID" not in get_metadata_vars()
    finally:
        reset_metadata_vars()


def test_a_command_without_a_run_folder_gets_no_job_id(
    platform: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner], fixed_run: None
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["ls", "--project-dir", "jaffle-shop"]) == 0
    assert fake_dbt.job_ids == [None]
    assert not (platform / ".dbtx" / "logs").exists()


def test_standalone_mode_has_no_run_folder(
    lone_project: Path, monkeypatch: pytest.MonkeyPatch, fake_dbt: type[FakeDbtRunner]
) -> None:
    monkeypatch.chdir(lone_project)
    assert cli.main(["run", "--standalone-mode"]) == 0
    assert fake_dbt.job_ids == [None]
    assert fake_dbt.calls == [["run"]]


# --------------------------------------------------------------------------------------
# The project's target folder
# --------------------------------------------------------------------------------------


def test_the_project_target_folder_defaults_to_target(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    assert load_config(RUN).project_target_dir == platform / "jaffle-shop" / "target"


def test_the_project_target_folder_from_dbt_project_yml(layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    root = layout.platform_root(sample="platform_sqlite.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop", dbt_project_text="name: jaffle_shop\ntarget-path: out\n")
    monkeypatch.chdir(root)
    assert load_config(RUN).project_target_dir == root / "jaffle-shop" / "out"


def test_the_project_target_folder_from_dbt_target_path(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBT_TARGET_PATH", "env-target")
    monkeypatch.chdir(platform)
    assert load_config(RUN).project_target_dir == platform / "jaffle-shop" / "env-target"


def test_standalone_has_no_project_target_folder(lone_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(lone_project)
    assert load_config(["run", "--standalone-mode"]).project_target_dir is None
