"""dbtx.config: modes, layers, validation and guard G1 (plan section 6, Phase 0).

Every test builds its own clean directory structure with the `layout` / `platform` /
`lone_project` fixtures (tests/unit/conftest.py) and uses checked-in sample config
files. The parser itself is tested in test_config_file.py.
"""

from __future__ import annotations

import shutil

import pytest

from dbtx import config
from dbtx.config import ConfigError, load_config
from tests.unit.conftest import config_sample


# --------------------------------------------------------------------------------------
# Step 1: argv extraction (pure functions: no state needed)
# --------------------------------------------------------------------------------------


def test_extracts_value_flags_in_both_forms():
    values, standalone, rest = config.extract_dbtx_flags(
        ["build", "--db-uri", "sqlite://", "-s", "x", "--db-backend=disabled"]
    )
    assert values == {"--db-uri": "sqlite://", "--db-backend": "disabled"}
    assert standalone is False
    assert rest == ["build", "-s", "x"]


def test_extracts_flags_before_the_command_too():
    values, standalone, rest = config.extract_dbtx_flags(
        ["--platform-dir", "/p", "--standalone-mode", "build"]
    )
    assert values == {"--platform-dir": "/p"}
    assert standalone is True
    assert rest == ["build"]


@pytest.mark.parametrize(
    "argv",
    [["build", "--db-uri"], ["build", "--db-uri", "--select", "x"], ["build", "--db-uri="]],
)
def test_flag_without_a_value_is_an_error(argv):
    with pytest.raises(ConfigError, match="--db-uri needs a value"):
        config.extract_dbtx_flags(argv)


def test_flag_given_twice_is_an_error():
    with pytest.raises(ConfigError, match="more than once"):
        config.extract_dbtx_flags(["--db-uri", "a", "--db-uri", "b"])


def test_standalone_mode_takes_no_value():
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
def test_command_name(argv, command):
    assert config.command_name(argv) == command


# --------------------------------------------------------------------------------------
# Step 2: mode
# --------------------------------------------------------------------------------------


def test_cwd_holding_dbtx_conf_is_the_platform(platform, monkeypatch):
    monkeypatch.chdir(platform)
    cfg = load_config(["build", "--project-dir", "jaffle-shop"])
    assert cfg.mode == "platform"
    assert cfg.platform_root == platform
    assert "current directory" in cfg.mode_reason


def test_platform_dir_flag(platform, layout, monkeypatch):
    monkeypatch.chdir(layout.directory("elsewhere"))
    cfg = load_config(["build", "--platform-dir", str(platform), "--project-dir", "jaffle-shop"])
    assert cfg.mode == "platform"
    assert cfg.platform_root == platform
    assert cfg.mode_reason == "--platform-dir"


def test_platform_dir_must_exist(layout):
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(["build", "--platform-dir", str(layout.base / "nope")])


def test_platform_dir_must_be_a_directory(layout):
    a_file = layout.base / "a-file"
    a_file.write_text("")
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(["build", "--platform-dir", str(a_file)])


def test_platform_dir_must_not_be_a_project(platform):
    with pytest.raises(ConfigError, match="is a dbt project"):
        load_config(["build", "--platform-dir", str(platform / "jaffle-shop")])


def test_platform_dir_needs_a_dbtx_conf(layout):
    root = layout.platform_root(sample=None)
    layout.project(root, "jaffle-shop", "jaffle_shop")
    with pytest.raises(ConfigError, match="has no dbtx.conf"):
        load_config(["build", "--platform-dir", str(root), "--project-dir", "jaffle-shop"])


def test_project_folder_with_dbtx_conf_is_never_a_platform_root(
    lone_project, layout, monkeypatch, enforce
):
    layout.config_file(lone_project, "platform_sqlite.conf")
    monkeypatch.chdir(lone_project)
    with pytest.raises(ConfigError, match="is never a platform root"):
        load_config(["build"])


def test_no_parent_directory_search(platform, monkeypatch, enforce):
    # cwd is a project folder whose parent is a platform root: still no platform.
    monkeypatch.chdir(platform / "jaffle-shop")
    with pytest.raises(ConfigError, match="no dbtx platform found"):
        load_config(["build"])


