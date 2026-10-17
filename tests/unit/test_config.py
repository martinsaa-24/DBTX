"""dbtx.config: modes, layers, validation and guard G1 (plan section 6, Phase 0).

Every test builds its own clean directory structure with the `layout` / `platform` /
`lone_project` fixtures (tests/unit/conftest.py) and uses checked-in sample config
files. The parser itself is tested in test_config_file.py.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest
import sqlalchemy as sa

from dbtx import config
from dbtx.config import ConfigError, load_config
from dbtx.store.adapters import PostgresAdapter, SqliteAdapter
from tests.unit.conftest import LayoutBuilder, config_sample
from tests.unit.fake_store_adapters import CustomSqliteAdapter

# --------------------------------------------------------------------------------------
# Step 1: argv extraction (pure functions: no state needed)
# --------------------------------------------------------------------------------------


def test_extracts_value_flags_in_both_forms() -> None:
    values, standalone, rest = config.extract_dbtx_flags(
        ["build", "--db-uri", "sqlite://", "-s", "x", "--db-backend=disabled"]
    )
    assert values == {"--db-uri": "sqlite://", "--db-backend": "disabled"}
    assert standalone is False
    assert rest == ["build", "-s", "x"]


def test_extracts_the_db_schema_flag_in_both_forms() -> None:
    values, _, rest = config.extract_dbtx_flags(["build", "--db-schema", "a", "-s", "x"])
    assert values == {"--db-schema": "a"}
    assert rest == ["build", "-s", "x"]
    values, _, rest = config.extract_dbtx_flags(["build", "--db-schema=b"])
    assert values == {"--db-schema": "b"}
    assert rest == ["build"]


def test_extracts_the_db_adapter_flag_in_both_forms() -> None:
    values, _, rest = config.extract_dbtx_flags(["build", "--db-adapter", "a", "-s", "x"])
    assert values == {"--db-adapter": "a"}
    assert rest == ["build", "-s", "x"]
    values, _, rest = config.extract_dbtx_flags(["build", "--db-adapter=b"])
    assert values == {"--db-adapter": "b"}
    assert rest == ["build"]


def test_extracts_flags_before_the_command_too() -> None:
    values, standalone, rest = config.extract_dbtx_flags(
        ["--platform-dir", "/p", "--standalone-mode", "build"]
    )
    assert values == {"--platform-dir": "/p"}
    assert standalone is True
    assert rest == ["build"]


@pytest.mark.parametrize(
    "argv",
    [
        ["build", "--db-uri"],
        ["build", "--db-uri", "--select", "x"],
        ["build", "--db-uri="],
        ["build", "--db-schema"],
        ["build", "--db-schema="],
        ["build", "--db-adapter"],
        ["build", "--db-adapter="],
    ],
)
def test_flag_without_a_value_is_an_error(argv: list[str]) -> None:
    flag = argv[1].split("=")[0]
    with pytest.raises(ConfigError, match=f"^{flag} needs a value$"):
        config.extract_dbtx_flags(argv)


def test_flag_given_twice_is_an_error() -> None:
    with pytest.raises(ConfigError, match="more than once"):
        config.extract_dbtx_flags(["--db-uri", "a", "--db-uri", "b"])


def test_standalone_mode_takes_no_value() -> None:
    with pytest.raises(ConfigError, match="takes no value"):
        config.extract_dbtx_flags(["--standalone-mode=yes"])


@pytest.mark.parametrize(
    "argv, command",
    [
        (["build", "-s", "x"], "build"),
        (["docs", "generate"], "docs generate"),
        (["source", "freshness"], "source freshness"),
        (["db", "status"], "db status"),
        (["docs", "--help"], "docs"),
        (["--debug", "build"], ""),
        ([], ""),
    ],
)
def test_command_name(argv: list[str], command: str) -> None:
    assert config.command_name(argv) == command


# --------------------------------------------------------------------------------------
# Step 2: mode
# --------------------------------------------------------------------------------------


def test_cwd_holding_dbtx_conf_is_the_platform(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(["build", "--project-dir", "jaffle-shop"])
    assert cfg.mode == "platform"
    assert cfg.platform_root == platform
    assert "current directory" in cfg.mode_reason


def test_platform_dir_flag(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(layout.directory("elsewhere"))
    cfg = load_config(["build", "--platform-dir", str(platform), "--project-dir", "jaffle-shop"])
    assert cfg.mode == "platform"
    assert cfg.platform_root == platform
    assert cfg.mode_reason == "--platform-dir"


def test_platform_dir_must_exist(layout: LayoutBuilder) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(["build", "--platform-dir", str(layout.base / "nope")])


def test_platform_dir_must_be_a_directory(layout: LayoutBuilder) -> None:
    a_file = layout.base / "a-file"
    a_file.write_text("")
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(["build", "--platform-dir", str(a_file)])


def test_platform_dir_must_not_be_a_project(platform: Path) -> None:
    with pytest.raises(ConfigError, match="is a dbt project"):
        load_config(["build", "--platform-dir", str(platform / "jaffle-shop")])


def test_platform_dir_needs_a_dbtx_conf(layout: LayoutBuilder) -> None:
    root = layout.platform_root(sample=None)
    layout.project(root, "jaffle-shop", "jaffle_shop")
    with pytest.raises(ConfigError, match="has no dbtx.conf"):
        load_config(["build", "--platform-dir", str(root), "--project-dir", "jaffle-shop"])


def test_project_folder_with_dbtx_conf_is_never_a_platform_root(
    lone_project: Path, layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout.config_file(lone_project, "platform_sqlite.conf")
    monkeypatch.chdir(lone_project)
    with pytest.raises(ConfigError, match="is never a platform root"):
        load_config(["build"])


def test_no_parent_directory_search(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cwd is a project folder whose parent is a platform root: still no platform.
    monkeypatch.chdir(platform / "jaffle-shop")
    with pytest.raises(ConfigError, match="no dbtx platform found"):
        load_config(["build"])


def test_no_platform_is_an_error(
    lone_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(lone_project)
    with pytest.raises(ConfigError) as info:
        load_config(["build"])
    message = str(info.value)
    assert "--platform-dir" in message and "--standalone-mode" in message


@pytest.mark.parametrize("argv", [["--help"], ["build", "--help"], ["--version"], [], ["docs"]])
def test_help_works_without_a_platform(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    assert load_config(argv).mode == "standalone"


# --------------------------------------------------------------------------------------
# Standalone mode
# --------------------------------------------------------------------------------------


def test_standalone_mode_gates_everything_platform_level(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBT_PROFILES_DIR", "/somewhere/else")
    cfg = load_config(["build", "--standalone-mode", "--project-dir", "jaffle-shop"])
    assert cfg.mode == "standalone"
    assert cfg.platform_root is None
    assert cfg.config_file is None
    assert cfg.db_backend is None and cfg.db_uri is None and cfg.db_schema is None
    assert cfg.db_adapter is None
    assert cfg.backend_enabled is False
    assert cfg.uses_database is False
    # No injection and no profiles check: argv reaches dbt untouched.
    assert cfg.argv == ("build", "--project-dir", "jaffle-shop")
    # A relative --project-dir is relative to cwd, as dbt treats it.
    assert cfg.project_dir == platform / "jaffle-shop"


def test_standalone_mode_reads_no_config_file(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Broken config files at the root and in the project do not matter.
    layout.config_file(platform, "invalid_unknown_key.conf")
    layout.config_file(platform / "jaffle-shop", "invalid_missing_equals.conf")
    monkeypatch.chdir(platform / "jaffle-shop")
    assert load_config(["build", "--standalone-mode"]).mode == "standalone"


@pytest.mark.parametrize(
    "flags",
    [
        ["--platform-dir", "/p"],
        ["--db-uri", "sqlite://"],
        ["--db-backend", "enabled"],
        ["--db-schema", "dbtx"],
        ["--db-adapter", "sqlite"],
        ["--config-file", "x.conf"],
    ],
)
def test_standalone_mode_contradictions(flags: list[str]) -> None:
    with pytest.raises(ConfigError):
        load_config(["build", "--standalone-mode", *flags])


def test_standalone_mode_ignores_db_env_vars(
    lone_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(lone_project)
    monkeypatch.setenv("DBTX_DB_URI", "not even a url")
    monkeypatch.setenv("DBTX_DB_BACKEND", "bogus")
    monkeypatch.setenv("DBTX_DB_SCHEMA", "Not A Schema")
    monkeypatch.setenv("DBTX_DB_ADAPTER", "no_such_adapter")
    monkeypatch.setenv("DBTX_CONFIG_FILE", "/does/not/exist")
    cfg = load_config(["build", "--standalone-mode"])
    assert cfg.ignored_env_vars == (
        "DBTX_DB_BACKEND",
        "DBTX_DB_URI",
        "DBTX_DB_SCHEMA",
        "DBTX_DB_ADAPTER",
        "DBTX_CONFIG_FILE",
    )


# --------------------------------------------------------------------------------------
# Step 2 (platform): project dir and argv rewrite
# --------------------------------------------------------------------------------------


def test_relative_project_dir_resolves_against_the_root_from_any_cwd(
    platform: Path, layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch, fixed_run: None
) -> None:
    monkeypatch.chdir(layout.directory("elsewhere"))
    cfg = load_config(
        ["build", "--platform-dir", str(platform), "--project-dir=jaffle-shop", "-s", "x"]
    )
    project = platform / "jaffle-shop"
    assert cfg.project_dir == project
    assert cfg.argv == (
        "build", "-s", "x",
        "--project-dir", str(project), "--profiles-dir", str(project),
        "--target-path", str(cfg.run_folder), "--log-path", str(cfg.run_folder),
    )


def test_dbt_project_dir_env_is_resolved_against_the_root(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBT_PROJECT_DIR", "other-shop")
    cfg = load_config(["run"])
    assert cfg.project_dir == platform / "other-shop"
    assert "--project-dir" in cfg.argv


def test_project_found_by_walking_up_from_cwd(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = platform / "jaffle-shop" / "models"
    models.mkdir()
    monkeypatch.chdir(models)
    cfg = load_config(["run", "--platform-dir", str(platform)])
    assert cfg.project_dir == platform / "jaffle-shop"


def test_platform_root_without_a_project_lists_the_folders(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config(["build"])
    assert "choose a project with --project-dir" in str(info.value)
    assert "jaffle-shop, other-shop" in str(info.value)


def test_project_must_be_a_direct_child(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = layout.project(platform / "team", "nested", "nested")
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="not a direct child"):
        load_config(["build", "--project-dir", str(nested)])


def test_project_folder_needs_a_dbt_project(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout.directory("platform/not-a-project")
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config(["build", "--project-dir", "not-a-project"])
    assert str(info.value) == (
        f"{platform / 'not-a-project'} (from --project-dir) has no dbt_project.yml; "
        f"project folders: jaffle-shop, other-shop"
    )


def test_a_project_dir_that_does_not_exist(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config(["build", "--project-dir", "jafffle-shop"])
    assert str(info.value) == (
        f"{platform / 'jafffle-shop'} (from --project-dir) does not exist; project "
        f"folders: jaffle-shop, other-shop"
    )


def test_a_project_dir_from_dbt_project_dir_that_does_not_exist(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBT_PROJECT_DIR", "nowhere")
    with pytest.raises(ConfigError, match=r"nowhere \(from DBT_PROJECT_DIR\) does not exist"):
        load_config(["build"])


def test_a_project_dir_that_is_a_file(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (platform / "notes.txt").write_text("not a project")
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config(["build", "--project-dir", "notes.txt"])
    assert str(info.value) == (
        f"{platform / 'notes.txt'} (from --project-dir) is not a directory; project "
        f"folders: jaffle-shop, other-shop"
    )


def test_project_level_dbtx_conf_is_an_error_in_platform_mode(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout.config_file(platform / "jaffle-shop", "platform_sqlite.conf")
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="not allowed in platform mode"):
        load_config(["build", "--project-dir", "jaffle-shop"])


def test_commands_without_a_project_run_from_the_root(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(["docs", "serve", "--docs-loc", "target"])
    assert cfg.project_dir is None
    assert cfg.argv == ("docs", "serve", "--docs-loc", "target")
    assert load_config(["config"]).project_dir is None


def test_templates_gets_only_the_project_dir(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(["templates", "--project-dir", "jaffle-shop"])
    assert cfg.argv == ("templates", "--project-dir", str(platform / "jaffle-shop"))


def test_platform_mode_needs_the_command_first(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="command must come first"):
        load_config(["--debug", "build"])


def test_unknown_command_is_left_to_dbt(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    assert load_config(["no-such-command"]).argv == ("no-such-command",)


# --------------------------------------------------------------------------------------
# Profiles (plan 6.5.6)
# --------------------------------------------------------------------------------------


def test_profiles_dir_flag_elsewhere_is_rejected(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    elsewhere = layout.directory("elsewhere")
    with pytest.raises(ConfigError, match="--profiles-dir=.* conflicts with platform mode"):
        load_config(["build", "--project-dir", "jaffle-shop", "--profiles-dir", str(elsewhere)])


def test_profiles_dir_env_elsewhere_is_rejected(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBT_PROFILES_DIR", str(layout.directory("elsewhere")))
    with pytest.raises(ConfigError, match="DBT_PROFILES_DIR=.* conflicts"):
        load_config(["build", "--project-dir", "jaffle-shop"])


def test_profiles_dir_pointing_at_the_project_is_accepted(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    project = platform / "jaffle-shop"
    monkeypatch.setenv("DBT_PROFILES_DIR", str(project))
    cfg = load_config(["build", "--project-dir", "jaffle-shop", "--profiles-dir", "jaffle-shop"])
    assert cfg.argv.count("--profiles-dir") == 1
    assert cfg.argv[cfg.argv.index("--profiles-dir") + 1] == str(project)


def test_profile_and_target_reach_dbt_unchanged(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(["run", "--project-dir", "jaffle-shop", "--profile", "x", "--target", "prod"])
    assert cfg.argv[:5] == ("run", "--profile", "x", "--target", "prod")


def test_missing_profiles_yml_is_left_to_dbt(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="platform_sqlite.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop", with_profiles=False)
    monkeypatch.chdir(root)
    load_config(["build", "--project-dir", "jaffle-shop"])  # no ConfigError


# --------------------------------------------------------------------------------------
# Which config file is used
# --------------------------------------------------------------------------------------

BUILD = ["build", "--project-dir", "jaffle-shop"]


def test_platform_root_file_by_default(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert cfg.config_file == platform / "dbtx.conf"
    assert cfg.config_file_reason == "platform root"


def test_env_config_file_beats_the_platform_file(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = layout.config_file(layout.directory("ci"), "platform_postgres.conf", "env.conf")
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_CONFIG_FILE", str(env_file))
    cfg = load_config(BUILD)
    assert cfg.config_file == env_file
    assert cfg.config_file_reason == "DBTX_CONFIG_FILE"


def test_cli_config_file_beats_env_and_is_never_merged(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci = layout.directory("ci")
    env_file = layout.config_file(ci, "platform_postgres.conf", "env.conf")
    cli_file = layout.config_file(ci, "backend_disabled.conf", "cli.conf")
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_CONFIG_FILE", str(env_file))
    cfg = load_config([*BUILD, "--config-file", str(cli_file)])
    assert cfg.config_file == cli_file
    # The platform file and the env file both set DB_URI; neither is merged in.
    assert cfg.db_uri.value is None


def test_explicit_config_file_flag_must_exist(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="does not exist"):
        load_config([*BUILD, "--config-file", "nope.conf"])


def test_explicit_config_file_env_must_exist(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_CONFIG_FILE", "nope.conf")
    with pytest.raises(ConfigError, match="DBTX_CONFIG_FILE=nope.conf"):
        load_config(BUILD)


def test_explicit_config_file_replaces_a_missing_platform_file(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample=None)
    layout.project(root, "jaffle-shop", "jaffle_shop")
    conf = layout.config_file(layout.directory("ci"), "backend_disabled.conf", "ci.conf")
    monkeypatch.chdir(layout.base)
    cfg = load_config(
        ["build", "--platform-dir", str(root), "--project-dir", "jaffle-shop", "--config-file", str(conf)]
    )
    assert cfg.backend_enabled is False


def test_dotenv_is_never_read(layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    shutil.copyfile(config_sample("decoy.env"), root / ".env")
    monkeypatch.chdir(root)
    assert load_config(["deps", "--project-dir", "jaffle-shop"]).db_uri.value is None


# --------------------------------------------------------------------------------------
# Steps 3-4: precedence and sources
# --------------------------------------------------------------------------------------


def test_db_uri_from_the_cli_beats_file_and_env(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_URI", "sqlite:////env.db")
    cfg = load_config([*BUILD, "--db-uri", "sqlite:////cli.db"])
    assert (cfg.db_uri.value, cfg.db_uri.source) == ("sqlite:////cli.db", "cli")


def test_db_uri_from_the_file_beats_env(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_URI", "sqlite:////env.db")
    cfg = load_config([*BUILD, "--log-root-location", "runs"])
    assert cfg.db_uri.source == f"file {platform / 'dbtx.conf'}:1"


def test_db_uri_from_env_when_the_file_has_none(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_URI", "sqlite:////env.db")
    cfg = load_config([*BUILD, "--log-root-location", "runs"])
    assert (cfg.db_uri.value, cfg.db_uri.source) == ("sqlite:////env.db", "env DBTX_DB_URI")


def test_db_backend_default_is_enabled(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert (cfg.db_backend.value, cfg.db_backend.source) == ("enabled", "default")


def test_db_backend_from_env(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_BACKEND", "disabled")
    cfg = load_config(BUILD)
    assert (cfg.db_backend.value, cfg.db_backend.source) == ("disabled", "env DBTX_DB_BACKEND")


def test_db_backend_from_the_file_beats_env(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")  # DB_BACKEND=enabled
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_BACKEND", "disabled")
    cfg = load_config(["config"])
    assert cfg.db_backend.value == "enabled"
    assert cfg.db_backend.source == f"file {root / 'dbtx.conf'}:2"


def test_db_backend_from_the_cli_beats_everything(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_BACKEND", "enabled")
    cfg = load_config([*BUILD, "--db-backend", "disabled", "--log-root-location", "runs"])
    assert (cfg.db_backend.value, cfg.db_backend.source) == ("disabled", "cli")


def test_unknown_dbtx_env_vars_warn(layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    monkeypatch.setenv("DBTX_DB_URL", "x")
    monkeypatch.setenv("DBTX_DB_STRICT", "1")
    monkeypatch.setenv("DBTX_DB_SCHEMA", "dbtx")
    cfg = load_config(["--help"])
    assert [w for w in cfg.warnings if "unknown environment variable" in w] == [
        "unknown environment variable DBTX_DB_URL is ignored"
    ]


# --------------------------------------------------------------------------------------
# Step 5: values
# --------------------------------------------------------------------------------------


def test_db_backend_is_case_insensitive(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    assert load_config([*BUILD, "--db-backend", "DISABLED"]).db_backend.value == "disabled"


def test_db_backend_rejects_other_values(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=r"DB_BACKEND \(cli\): 'maybe'"):
        load_config([*BUILD, "--db-backend", "maybe"])


def test_db_backend_value_from_a_file_names_the_line(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="bad_backend_value.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    with pytest.raises(ConfigError, match=r"DB_BACKEND \(file .*dbtx.conf:1\): 'sometimes'"):
        load_config(BUILD)


@pytest.mark.parametrize(
    "uri, error",
    [
        ("not a url", "not a valid SQLAlchemy URL"),
        ("mysql://h/db", "unsupported backend 'mysql'"),
        ("postgresql+psycopg2://h/db", "unsupported Postgres driver 'psycopg2'"),
        ("sqlite+pysqlcipher:///x.db", "unsupported SQLite driver"),
    ],
)
def test_invalid_db_uri(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
    error: str,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=error):
        load_config([*BUILD, "--db-uri", uri])


def test_malformed_uri_is_rejected_even_when_disabled(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="not a valid SQLAlchemy URL"):
        load_config([*BUILD, "--db-backend", "disabled", "--db-uri", "nope"])


@pytest.fixture
def psycopg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """psycopg is not installed (as far as dbtx can tell)."""
    real_find_spec = config.importlib.util.find_spec
    monkeypatch.setattr(
        config.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "psycopg" else real_find_spec(name, *a),
    )


def test_missing_psycopg_is_an_error(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    psycopg_missing: None,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=r"install dbtx\[postgres\]"):
        load_config([*BUILD, "--db-uri", "postgresql+psycopg://u@h/db"])


def test_a_disabled_backend_never_needs_its_driver(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    psycopg_missing: None,
) -> None:
    monkeypatch.chdir(platform)
    load_config([*BUILD, "--db-uri", "postgresql+psycopg://u@h/db", "--db-backend", "disabled"])


def test_password_is_masked(layout: LayoutBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    root = layout.platform_root(sample="platform_postgres.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    assert load_config(BUILD).masked_db_uri() == "postgresql+psycopg://dbtx:***@db/dbtx"


# --------------------------------------------------------------------------------------
# Step 5: the backend and driver come from the adapter registry (plugins included)
# --------------------------------------------------------------------------------------

FAKE_ADAPTERS = "tests.unit.fake_store_adapters"


def test_a_plugin_backend_is_accepted(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbAdapter"})
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", "fakedb+fakedriver://h/db"])
    assert cfg.db_uri.value == "fakedb+fakedriver://h/db"


def test_unsupported_backend_lists_the_plugins(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbAdapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "mysql://h/db"])
    assert str(info.value) == (
        "DB_URI (cli): unsupported backend 'mysql'; supported: sqlite, postgresql, fakedb"
    )


def test_a_plugin_driver_is_checked(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbAdapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "fakedb+otherdriver://h/db"])
    assert str(info.value) == (
        "DB_URI (cli): unsupported FakeDB driver 'otherdriver'; use fakedb+fakedriver://"
    )


def test_a_plugin_driver_package_must_be_installed(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbWithPackageAdapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "fakedb+fakedriver://h/db"])
    assert str(info.value) == (
        "DB_URI (cli) is a FakeDB URL but fakedb_driver_that_is_not_installed is not "
        "installed; install fakedb-driver"
    )


def test_a_plugin_uri_without_a_driver_and_no_dialect(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    # No `+driver`: SQLAlchemy would load the `fakedb` dialect to find the default driver,
    # and there is none.
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbAdapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "fakedb://h/db"])
    assert str(info.value) == (
        "DB_URI (cli): the FakeDB URI names no driver and SQLAlchemy has no 'fakedb' "
        "dialect installed to pick one; use fakedb+fakedriver://"
    )


def test_a_plugin_uri_without_a_driver_and_no_driver_package(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    # The missing dialect usually ships with the driver package: say how to install it.
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbWithPackageAdapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "fakedb://h/db"])
    assert str(info.value) == (
        "DB_URI (cli) is a FakeDB URL but fakedb_driver_that_is_not_installed is not "
        "installed; install fakedb-driver"
    )


def test_a_plugin_uri_without_a_driver_when_the_backend_is_disabled(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    # Disabled: the driver package is not checked, so the driver message is the one.
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbWithPackageAdapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-backend", "disabled", "--db-uri", "fakedb://h/db"])
    assert str(info.value) == (
        "DB_URI (cli): the FakeDB URI names no driver and SQLAlchemy has no 'fakedb' "
        "dialect installed to pick one; use fakedb+fakedriver://"
    )


def test_other_errors_finding_the_driver_are_not_caught(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbAdapter"})
    monkeypatch.chdir(platform)

    def broken_dialect(self: sa.engine.URL) -> NoReturn:
        raise RuntimeError("a bug in the dialect")

    monkeypatch.setattr(sa.engine.URL, "get_dialect", broken_dialect)
    with pytest.raises(RuntimeError, match="a bug in the dialect"):
        load_config([*BUILD, "--db-uri", "fakedb://h/db"])


def test_a_broken_plugin_is_a_config_error(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": "tests.unit.no_such_module:Adapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=r"^DB_URI \(cli\): store adapter plugin 'fakedb' .*could not be loaded"):
        load_config([*BUILD, "--db-uri", "fakedb+fakedriver://h/db"])


# --------------------------------------------------------------------------------------
# DB_SCHEMA (plan 6.4, added in Phase 2)
# --------------------------------------------------------------------------------------

POSTGRES_URI = "postgresql+psycopg://dbtx@db/dbtx"


@pytest.fixture
def postgres_schema_platform(layout: LayoutBuilder) -> Path:
    """A platform whose dbtx.conf sets a Postgres DB_URI and DB_SCHEMA=analytics_dbtx."""
    root = layout.platform_root(sample="platform_postgres_schema.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    return root


def test_db_schema_default_is_dbtx(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", POSTGRES_URI])
    assert (cfg.db_schema.value, cfg.db_schema.source) == ("dbtx", "default")


def test_db_schema_from_the_file(
    postgres_schema_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(postgres_schema_platform)
    cfg = load_config(BUILD)
    assert (cfg.db_schema.value, cfg.db_schema.source) == (
        "analytics_dbtx",
        f"file {postgres_schema_platform / 'dbtx.conf'}:2",
    )


def test_db_schema_from_the_file_beats_env(
    postgres_schema_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(postgres_schema_platform)
    monkeypatch.setenv("DBTX_DB_SCHEMA", "from_env")
    assert load_config(BUILD).db_schema.value == "analytics_dbtx"


def test_db_schema_from_env(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_SCHEMA", "from_env")
    cfg = load_config([*BUILD, "--db-uri", POSTGRES_URI])
    assert (cfg.db_schema.value, cfg.db_schema.source) == ("from_env", "env DBTX_DB_SCHEMA")


def test_db_schema_from_the_cli_beats_file_and_env(
    postgres_schema_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(postgres_schema_platform)
    monkeypatch.setenv("DBTX_DB_SCHEMA", "from_env")
    cfg = load_config([*BUILD, "--db-schema=from_cli"])
    assert (cfg.db_schema.value, cfg.db_schema.source) == ("from_cli", "cli")


@pytest.mark.parametrize("schema", ["a", "_private", "dbtx_2", "a" * 63])
def test_valid_db_schema_names(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: str,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", POSTGRES_URI, "--db-schema", schema])
    assert cfg.db_schema.value == schema


@pytest.mark.parametrize(
    "schema", ["Analytics", "1abc", "has-dash", "has space", '"quoted"', "a" * 64]
)
def test_invalid_db_schema_names(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: str,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", POSTGRES_URI, "--db-schema", schema])
    assert str(info.value) == (
        f"DB_SCHEMA (cli): {schema!r} is not a valid schema name; use lowercase letters, "
        f"digits and _, not starting with a digit, at most 63 characters"
    )


def test_db_schema_value_from_a_file_names_the_line(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="bad_schema_value.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    with pytest.raises(ConfigError, match=r"DB_SCHEMA \(file .*dbtx.conf:2\): 'Analytics-DBTX'"):
        load_config(BUILD)


def test_db_schema_with_a_sqlite_uri_is_an_error(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-schema", "dbtx"])
    assert str(info.value) == (
        "DB_SCHEMA (cli): SQLite has no schemas; remove DB_SCHEMA, or use a DB_URI "
        "whose backend has schemas"
    )


def test_db_schema_from_a_file_with_a_sqlite_uri(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="sqlite_with_schema.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    with pytest.raises(ConfigError, match=r"^DB_SCHEMA \(file .*dbtx.conf:3\): SQLite has no schemas"):
        load_config(BUILD)


def test_db_schema_with_a_sqlite_uri_is_an_error_even_when_disabled(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="SQLite has no schemas"):
        load_config([*BUILD, "--db-schema", "dbtx", "--db-backend", "disabled"])


def test_db_schema_with_a_plugin_backend_without_schemas(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": f"{FAKE_ADAPTERS}:FakeDbAdapter"})
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=r"^DB_SCHEMA \(cli\): FakeDB has no schemas"):
        load_config([*BUILD, "--db-uri", "fakedb+fakedriver://h/db", "--db-schema", "x"])


def test_the_default_db_schema_is_fine_with_sqlite(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert (cfg.db_schema.value, cfg.db_schema.source) == ("dbtx", "default")


def test_db_schema_without_a_uri_is_accepted(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    assert load_config(["config", "--db-schema", "x"]).db_schema.value == "x"


# --------------------------------------------------------------------------------------
# Step 6: requirement check and command classes
# --------------------------------------------------------------------------------------


@pytest.fixture
def platform_without_uri(layout: LayoutBuilder) -> Path:
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    return root


def test_missing_uri_for_a_database_command(
    platform_without_uri: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform_without_uri)
    with pytest.raises(ConfigError) as info:
        load_config(BUILD)
    message = str(info.value)
    assert "needs a database" in message
    assert "--db-backend disabled" in message and "--standalone-mode" in message


@pytest.mark.parametrize(
    "argv",
    [
        [*BUILD, "--db-backend", "disabled", "--log-root-location", "runs"],
        ["deps", "--project-dir", "jaffle-shop"],
        ["debug", "--project-dir", "jaffle-shop"],
        ["config"],
        ["db", "status"],
    ],
)
def test_missing_uri_is_fine_when_disabled_or_unused(
    platform_without_uri: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.chdir(platform_without_uri)
    load_config(argv)


def test_docs_serve_uses_the_database(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It serves the run history from the database (plan Phase 7)."""
    monkeypatch.chdir(platform)
    assert "docs serve" in config.COMMANDS_THAT_USE_THE_DATABASE
    assert load_config(["docs", "serve", "--project-dir", "jaffle-shop"]).uses_database is True


@pytest.mark.parametrize("command", config.COMMANDS_THAT_USE_THE_DATABASE)
def test_commands_that_use_the_database(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.chdir(platform)
    argv = [*command.split(), "--project-dir", "jaffle-shop"]
    assert load_config(argv).uses_database is True


@pytest.mark.parametrize(
    "command",
    [
        "config", "db status", "ls", "list", "debug", "deps", "clean", "show",
        "retry", "clone", "docs patch", "templates",
    ],
)
def test_commands_that_do_not_require_the_database(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.chdir(platform)
    argv = [*command.split(), "--project-dir", "jaffle-shop"]
    assert load_config(argv).uses_database is False


# --------------------------------------------------------------------------------------
# Step 7: SQLite paths
# --------------------------------------------------------------------------------------


def test_relative_sqlite_path_from_the_file_is_relative_to_the_file(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(layout.directory("elsewhere"))
    cfg = load_config(["build", "--platform-dir", str(platform), "--project-dir", "jaffle-shop"])
    assert cfg.db_uri.value == f"sqlite:///{platform}/.dbtx/dbtx.db"


def test_relative_sqlite_path_from_the_cli_is_relative_to_cwd(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform / "jaffle-shop")
    cfg = load_config(["build", "--platform-dir", str(platform), "--db-uri", "sqlite:///here.db"])
    assert cfg.db_uri.value == f"sqlite:///{platform}/jaffle-shop/here.db"


@pytest.mark.parametrize("uri", ["sqlite:////abs/x.db", "sqlite://", "sqlite:///:memory:"])
def test_other_sqlite_paths_are_unchanged(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    monkeypatch.chdir(platform)
    assert load_config([*BUILD, "--db-uri", uri]).db_uri.value == uri


# --------------------------------------------------------------------------------------
# Step 8: guard G1
# --------------------------------------------------------------------------------------


@pytest.fixture
def colliding_platform(platform: Path, layout: LayoutBuilder) -> Path:
    """The standard platform plus jaffle-shop-2, which reuses the name jaffle_shop."""
    layout.project(platform, "jaffle-shop-2", "jaffle_shop")
    return platform


@pytest.mark.parametrize("folder", ["jaffle-shop", "jaffle-shop-2"])
def test_g1_collision_with_the_current_project_is_recorded(
    colliding_platform: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    # Not decided here: the established owner in the database may run (guard G2), so
    # the start-up checks decide (test_store_startup.py).
    monkeypatch.chdir(colliding_platform)
    cfg = load_config(["build", "--project-dir", folder])
    assert cfg.project_name == "jaffle_shop"
    assert cfg.colliding_folders == ("jaffle-shop", "jaffle-shop-2")
    assert not any("declared by" in w for w in cfg.warnings)


def test_g1_records_the_project_name_without_a_collision(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert (cfg.project_name, cfg.colliding_folders) == ("jaffle_shop", ())


def test_g1_unreadable_name_is_left_to_dbt(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout.project(platform, "nameless", "unused", dbt_project_text="profile: x\n")
    monkeypatch.chdir(platform)
    cfg = load_config(["build", "--project-dir", "nameless"])
    assert (cfg.project_name, cfg.colliding_folders) == (None, ())


def test_g1_unrelated_collision_is_a_warning(
    colliding_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(colliding_platform)
    cfg = load_config(["build", "--project-dir", "other-shop"])
    assert any("'jaffle_shop' is declared by 2 folders" in w for w in cfg.warnings)
    assert (cfg.project_name, cfg.colliding_folders) == ("other_shop", ())


@pytest.mark.parametrize(
    "argv", [[*BUILD, "--db-backend", "disabled"], ["deps", "--project-dir", "jaffle-shop"]]
)
def test_g1_needs_the_backend_and_a_database_command(
    colliding_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.chdir(colliding_platform)
    cfg = load_config(argv)
    assert (cfg.project_name, cfg.colliding_folders) == (None, ())


def test_g1_ignores_hidden_and_nested_folders(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout.project(platform, ".backup", "jaffle_shop")
    layout.project(platform / "other-shop" / "dbt_packages", "copy", "jaffle_shop")
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert not any("declared by" in w for w in cfg.warnings)


def test_g1_renders_a_jinja_name(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout.project(
        platform,
        "jaffle-shop-2",
        "unused",
        dbt_project_text="name: \"{{ env_var('DBTX_TEST_UNSET_NAME', 'jaffle_shop') }}\"\n",
    )
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert cfg.colliding_folders == ("jaffle-shop", "jaffle-shop-2")


# --------------------------------------------------------------------------------------
# DB_ADAPTER (added in Phase 2): the store adapter, when not the default for DB_URI
# --------------------------------------------------------------------------------------

@pytest.fixture
def custom_sqlite(adapter_plugins: Callable[[dict[str, str]], None]) -> None:
    """The `custom_sqlite` plugin: SQLite through pysqlite or pysqlcipher."""
    adapter_plugins({"custom_sqlite": f"{FAKE_ADAPTERS}:CustomSqliteAdapter"})


def test_db_adapter_default_is_unset(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert (cfg.db_adapter.value, cfg.db_adapter.source) == (None, "default")


def test_db_adapter_from_the_file(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    root = layout.platform_root(sample="platform_sqlite_adapter.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_ADAPTER", "sqlite")  # the file beats the env
    cfg = load_config(BUILD)
    assert (cfg.db_adapter.value, cfg.db_adapter.source) == (
        "custom_sqlite",
        f"file {root / 'dbtx.conf'}:2",
    )


def test_db_adapter_from_env(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_ADAPTER", "custom_sqlite")
    cfg = load_config(BUILD)
    assert (cfg.db_adapter.value, cfg.db_adapter.source) == ("custom_sqlite", "env DBTX_DB_ADAPTER")


def test_db_adapter_from_the_cli_beats_file_and_env(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    root = layout.platform_root(sample="platform_sqlite_adapter.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_ADAPTER", "custom_sqlite")
    cfg = load_config([*BUILD, "--db-adapter=sqlite"])
    assert (cfg.db_adapter.value, cfg.db_adapter.source) == ("sqlite", "cli")


# The matrix: which adapter serves DB_URI, with and without DB_ADAPTER.


@pytest.mark.parametrize("uri", ["sqlite:///x.db", "sqlite+pysqlite:///x.db"])
def test_sqlite_uri_without_db_adapter_is_the_builtin(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
    uri: str,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", uri])
    assert cfg.resolved_adapter() == ("sqlite", SqliteAdapter)


def test_pysqlcipher_uri_without_db_adapter_is_the_plugin(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", "sqlite+pysqlcipher:///x.db"])
    assert cfg.resolved_adapter() == ("custom_sqlite", CustomSqliteAdapter)


def test_db_adapter_names_the_builtin(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", "sqlite:///x.db", "--db-adapter", "sqlite"])
    assert cfg.resolved_adapter() == ("sqlite", SqliteAdapter)


def test_db_adapter_names_a_plugin_for_a_builtin_pair(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", "sqlite:///x.db", "--db-adapter", "custom_sqlite"])
    assert cfg.resolved_adapter() == ("custom_sqlite", CustomSqliteAdapter)


def test_db_adapter_that_does_not_support_the_driver(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "sqlite+pysqlcipher:///x.db", "--db-adapter", "sqlite"])
    assert str(info.value) == (
        "DB_ADAPTER (cli): store adapter 'sqlite' does not support driver 'pysqlcipher'; "
        "it supports: pysqlite"
    )


def test_db_adapter_for_another_dialect(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-adapter", "postgresql"])
    assert str(info.value) == (
        "DB_ADAPTER (cli): store adapter 'postgresql' serves postgresql, not sqlite"
    )


def test_unknown_db_adapter(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-adapter", "nope"])
    assert str(info.value) == (
        "DB_ADAPTER (cli): unknown store adapter 'nope'; available: sqlite, postgresql, "
        "custom_sqlite"
    )


def test_unknown_db_adapter_without_a_uri(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A typo is caught even before DB_URI is set.
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    with pytest.raises(ConfigError, match=r"^DB_ADAPTER \(cli\): unknown store adapter 'nope'"):
        load_config([*BUILD, "--db-adapter", "nope"])


def test_db_adapter_from_a_file_names_the_line(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # custom_sqlite is not installed here.
    root = layout.platform_root(sample="platform_sqlite_adapter.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    with pytest.raises(ConfigError, match=r"^DB_ADAPTER \(file .*dbtx.conf:2\): unknown store adapter"):
        load_config(BUILD)


def test_several_plugins_for_the_pair_need_db_adapter(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins(
        {
            "custom_sqlite": f"{FAKE_ADAPTERS}:CustomSqliteAdapter",
            "other_sqlite": f"{FAKE_ADAPTERS}:OtherSqliteAdapter",
        }
    )
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "sqlite+pysqlcipher:///x.db"])
    assert str(info.value) == (
        "DB_URI (cli): several store adapters serve sqlite+pysqlcipher: custom_sqlite, "
        "other_sqlite; choose one with DB_ADAPTER"
    )
    cfg = load_config(
        [*BUILD, "--db-uri", "sqlite+pysqlcipher:///x.db", "--db-adapter", "other_sqlite"]
    )
    assert cfg.resolved_adapter()[0] == "other_sqlite"


def test_the_package_for_the_uri_driver_must_be_installed(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE_ADAPTERS}:CustomSqliteMissingPackageAdapter"})
    monkeypatch.chdir(platform)
    # pysqlite needs no package...
    load_config([*BUILD, "--db-uri", "sqlite:///x.db", "--db-adapter", "custom_sqlite"])
    # ...pysqlcipher does.
    with pytest.raises(ConfigError) as info:
        load_config([*BUILD, "--db-uri", "sqlite+pysqlcipher:///x.db"])
    assert str(info.value) == (
        "DB_URI (cli) is a SQLite (custom) URL but sqlcipher3_that_is_not_installed is "
        "not installed; install custom-sqlite[cipher]"
    )


def test_db_schema_is_checked_against_the_resolved_adapter(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_sqlite: None,
) -> None:
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=r"^DB_SCHEMA \(cli\): SQLite \(custom\) has no schemas"):
        load_config(
            [*BUILD, "--db-uri", "sqlite:///x.db", "--db-adapter", "custom_sqlite", "--db-schema", "x"]
        )


def test_resolved_adapter_without_a_uri(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    assert load_config(["config"]).resolved_adapter() is None


def test_resolved_adapter_for_postgres(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    cfg = load_config([*BUILD, "--db-uri", "postgresql+psycopg://h/db", "--db-backend", "disabled"])
    assert cfg.resolved_adapter() == ("postgresql", PostgresAdapter)