def test_no_platform_is_an_error_once_enforced(lone_project, monkeypatch, enforce):
    monkeypatch.chdir(lone_project)
    with pytest.raises(ConfigError) as info:
        load_config(["build"])
    message = str(info.value)
    assert "--platform-dir" in message and "--standalone-mode" in message


def test_no_platform_falls_back_to_standalone_until_enforced(lone_project, monkeypatch):
    monkeypatch.chdir(lone_project)
    cfg = load_config(["build", "-s", "x"])
    assert cfg.mode == "standalone"
    assert cfg.argv == ("build", "-s", "x")
    assert any("no dbtx platform found" in w for w in cfg.warnings)


@pytest.mark.parametrize("argv", [["--help"], ["build", "--help"], ["--version"], []])
def test_help_works_without_a_platform(layout, monkeypatch, enforce, argv):
    monkeypatch.chdir(layout.directory("empty"))
    assert load_config(argv).mode == "standalone"


# --------------------------------------------------------------------------------------
# Standalone mode
# --------------------------------------------------------------------------------------


def test_standalone_mode_gates_everything_platform_level(platform, monkeypatch):
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBT_PROFILES_DIR", "/somewhere/else")
    cfg = load_config(["build", "--standalone-mode", "--project-dir", "jaffle-shop"])
    assert cfg.mode == "standalone"
    assert cfg.platform_root is None
    assert cfg.config_file is None
    assert cfg.db_backend is None and cfg.db_uri is None
    assert cfg.backend_enabled is False
    assert cfg.uses_database is False
    # No injection and no profiles check: argv reaches dbt untouched.
    assert cfg.argv == ("build", "--project-dir", "jaffle-shop")
    # A relative --project-dir is relative to cwd, as dbt treats it.
    assert cfg.project_dir == platform / "jaffle-shop"


def test_standalone_mode_reads_no_config_file(platform, layout, monkeypatch):
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
        ["--config-file", "x.conf"],
    ],
)
def test_standalone_mode_contradictions(flags):
    with pytest.raises(ConfigError):
        load_config(["build", "--standalone-mode", *flags])


def test_standalone_mode_ignores_db_env_vars(lone_project, monkeypatch):
    monkeypatch.chdir(lone_project)
    monkeypatch.setenv("DBTX_DB_URI", "not even a url")
    monkeypatch.setenv("DBTX_DB_BACKEND", "bogus")
    monkeypatch.setenv("DBTX_CONFIG_FILE", "/does/not/exist")
    cfg = load_config(["build", "--standalone-mode"])
    assert cfg.ignored_env_vars == ("DBTX_DB_BACKEND", "DBTX_DB_URI", "DBTX_CONFIG_FILE")


# --------------------------------------------------------------------------------------
# Step 2 (platform): project dir and argv rewrite
# --------------------------------------------------------------------------------------


def test_relative_project_dir_resolves_against_the_root_from_any_cwd(
    platform, layout, monkeypatch
):
    monkeypatch.chdir(layout.directory("elsewhere"))
    cfg = load_config(
        ["build", "--platform-dir", str(platform), "--project-dir=jaffle-shop", "-s", "x"]
    )
    project = platform / "jaffle-shop"
    assert cfg.project_dir == project
    assert cfg.argv == (
        "build", "-s", "x",
        "--project-dir", str(project), "--profiles-dir", str(project),
    )


def test_dbt_project_dir_env_is_resolved_against_the_root(platform, monkeypatch):
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBT_PROJECT_DIR", "other-shop")
    cfg = load_config(["run"])
    assert cfg.project_dir == platform / "other-shop"
    assert "--project-dir" in cfg.argv


def test_project_found_by_walking_up_from_cwd(platform, monkeypatch):
    models = platform / "jaffle-shop" / "models"
    models.mkdir()
    monkeypatch.chdir(models)
    cfg = load_config(["run", "--platform-dir", str(platform)])
    assert cfg.project_dir == platform / "jaffle-shop"


def test_platform_root_without_a_project_lists_the_folders(platform, monkeypatch):
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError) as info:
        load_config(["build"])
    assert "choose a project with --project-dir" in str(info.value)
    assert "jaffle-shop, other-shop" in str(info.value)


def test_project_must_be_a_direct_child(platform, layout, monkeypatch):
    nested = layout.project(platform / "team", "nested", "nested")
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="not a direct child"):
        load_config(["build", "--project-dir", str(nested)])


def test_project_folder_needs_a_dbt_project(platform, layout, monkeypatch):
    layout.directory("platform/not-a-project")
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="has no dbt_project.yml"):
        load_config(["build", "--project-dir", "not-a-project"])


def test_project_level_dbtx_conf_is_an_error_in_platform_mode(platform, layout, monkeypatch):
    layout.config_file(platform / "jaffle-shop", "platform_sqlite.conf")
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="not allowed in platform mode"):
        load_config(["build", "--project-dir", "jaffle-shop"])


def test_commands_without_a_project_run_from_the_root(platform, monkeypatch):
    monkeypatch.chdir(platform)
    cfg = load_config(["docs", "serve", "--docs-loc", "target"])
    assert cfg.project_dir is None
    assert cfg.argv == ("docs", "serve", "--docs-loc", "target")
    assert load_config(["config"]).project_dir is None


def test_templates_gets_only_the_project_dir(platform, monkeypatch):
    monkeypatch.chdir(platform)
    cfg = load_config(["templates", "--project-dir", "jaffle-shop"])
    assert cfg.argv == ("templates", "--project-dir", str(platform / "jaffle-shop"))


def test_platform_mode_needs_the_command_first(platform, monkeypatch):
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="command must come first"):
        load_config(["--debug", "build"])


def test_unknown_command_is_left_to_dbt(platform, monkeypatch):
    monkeypatch.chdir(platform)
    assert load_config(["no-such-command"]).argv == ("no-such-command",)


# --------------------------------------------------------------------------------------
# Profiles (plan 6.5.6)
# --------------------------------------------------------------------------------------


def test_profiles_dir_flag_elsewhere_is_rejected(platform, layout, monkeypatch):
    monkeypatch.chdir(platform)
    elsewhere = layout.directory("elsewhere")
    with pytest.raises(ConfigError, match="--profiles-dir=.* conflicts with platform mode"):
        load_config(["build", "--project-dir", "jaffle-shop", "--profiles-dir", str(elsewhere)])


def test_profiles_dir_env_elsewhere_is_rejected(platform, layout, monkeypatch):
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBT_PROFILES_DIR", str(layout.directory("elsewhere")))
    with pytest.raises(ConfigError, match="DBT_PROFILES_DIR=.* conflicts"):
        load_config(["build", "--project-dir", "jaffle-shop"])


def test_profiles_dir_pointing_at_the_project_is_accepted(platform, monkeypatch):
    monkeypatch.chdir(platform)
    project = platform / "jaffle-shop"
    monkeypatch.setenv("DBT_PROFILES_DIR", str(project))
    cfg = load_config(["build", "--project-dir", "jaffle-shop", "--profiles-dir", "jaffle-shop"])
    assert cfg.argv.count("--profiles-dir") == 1
    assert cfg.argv[-1] == str(project)


def test_profile_and_target_reach_dbt_unchanged(platform, monkeypatch):
    monkeypatch.chdir(platform)
    cfg = load_config(["run", "--project-dir", "jaffle-shop", "--profile", "x", "--target", "prod"])
    assert cfg.argv[:5] == ("run", "--profile", "x", "--target", "prod")


def test_missing_profiles_yml_is_left_to_dbt(layout, monkeypatch):
    root = layout.platform_root(sample="platform_sqlite.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop", with_profiles=False)
    monkeypatch.chdir(root)
    load_config(["build", "--project-dir", "jaffle-shop"])  # no ConfigError


# --------------------------------------------------------------------------------------
# Which config file is used
# --------------------------------------------------------------------------------------

BUILD = ["build", "--project-dir", "jaffle-shop"]


def test_platform_root_file_by_default(platform, monkeypatch):
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert cfg.config_file == platform / "dbtx.conf"
    assert cfg.config_file_reason == "platform root"


def test_env_config_file_beats_the_platform_file(platform, layout, monkeypatch):
    env_file = layout.config_file(layout.directory("ci"), "platform_postgres.conf", "env.conf")
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_CONFIG_FILE", str(env_file))
    cfg = load_config(BUILD)
    assert cfg.config_file == env_file
    assert cfg.config_file_reason == "DBTX_CONFIG_FILE"


def test_cli_config_file_beats_env_and_is_never_merged(platform, layout, monkeypatch):
    ci = layout.directory("ci")
    env_file = layout.config_file(ci, "platform_postgres.conf", "env.conf")
    cli_file = layout.config_file(ci, "backend_disabled.conf", "cli.conf")
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_CONFIG_FILE", str(env_file))
    cfg = load_config([*BUILD, "--config-file", str(cli_file)])
    assert cfg.config_file == cli_file
    # The platform file and the env file both set DB_URI; neither is merged in.
    assert cfg.db_uri.value is None


def test_explicit_config_file_flag_must_exist(platform, monkeypatch):
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="does not exist"):
        load_config([*BUILD, "--config-file", "nope.conf"])


def test_explicit_config_file_env_must_exist(platform, monkeypatch):
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_CONFIG_FILE", "nope.conf")
    with pytest.raises(ConfigError, match="DBTX_CONFIG_FILE=nope.conf"):
        load_config(BUILD)


def test_explicit_config_file_replaces_a_missing_platform_file(layout, monkeypatch):
    root = layout.platform_root(sample=None)
    layout.project(root, "jaffle-shop", "jaffle_shop")
    conf = layout.config_file(layout.directory("ci"), "backend_disabled.conf", "ci.conf")
    monkeypatch.chdir(layout.base)
    cfg = load_config(
        ["build", "--platform-dir", str(root), "--project-dir", "jaffle-shop", "--config-file", str(conf)]
    )
    assert cfg.backend_enabled is False


def test_dotenv_is_never_read(layout, monkeypatch):
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    shutil.copyfile(config_sample("decoy.env"), root / ".env")
    monkeypatch.chdir(root)
    assert load_config(["deps", "--project-dir", "jaffle-shop"]).db_uri.value is None


# --------------------------------------------------------------------------------------
# Steps 3-4: precedence and sources
# --------------------------------------------------------------------------------------


def test_db_uri_from_the_cli_beats_file_and_env(platform, monkeypatch):
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_URI", "sqlite:////env.db")
    cfg = load_config([*BUILD, "--db-uri", "sqlite:////cli.db"])
    assert (cfg.db_uri.value, cfg.db_uri.source) == ("sqlite:////cli.db", "cli")


def test_db_uri_from_the_file_beats_env(platform, monkeypatch):
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_URI", "sqlite:////env.db")
    cfg = load_config(BUILD)
    assert cfg.db_uri.source == f"file {platform / 'dbtx.conf'}:1"


def test_db_uri_from_env_when_the_file_has_none(layout, monkeypatch):
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_URI", "sqlite:////env.db")
    cfg = load_config(BUILD)
    assert (cfg.db_uri.value, cfg.db_uri.source) == ("sqlite:////env.db", "env DBTX_DB_URI")


def test_db_backend_default_is_enabled(platform, monkeypatch):
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert (cfg.db_backend.value, cfg.db_backend.source) == ("enabled", "default")


def test_db_backend_from_env(platform, monkeypatch):
    monkeypatch.chdir(platform)
    monkeypatch.setenv("DBTX_DB_BACKEND", "disabled")
    cfg = load_config(BUILD)
    assert (cfg.db_backend.value, cfg.db_backend.source) == ("disabled", "env DBTX_DB_BACKEND")


def test_db_backend_from_the_file_beats_env(layout, monkeypatch):
    root = layout.platform_root(sample="backend_without_uri.conf")  # DB_BACKEND=enabled
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_BACKEND", "disabled")
    cfg = load_config(BUILD)
    assert cfg.db_backend.value == "enabled"
    assert cfg.db_backend.source == f"file {root / 'dbtx.conf'}:2"


def test_db_backend_from_the_cli_beats_everything(layout, monkeypatch):
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DBTX_DB_BACKEND", "enabled")
    cfg = load_config([*BUILD, "--db-backend", "disabled"])
    assert (cfg.db_backend.value, cfg.db_backend.source) == ("disabled", "cli")


def test_unknown_dbtx_env_vars_warn(layout, monkeypatch):
    monkeypatch.chdir(layout.directory("empty"))
    monkeypatch.setenv("DBTX_DB_URL", "x")
    monkeypatch.setenv("DBTX_DB_STRICT", "1")
    cfg = load_config(["--help"])
    assert [w for w in cfg.warnings if "unknown environment variable" in w] == [
        "unknown environment variable DBTX_DB_URL is ignored"
    ]


# --------------------------------------------------------------------------------------
# Step 5: values
# --------------------------------------------------------------------------------------


def test_db_backend_is_case_insensitive(platform, monkeypatch):
    monkeypatch.chdir(platform)
    assert load_config([*BUILD, "--db-backend", "DISABLED"]).db_backend.value == "disabled"


def test_db_backend_rejects_other_values(platform, monkeypatch):
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=r"DB_BACKEND \(cli\): 'maybe'"):
        load_config([*BUILD, "--db-backend", "maybe"])


def test_db_backend_value_from_a_file_names_the_line(layout, monkeypatch):
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
def test_invalid_db_uri(platform, monkeypatch, uri, error):
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=error):
        load_config([*BUILD, "--db-uri", uri])


def test_malformed_uri_is_rejected_even_when_disabled(platform, monkeypatch):
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="not a valid SQLAlchemy URL"):
        load_config([*BUILD, "--db-backend", "disabled", "--db-uri", "nope"])


@pytest.fixture
def psycopg_missing(monkeypatch):
    """psycopg is not installed (as far as dbtx can tell)."""
    real_find_spec = config.importlib.util.find_spec
    monkeypatch.setattr(
        config.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "psycopg" else real_find_spec(name, *a),
    )


def test_missing_psycopg_is_an_error(platform, monkeypatch, psycopg_missing):
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match=r"install dbtx\[postgres\]"):
        load_config([*BUILD, "--db-uri", "postgresql+psycopg://u@h/db"])


def test_a_disabled_backend_never_needs_its_driver(platform, monkeypatch, psycopg_missing):
    monkeypatch.chdir(platform)
    load_config([*BUILD, "--db-uri", "postgresql+psycopg://u@h/db", "--db-backend", "disabled"])


def test_password_is_masked(layout, monkeypatch):
    root = layout.platform_root(sample="platform_postgres.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    assert load_config(BUILD).masked_db_uri() == "postgresql+psycopg://dbtx:***@db/dbtx"


# --------------------------------------------------------------------------------------
# Step 6: requirement check and command classes
# --------------------------------------------------------------------------------------


@pytest.fixture
def platform_without_uri(layout):
    root = layout.platform_root(sample="backend_without_uri.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    return root


def test_missing_uri_for_a_database_command(platform_without_uri, monkeypatch, enforce):
    monkeypatch.chdir(platform_without_uri)
    with pytest.raises(ConfigError) as info:
        load_config(BUILD)
    message = str(info.value)
    assert "needs a database" in message
    assert "--db-backend disabled" in message and "--standalone-mode" in message


def test_missing_uri_only_warns_until_enforced(platform_without_uri, monkeypatch):
    monkeypatch.chdir(platform_without_uri)
    cfg = load_config(BUILD)
    assert any("needs a database" in w for w in cfg.warnings)


@pytest.mark.parametrize(
    "argv",
    [
        [*BUILD, "--db-backend", "disabled"],
        ["deps", "--project-dir", "jaffle-shop"],
        ["debug", "--project-dir", "jaffle-shop"],
        ["config"],
        ["db", "status"],
    ],
)
def test_missing_uri_is_fine_when_disabled_or_unused(
    platform_without_uri, monkeypatch, enforce, argv
):
    monkeypatch.chdir(platform_without_uri)
    load_config(argv)


@pytest.mark.parametrize("command", config.COMMANDS_THAT_USE_THE_DATABASE)
def test_commands_that_use_the_database(platform, monkeypatch, command):
    monkeypatch.chdir(platform)
    argv = [*command.split(), "--project-dir", "jaffle-shop"]
    assert load_config(argv).uses_database is True


@pytest.mark.parametrize(
    "command",
    [
        "config", "db status", "ls", "list", "debug", "deps", "clean", "show",
        "retry", "clone", "docs serve", "docs patch", "templates",
    ],
)
def test_commands_that_do_not_require_the_database(platform, monkeypatch, command):
    monkeypatch.chdir(platform)
    argv = [*command.split(), "--project-dir", "jaffle-shop"]
    assert load_config(argv).uses_database is False


# --------------------------------------------------------------------------------------
# Step 7: SQLite paths
# --------------------------------------------------------------------------------------


def test_relative_sqlite_path_from_the_file_is_relative_to_the_file(platform, layout, monkeypatch):
    monkeypatch.chdir(layout.directory("elsewhere"))
    cfg = load_config(["build", "--platform-dir", str(platform), "--project-dir", "jaffle-shop"])
    assert cfg.db_uri.value == f"sqlite:///{platform}/.dbtx/dbtx.db"


def test_relative_sqlite_path_from_the_cli_is_relative_to_cwd(platform, monkeypatch):
    monkeypatch.chdir(platform / "jaffle-shop")
    cfg = load_config(["build", "--platform-dir", str(platform), "--db-uri", "sqlite:///here.db"])
    assert cfg.db_uri.value == f"sqlite:///{platform}/jaffle-shop/here.db"


@pytest.mark.parametrize("uri", ["sqlite:////abs/x.db", "sqlite://", "sqlite:///:memory:"])
def test_other_sqlite_paths_are_unchanged(platform, monkeypatch, uri):
    monkeypatch.chdir(platform)
    assert load_config([*BUILD, "--db-uri", uri]).db_uri.value == uri


# --------------------------------------------------------------------------------------
# Step 8: guard G1
# --------------------------------------------------------------------------------------


@pytest.fixture
def colliding_platform(platform, layout):
    """The standard platform plus jaffle-shop-2, which reuses the name jaffle_shop."""
    layout.project(platform, "jaffle-shop-2", "jaffle_shop")
    return platform


@pytest.mark.parametrize("folder", ["jaffle-shop", "jaffle-shop-2"])
def test_g1_collision_with_the_current_project(colliding_platform, monkeypatch, enforce, folder):
    monkeypatch.chdir(colliding_platform)
    with pytest.raises(ConfigError) as info:
        load_config(["build", "--project-dir", folder])
    assert "'jaffle_shop' is declared by 2 folders" in str(info.value)
    assert "jaffle-shop, jaffle-shop-2" in str(info.value)


def test_g1_only_warns_until_enforced(colliding_platform, monkeypatch):
    monkeypatch.chdir(colliding_platform)
    cfg = load_config(BUILD)
    assert any("declared by 2 folders" in w for w in cfg.warnings)


def test_g1_unrelated_collision_is_a_warning(colliding_platform, monkeypatch, enforce):
    monkeypatch.chdir(colliding_platform)
    cfg = load_config(["build", "--project-dir", "other-shop"])
    assert any("'jaffle_shop' is declared by 2 folders" in w for w in cfg.warnings)


@pytest.mark.parametrize(
    "argv", [[*BUILD, "--db-backend", "disabled"], ["deps", "--project-dir", "jaffle-shop"]]
)
def test_g1_needs_the_backend_and_a_database_command(colliding_platform, monkeypatch, enforce, argv):
    monkeypatch.chdir(colliding_platform)
    load_config(argv)


def test_g1_ignores_hidden_and_nested_folders(platform, layout, monkeypatch, enforce):
    layout.project(platform, ".backup", "jaffle_shop")
    layout.project(platform / "other-shop" / "dbt_packages", "copy", "jaffle_shop")
    monkeypatch.chdir(platform)
    cfg = load_config(BUILD)
    assert not any("declared by" in w for w in cfg.warnings)


def test_g1_renders_a_jinja_name(platform, layout, monkeypatch, enforce):
    layout.project(
        platform,
        "jaffle-shop-2",
        "unused",
        dbt_project_text="name: \"{{ env_var('DBTX_TEST_UNSET_NAME', 'jaffle_shop') }}\"\n",
    )
    monkeypatch.chdir(platform)
    with pytest.raises(ConfigError, match="declared by 2 folders"):
        load_config(BUILD)
