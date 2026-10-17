"""dbtx configuration: loaded and validated before anything else a command does.

See docs/agentic/orm_implementation_plan.md, section 6. In short:

* **Modes.** Platform mode is the default. The platform root is `--platform-dir DIR`,
  or else the current directory when it holds `dbtx.conf` and is not itself a dbt
  project. `--standalone-mode` opts out: no config file, no database, no platform
  checks.
* **Layers** (platform mode only), highest first: CLI flags, the config file, then
  `DBTX_*` environment variables. `.env` files are never read.
* **Options:** `DB_BACKEND` (`enabled` | `disabled`, default `enabled`), `DB_URI`
  (required when the backend is enabled and the command uses the database),
  `DB_SCHEMA` (default `dbtx`; only for backends with schemas, such as Postgres) and
  `DB_ADAPTER` (the store adapter's name; default: the adapter for DB_URI's backend and
  driver, see `dbtx.store.adapters`).

`load_config(argv)` either returns a `DbtxConfig` or raises `ConfigError`. Callers turn
a `ConfigError` into exit code 2 before dbt is ever started.
"""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

CONFIG_FILE_NAME = "dbtx.conf"
DBT_PROJECT_FILE_NAME = "dbt_project.yml"

# dbtx's own command-line flags. They are removed from argv before dbt sees it.
FLAG_DB_BACKEND = "--db-backend"
FLAG_DB_URI = "--db-uri"
FLAG_DB_SCHEMA = "--db-schema"
FLAG_DB_ADAPTER = "--db-adapter"
FLAG_CONFIG_FILE = "--config-file"
FLAG_PLATFORM_DIR = "--platform-dir"
FLAG_STANDALONE_MODE = "--standalone-mode"
FLAG_LOG_ROOT_LOCATION = "--log-root-location"
FLAG_JOB_ID = "--job-id"

FLAGS_WITH_A_VALUE = (
    FLAG_DB_BACKEND,
    FLAG_DB_URI,
    FLAG_DB_SCHEMA,
    FLAG_DB_ADAPTER,
    FLAG_CONFIG_FILE,
    FLAG_PLATFORM_DIR,
    FLAG_LOG_ROOT_LOCATION,
    FLAG_JOB_ID,
)

# The run-folder flags: isolated runs are a platform feature.
RUN_FOLDER_FLAGS = (FLAG_LOG_ROOT_LOCATION, FLAG_JOB_ID)

# The --db-* flags: none of them has any meaning in standalone mode.
DB_FLAGS = (FLAG_DB_BACKEND, FLAG_DB_URI, FLAG_DB_SCHEMA, FLAG_DB_ADAPTER)

# Keys allowed in dbtx.conf.
FILE_KEY_DB_BACKEND = "DB_BACKEND"
FILE_KEY_DB_URI = "DB_URI"
FILE_KEY_DB_SCHEMA = "DB_SCHEMA"
FILE_KEY_DB_ADAPTER = "DB_ADAPTER"
FILE_KEY_LOG_ROOT_LOCATION = "LOG_ROOT_LOCATION"
FILE_KEYS = (
    FILE_KEY_DB_BACKEND,
    FILE_KEY_DB_URI,
    FILE_KEY_DB_SCHEMA,
    FILE_KEY_DB_ADAPTER,
    FILE_KEY_LOG_ROOT_LOCATION,
)

# Environment variables dbtx reads.
ENV_DB_BACKEND = "DBTX_DB_BACKEND"
ENV_DB_URI = "DBTX_DB_URI"
ENV_DB_SCHEMA = "DBTX_DB_SCHEMA"
ENV_DB_ADAPTER = "DBTX_DB_ADAPTER"
ENV_CONFIG_FILE = "DBTX_CONFIG_FILE"
ENV_LOG_ROOT_LOCATION = "DBTX_LOG_ROOT_LOCATION"

# Every DBTX_* name dbtx knows about. Any other DBTX_* variable gets a warning, the env
# counterpart of the strict keys in dbtx.conf. DBTX_DB_STRICT is a test switch, read
# elsewhere.
KNOWN_ENV_VARS = (
    ENV_DB_BACKEND,
    ENV_DB_URI,
    ENV_DB_SCHEMA,
    ENV_DB_ADAPTER,
    ENV_CONFIG_FILE,
    ENV_LOG_ROOT_LOCATION,
    "DBTX_DB_STRICT",
)

# The key in --vars that names dbtx's job id (standalone mode only; in platform mode
# dbtx sets the job id itself and hands it to dbt as DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID).
DBTX_JOB_ID_VAR = "DBTX_JOB_ID"

# Commands that run in their own run folder in platform mode: the recorded commands
# (plan 5.2; dbtx.store.enums.COMMAND_BY_CLI). They require LOG_ROOT_LOCATION.
COMMANDS_WITH_A_RUN_FOLDER = (
    "build",
    "compile",
    "run",
    "test",
    "seed",
    "snapshot",
    "parse",
    "run-operation",
    "source freshness",
    "source snapshot-freshness",
    "docs generate",
)

DB_BACKEND_ENABLED = "enabled"
DB_BACKEND_DISABLED = "disabled"

MODE_PLATFORM = "platform"
MODE_STANDALONE = "standalone"

# DB_SCHEMA: the schema dbtx's tables live in, on backends that have schemas. Names are
# limited to what Postgres accepts unquoted (and folds to nothing else), so the name in
# the config is exactly the name in the database.
DEFAULT_DB_SCHEMA = "dbtx"
DB_SCHEMA_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")

# Commands that use the database: the commands the store records (plan 5.2), `dbtx docs
# serve` (it serves the run history from the database, plan Phase 7) and the `dbtx db`
# commands. In platform mode with the backend enabled they require DB_URI.
COMMANDS_THAT_USE_THE_DATABASE = (
    "build",
    "compile",
    "run",
    "test",
    "seed",
    "snapshot",
    "parse",
    "run-operation",
    "source freshness",
    "source snapshot-freshness",
    "docs generate",
    "docs serve",
    "db init",
    "db upgrade",
    "db load",
    "db projects",
)

# Commands that report on the database. They never fail because DB_URI is missing.
COMMANDS_THAT_REPORT_ON_THE_DATABASE = ("config", "db status")

# Commands dbtx implements itself, rather than handing them to dbt.
DBTX_COMMANDS = (
    "templates",
    "warm-cache",
    "config",
    "docs patch",
    "docs install",
    "docs status",
    "docs serve",
    "db init",
    "db upgrade",
    "db status",
    "db load",
    "db projects",
)

# Commands that do not belong to a single project. In platform mode they run without a
# project dir, so they also work from the platform root.
COMMANDS_WITHOUT_A_PROJECT = (
    "config",
    "init",
    "docs patch",
    "docs install",
    "docs status",
    "docs serve",
    "db init",
    "db upgrade",
    "db status",
    "db projects",
)

# `dbtx docs` commands whose --docs-loc defaults, in platform mode, to the docs site in
# the project's own target folder: they need a project only when --docs-loc is not given.
DOCS_SITE_COMMANDS = ("docs install", "docs status", "docs serve")

# Two-word commands start with one of these groups.
COMMAND_GROUPS = ("docs", "source", "db")


class ConfigError(Exception):
    """A configuration problem. The command exits 2 without starting dbt."""


@dataclass(frozen=True)
class Setting:
    """One resolved option: its value and where the value came from.

    `source` is "cli", "file <path>:<line>", "env <NAME>" or "default". `origin_dir` is
    the directory a relative SQLite path in the value is resolved against.
    """

    value: Optional[str]
    source: str
    origin_dir: Optional[Path] = None


@dataclass(frozen=True)
class DbtxConfig:
    mode: str
    mode_reason: str
    platform_root: Optional[Path]
    project_dir: Optional[Path]
    config_file: Optional[Path]
    config_file_reason: str
    # None in standalone mode, where database options do not exist. `db_schema` always
    # has a value in platform mode (the default is "dbtx"); it is used only by backends
    # with schemas. `db_adapter`'s value is None when DB_ADAPTER is not set: the
    # default adapter for DB_URI.
    db_backend: Optional[Setting]
    db_uri: Optional[Setting]
    db_schema: Optional[Setting]
    db_adapter: Optional[Setting]
    command: str
    uses_database: bool
    # Guard G1 (platform mode, backend enabled, a database command with a project):
    # the project's dbt name, and every folder on the platform declaring it when there
    # is more than one. The start-up checks decide a collision together with guard G2.
    project_name: Optional[str]
    colliding_folders: tuple[str, ...]
    # The argv to hand on: dbtx flags removed and, in platform mode, the absolute
    # --project-dir / --profiles-dir added.
    argv: tuple[str, ...]
    warnings: tuple[str, ...]
    # DBTX_* variables that were set but ignored (standalone mode).
    ignored_env_vars: tuple[str, ...]
    # Isolated runs (platform mode): the root of the run folders (None in standalone
    # mode), and for a command that runs in a run folder its job id and folder.
    log_root_location: Optional[Setting] = None
    job_id: Optional[str] = None
    run_folder: Optional[Path] = None
    # The project's own target folder (platform mode, a command with a project): where
    # the warm partial-parse cache and the docs site live.
    project_target_dir: Optional[Path] = None

    @property
    def is_platform(self) -> bool:
        return self.mode == MODE_PLATFORM

    @property
    def backend_enabled(self) -> bool:
        return self.db_backend is not None and self.db_backend.value == DB_BACKEND_ENABLED

    def resolved_adapter(self):
        """(name, class) of the store adapter for DB_URI and DB_ADAPTER, or None when no
        URI is set. load_config has already checked that it resolves."""
        if self.db_uri is None or self.db_uri.value is None:
            return None
        from sqlalchemy.engine import make_url

        from dbtx.store.adapters import resolve_adapter

        assert self.db_adapter is not None
        url = make_url(self.db_uri.value)
        return resolve_adapter(url.get_backend_name(), url.get_driver_name(), self.db_adapter.value)

    def masked_db_uri(self) -> Optional[str]:
        if self.db_uri is None or self.db_uri.value is None:
            return None
        return mask_uri(self.db_uri.value)


# --------------------------------------------------------------------------------------
# Time and generated ids (tests replace these)
# --------------------------------------------------------------------------------------


def run_started_at() -> datetime:
    """Now, aware UTC: names the run folder's date and hour."""
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    """A new job id for a run that was given none."""
    return str(uuid.uuid4())


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def load_config(argv: list[str]) -> DbtxConfig:
    """Steps 1-8 of the plan's validation order (section 6.6), for one command line."""
    warnings: list[str] = []
    cwd = Path.cwd()

    # Step 1: take dbtx's own flags out of argv.
    flag_values, standalone_requested, rest = extract_dbtx_flags(argv)
    command = command_name(rest)
    is_help = is_help_request(rest)

    # Step 2: decide the mode.
    (
        mode,
        mode_reason,
        platform_root,
    ) = decide_mode(flag_values, standalone_requested, cwd, is_help)

    warn_about_unknown_env_vars(warnings)

    if mode == MODE_STANDALONE:
        return standalone_config(mode_reason, rest, command, warnings)

    assert platform_root is not None

    # Step 2 (platform): the project dir, the argv rewrite and the config file.
    if command == "" and not is_help:
        # e.g. `dbtx --debug build`. Without knowing the command, dbtx cannot scope the
        # project and profiles dir, so dbt would fall back to cwd and ~/.dbt.
        raise ConfigError(
            f"in platform mode the command must come first (got {rest[0]!r}); put dbt "
            f"options after it, e.g. `dbtx build --debug`"
        )
    project_dir: Optional[Path] = None
    needs_a_project = command_needs_a_project(command)
    if command in DOCS_SITE_COMMANDS and last_option_value(rest, "--docs-loc") is None:
        # Without --docs-loc these default to the project's docs site (plan Phase 6).
        needs_a_project = True
    if not is_help and needs_a_project:
        project_dir = resolve_platform_project_dir(rest, platform_root, cwd)
        check_no_project_config_file(project_dir, platform_root)
        if command_is_handed_to_dbt(command):
            check_profiles_dir(rest, project_dir, cwd)
            check_no_dbtx_job_id_in_vars(rest)
            rest = inject_project_and_profiles_dir(rest, project_dir)
        elif command == "templates":
            rest = inject_project_dir_only(rest, project_dir)
        elif command == "warm-cache":
            # dbtx's command, but it hands `dbt parse` the rest of the argv.
            check_profiles_dir(rest, project_dir, cwd)
            check_no_dbtx_job_id_in_vars(rest)
            if last_option_value(rest, "--target-path") is not None:
                raise ConfigError(
                    "--target-path conflicts with `dbtx warm-cache`: the cache goes to the "
                    "project's target folder, where runs look for it"
                )
            rest = inject_project_and_profiles_dir(rest, project_dir)

    config_file, config_file_reason = locate_config_file(flag_values, platform_root, cwd)
    file_entries = parse_config_file(config_file)

    # Steps 3-4: read the env and resolve each option by precedence.
    db_backend = resolve_db_backend(flag_values, file_entries, config_file, cwd)
    db_uri = resolve_db_uri(flag_values, file_entries, config_file, cwd)
    db_schema = resolve_db_schema(flag_values, file_entries, config_file, cwd)
    db_adapter = resolve_db_adapter(flag_values, file_entries, config_file, cwd)

    # Step 5: validate the values.
    db_backend = validate_db_backend(db_backend)
    backend_enabled = db_backend.value == DB_BACKEND_ENABLED
    validate_db_adapter_name(db_adapter)
    adapter_class = None
    if db_uri.value is not None:
        adapter_class = validate_db_uri(db_uri, db_adapter, check_driver=backend_enabled)
    validate_db_schema(db_schema, adapter_class)

    # Step 6: the requirement check, behind its gates.
    uses_database = command in COMMANDS_THAT_USE_THE_DATABASE
    if backend_enabled and uses_database and db_uri.value is None:
        message = (
            f"`dbtx {command}` needs a database: set DB_URI (with --db-uri, "
            f"DB_URI= in {config_file}, or {ENV_DB_URI}), pass --db-backend disabled, "
            f"or pass --standalone-mode"
        )
        raise ConfigError(message)

    # Step 7: make a relative SQLite path absolute.
    if db_uri.value is not None:
        db_uri = normalise_sqlite_path(db_uri)

    # Isolated runs (plan Phase 6): the run folder, its job id, and dbt's paths.
    log_root_location = resolve_log_root_location(flag_values, file_entries, config_file, cwd)
    job_id: Optional[str] = None
    run_folder: Optional[Path] = None
    project_target_dir: Optional[Path] = None
    if project_dir is not None:
        project_target_dir = project_target_dir_for(project_dir)
    if command in COMMANDS_WITH_A_RUN_FOLDER and project_dir is not None:
        check_no_own_target_or_log_path(rest, command)
        if log_root_location.value is None:
            raise ConfigError(
                f"`dbtx {command}` needs a run folder root: set LOG_ROOT_LOCATION (with "
                f"{FLAG_LOG_ROOT_LOCATION}, LOG_ROOT_LOCATION= in {config_file}, or "
                f"{ENV_LOG_ROOT_LOCATION}), or pass {FLAG_STANDALONE_MODE}"
            )
        job_id = choose_job_id(flag_values)
        run_folder = run_folder_for(Path(log_root_location.value), project_dir, job_id)
        rest = rest + ["--target-path", str(run_folder), "--log-path", str(run_folder)]
    elif FLAG_JOB_ID in flag_values and not is_help:
        raise ConfigError(
            f"{FLAG_JOB_ID} applies only to commands that run in a run folder, not "
            f"`dbtx {command}`"
        )

    # Step 8 (Phase 0 part): guard G1. A collision involving this project is decided by
    # the start-up checks, together with guard G2 (dbtx.store.startup).
    project_name = None
    colliding_folders: tuple[str, ...] = ()
    if backend_enabled and uses_database and project_dir is not None:
        project_name, colliding_folders = check_project_name_collisions(
            platform_root, project_dir, warnings
        )

    return DbtxConfig(
        mode=MODE_PLATFORM,
        mode_reason=mode_reason,
        platform_root=platform_root,
        project_dir=project_dir,
        config_file=config_file,
        config_file_reason=config_file_reason,
        db_backend=db_backend,
        db_uri=db_uri,
        db_schema=db_schema,
        db_adapter=db_adapter,
        command=command,
        uses_database=uses_database,
        project_name=project_name,
        colliding_folders=colliding_folders,
        argv=tuple(rest),
        warnings=tuple(warnings),
        ignored_env_vars=(),
        log_root_location=log_root_location,
        job_id=job_id,
        run_folder=run_folder,
        project_target_dir=project_target_dir,
    )


# --------------------------------------------------------------------------------------
# Step 1: argv
# --------------------------------------------------------------------------------------


def extract_dbtx_flags(argv: list[str]) -> tuple[dict[str, str], bool, list[str]]:
    """Split dbtx's own flags out of argv, wherever they appear.

    Returns (flag values, whether --standalone-mode was given, everything else).
    Accepts `--flag value` and `--flag=value`.
    """
    flag_values: dict[str, str] = {}
    standalone_requested = False
    rest: list[str] = []

    i = 0
    while i < len(argv):
        token = argv[i]

        if token == FLAG_STANDALONE_MODE:
            standalone_requested = True
            i += 1
            continue
        if token.startswith(FLAG_STANDALONE_MODE + "="):
            raise ConfigError(f"{FLAG_STANDALONE_MODE} takes no value")

        matched_flag = None
        value = None
        for flag in FLAGS_WITH_A_VALUE:
            if token == flag:
                if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                    raise ConfigError(f"{flag} needs a value")
                matched_flag = flag
                value = argv[i + 1]
                i += 2
                break
            if token.startswith(flag + "="):
                value = token[len(flag) + 1 :]
                if value == "":
                    raise ConfigError(f"{flag} needs a value")
                matched_flag = flag
                i += 1
                break

        if matched_flag is None:
            rest.append(token)
            i += 1
            continue

        if matched_flag in flag_values:
            raise ConfigError(f"{matched_flag} was given more than once")
        flag_values[matched_flag] = value  # type: ignore[assignment]

    return flag_values, standalone_requested, rest


def command_name(argv: list[str]) -> str:
    """The command the user asked for: `build`, `docs generate`, `db status`, or ""."""
    if not argv or argv[0].startswith("-"):
        return ""
    first = argv[0]
    if first in COMMAND_GROUPS and len(argv) > 1 and not argv[1].startswith("-"):
        return f"{first} {argv[1]}"
    return first


def is_help_request(argv: list[str]) -> bool:
    if not argv:
        return True  # bare `dbtx` prints dbt's help
    if argv == ["docs"]:
        return True  # bare `dbtx docs` prints dbtx's docs help
    return "-h" in argv or "--help" in argv or "--version" in argv


# --------------------------------------------------------------------------------------
# Step 2: mode
# --------------------------------------------------------------------------------------


def decide_mode(
    flag_values: dict[str, str],
    standalone_requested: bool,
    cwd: Path,
    is_help: bool,
) -> tuple[str, str, Optional[Path]]:
    """Returns (mode, reason, platform root). First match wins (plan 6.5.1)."""

    # Rule 0: --standalone-mode.
    if standalone_requested:
        if FLAG_PLATFORM_DIR in flag_values:
            raise ConfigError(
                f"{FLAG_STANDALONE_MODE} and {FLAG_PLATFORM_DIR} contradict each other"
            )
        for flag in DB_FLAGS:
            if flag in flag_values:
                raise ConfigError(
                    f"{flag} has no effect with {FLAG_STANDALONE_MODE}: standalone mode "
                    f"has no database"
                )
        if FLAG_CONFIG_FILE in flag_values:
            raise ConfigError(
                f"{FLAG_CONFIG_FILE} has no effect with {FLAG_STANDALONE_MODE}: "
                f"standalone mode reads no config file"
            )
        for flag in RUN_FOLDER_FLAGS:
            if flag in flag_values:
                raise ConfigError(
                    f"{flag} has no effect with {FLAG_STANDALONE_MODE}: run folders are a "
                    f"platform feature"
                )
        return MODE_STANDALONE, f"forced by {FLAG_STANDALONE_MODE}", None

    # Rule 1: --platform-dir.
    if FLAG_PLATFORM_DIR in flag_values:
        root = Path(flag_values[FLAG_PLATFORM_DIR]).expanduser()
        if not root.is_absolute():
            root = cwd / root
        root = root.resolve()
        if not root.exists():
            raise ConfigError(f"{FLAG_PLATFORM_DIR} {root} does not exist")
        if not root.is_dir():
            raise ConfigError(f"{FLAG_PLATFORM_DIR} {root} is not a directory")
        if (root / DBT_PROJECT_FILE_NAME).exists():
            raise ConfigError(
                f"{FLAG_PLATFORM_DIR} {root} is a dbt project, not a platform root; "
                f"the platform root is the folder that contains the project folders"
            )
        return MODE_PLATFORM, f"{FLAG_PLATFORM_DIR}", root

    # Rule 2: the current directory holds dbtx.conf and is not a dbt project.
    cwd_has_config = (cwd / CONFIG_FILE_NAME).is_file()
    cwd_is_a_project = (cwd / DBT_PROJECT_FILE_NAME).exists()
    if cwd_has_config and not cwd_is_a_project:
        return MODE_PLATFORM, f"current directory holds {CONFIG_FILE_NAME}", cwd.resolve()

    # None of the above.
    if is_help:
        return MODE_STANDALONE, "no platform found (help requested)", None

    message = (
        f"no dbtx platform found (no {FLAG_PLATFORM_DIR}, and {cwd} has no "
        f"{CONFIG_FILE_NAME}). Run from the platform root, pass "
        f"{FLAG_PLATFORM_DIR} <dir>, or add {FLAG_STANDALONE_MODE} to run without the "
        f"platform"
    )
    if cwd_has_config and cwd_is_a_project:
        message += (
            f". Note: {cwd / CONFIG_FILE_NAME} is inside a dbt project; a project folder "
            f"is never a platform root"
        )
    raise ConfigError(message)


def standalone_config(
    mode_reason: str, rest: list[str], command: str, warnings: list[str]
) -> DbtxConfig:
    """Standalone mode: no config file, no database options, no platform checks."""
    ignored = []
    for name in (
        ENV_DB_BACKEND,
        ENV_DB_URI,
        ENV_DB_SCHEMA,
        ENV_DB_ADAPTER,
        ENV_CONFIG_FILE,
        ENV_LOG_ROOT_LOCATION,
    ):
        if os.environ.get(name):
            ignored.append(name)

    return DbtxConfig(
        mode=MODE_STANDALONE,
        mode_reason=mode_reason,
        platform_root=None,
        project_dir=standalone_project_dir(rest),
        config_file=None,
        config_file_reason="none (standalone mode reads no config file)",
        db_backend=None,
        db_uri=None,
        db_schema=None,
        db_adapter=None,
        command=command,
        uses_database=False,
        project_name=None,
        colliding_folders=(),
        argv=tuple(rest),
        warnings=tuple(warnings),
        ignored_env_vars=tuple(ignored),
    )


def standalone_project_dir(argv: list[str]) -> Optional[Path]:
    """The project dir dbt will use, for display only. dbtx changes nothing here."""
    raw = last_option_value(argv, "--project-dir")
    if raw is None:
        raw = os.environ.get("DBT_PROJECT_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    from dbt.cli.resolvers import default_project_dir

    return default_project_dir().resolve()


# --------------------------------------------------------------------------------------
# Step 2 (platform): project dir, profiles dir, argv rewrite
# --------------------------------------------------------------------------------------


def command_needs_a_project(command: str) -> bool:
    if command == "":
        return False
    if command in COMMANDS_WITHOUT_A_PROJECT:
        return False
    if command in DBTX_COMMANDS:
        return True  # templates, db load
    return command in dbt_commands()


def command_is_handed_to_dbt(command: str) -> bool:
    return command not in DBTX_COMMANDS and command in dbt_commands()


def dbt_commands() -> set[str]:
    """Every dbt command name, with groups spelled out: `build`, `docs generate`, ..."""
    from dbt.cli.main import cli

    names: set[str] = set()
    for name, command in cli.commands.items():
        subcommands = getattr(command, "commands", None)
        if subcommands:
            for subname in subcommands:
                names.add(f"{name} {subname}")
        else:
            names.add(name)
    return names


def last_option_value(argv: list[str], option: str) -> Optional[str]:
    """The value of the last `option value` / `option=value` in argv, as click uses it."""
    value = None
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == option and i + 1 < len(argv):
            value = argv[i + 1]
            i += 2
            continue
        if token.startswith(option + "="):
            value = token[len(option) + 1 :]
        i += 1
    return value


def remove_option(argv: list[str], option: str) -> list[str]:
    """argv without any `option value` / `option=value`."""
    kept: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == option:
            i += 2
            continue
        if token.startswith(option + "="):
            i += 1
            continue
        kept.append(token)
        i += 1
    return kept


def project_folders(platform_root: Path) -> list[str]:
    """Names of the platform root's direct children that are dbt projects."""
    names = []
    for child in sorted(platform_root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_dir() and (child / DBT_PROJECT_FILE_NAME).is_file():
            names.append(child.name)
    return names


def resolve_platform_project_dir(argv: list[str], platform_root: Path, cwd: Path) -> Path:
    """The project for this run, which must be a direct child of the platform root."""
    raw = last_option_value(argv, "--project-dir")
    source = "--project-dir"
    if raw is None:
        raw = os.environ.get("DBT_PROJECT_DIR")
        source = "DBT_PROJECT_DIR"

    folders = ", ".join(project_folders(platform_root)) or "none found"

    if raw:
        project_dir = Path(raw).expanduser()
        if not project_dir.is_absolute():
            project_dir = platform_root / project_dir
        project_dir = project_dir.resolve()
    else:
        # dbt's own walk: cwd, then its parents, stopping at the platform root.
        project_dir = None
        candidate = cwd.resolve()
        while True:
            if candidate == platform_root:
                break
            if (candidate / DBT_PROJECT_FILE_NAME).is_file():
                project_dir = candidate
                break
            if candidate.parent == candidate:
                break
            candidate = candidate.parent
        if project_dir is None:
            raise ConfigError(
                f"{platform_root} is a dbtx platform; choose a project with "
                f"--project-dir <folder> ({folders})"
            )
        source = "current directory"

    if project_dir.parent != platform_root:
        raise ConfigError(
            f"project {project_dir} (from {source}) is not a direct child of the "
            f"platform root {platform_root}; project folders: {folders}"
        )
    if not project_dir.exists():
        raise ConfigError(
            f"{project_dir} (from {source}) does not exist; project folders: {folders}"
        )
    if not project_dir.is_dir():
        raise ConfigError(
            f"{project_dir} (from {source}) is not a directory; project folders: {folders}"
        )
    if not (project_dir / DBT_PROJECT_FILE_NAME).is_file():
        raise ConfigError(
            f"{project_dir} (from {source}) has no {DBT_PROJECT_FILE_NAME}; project "
            f"folders: {folders}"
        )
    return project_dir


def check_no_project_config_file(project_dir: Path, platform_root: Path) -> None:
    project_config = project_dir / CONFIG_FILE_NAME
    if project_config.exists():
        raise ConfigError(
            f"{project_config} is not allowed in platform mode: the platform's "
            f"{platform_root / CONFIG_FILE_NAME} configures every project. Remove the "
            f"project's {CONFIG_FILE_NAME}"
        )


def check_profiles_dir(argv: list[str], project_dir: Path, cwd: Path) -> None:
    """In platform mode profiles are scoped per project (plan 6.5.6).

    A --profiles-dir / DBT_PROFILES_DIR pointing anywhere else is rejected rather than
    silently overridden. Profiles themselves are not validated; dbt reports those.
    """
    raw = last_option_value(argv, "--profiles-dir")
    source = "--profiles-dir"
    if raw is None:
        raw = os.environ.get("DBT_PROFILES_DIR")
        source = "DBT_PROFILES_DIR"
    if not raw:
        return

    profiles_dir = Path(raw).expanduser()
    if not profiles_dir.is_absolute():
        profiles_dir = cwd / profiles_dir
    profiles_dir = profiles_dir.resolve()
    if profiles_dir != project_dir:
        raise ConfigError(
            f"{source}={raw} conflicts with platform mode: profiles are scoped per "
            f"project ({project_dir}). Unset it"
        )


def inject_project_and_profiles_dir(argv: list[str], project_dir: Path) -> list[str]:
    """Hand dbt the absolute project dir, and the same folder as its profiles dir.

    Added as real command-line options, so `explicit_params` sees them and `argv_for`
    forwards them to dbtx's internal `parse` and `ls` too.
    """
    rewritten = remove_option(argv, "--project-dir")
    rewritten = remove_option(rewritten, "--profiles-dir")
    rewritten += ["--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
    return rewritten


def inject_project_dir_only(argv: list[str], project_dir: Path) -> list[str]:
    """For `dbtx templates`, which takes --project-dir but has no profiles."""
    rewritten = remove_option(argv, "--project-dir")
    rewritten += ["--project-dir", str(project_dir)]
    return rewritten


# --------------------------------------------------------------------------------------
# Step 2 (platform): the config file
# --------------------------------------------------------------------------------------


def locate_config_file(
    flag_values: dict[str, str], platform_root: Path, cwd: Path
) -> tuple[Path, str]:
    """Exactly one file, the first that applies (plan 6.5.2). Never merged."""
    if FLAG_CONFIG_FILE in flag_values:
        path = Path(flag_values[FLAG_CONFIG_FILE]).expanduser()
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        if not path.is_file():
            raise ConfigError(f"{FLAG_CONFIG_FILE} {path} does not exist")
        return path, FLAG_CONFIG_FILE

    from_env = os.environ.get(ENV_CONFIG_FILE)
    if from_env:
        path = Path(from_env).expanduser()
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        if not path.is_file():
            raise ConfigError(f"{ENV_CONFIG_FILE}={from_env}: {path} does not exist")
        return path, ENV_CONFIG_FILE

    path = platform_root / CONFIG_FILE_NAME
    if not path.is_file():
        raise ConfigError(
            f"platform root {platform_root} has no {CONFIG_FILE_NAME}; a platform needs "
            f"one (or name a file with {FLAG_CONFIG_FILE})"
        )
    return path, "platform root"


def parse_config_file(path: Path) -> dict[str, tuple[str, int]]:
    """Read a strict KEY=VALUE file. Returns {key: (value, line number)}.

    Blank lines and `#` comment lines are ignored, whitespace around keys and values is
    stripped, and a value may be wrapped in matching single or double quotes. Anything
    else unusual is an error rather than a guess: unknown or duplicate keys, `export`,
    `${...}` interpolation, unterminated quotes (multi-line values) and empty values.
    """
    entries: dict[str, tuple[str, int]] = {}
    text = path.read_text(encoding="utf-8")

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        where = f"{path}:{number}"

        if line == "" or line.startswith("#"):
            continue
        if line.startswith("export "):
            raise ConfigError(f"{where}: `export` is not supported; write KEY=VALUE")
        if "=" not in line:
            raise ConfigError(f"{where}: expected KEY=VALUE, got {raw_line.strip()!r}")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if key not in FILE_KEYS:
            raise ConfigError(
                f"{where}: unknown key {key!r}; known keys: {', '.join(FILE_KEYS)}"
            )
        if key in entries:
            first_line = entries[key][1]
            raise ConfigError(f"{where}: {key} is already set on line {first_line}")

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif value[:1] in ("'", '"'):
            raise ConfigError(
                f"{where}: unterminated quote; multi-line values are not supported"
            )

        if "${" in value:
            raise ConfigError(f"{where}: ${{...}} interpolation is not supported")
        if value == "":
            raise ConfigError(f"{where}: {key} has an empty value")

        entries[key] = (value, number)

    return entries


# --------------------------------------------------------------------------------------
# Steps 3-4: env and precedence
# --------------------------------------------------------------------------------------


def warn_about_unknown_env_vars(warnings: list[str]) -> None:
    for name in sorted(os.environ):
        if name.startswith("DBTX_") and name not in KNOWN_ENV_VARS:
            warnings.append(f"unknown environment variable {name} is ignored")


def resolve_db_backend(
    flag_values: dict[str, str],
    file_entries: dict[str, tuple[str, int]],
    config_file: Path,
    cwd: Path,
) -> Setting:
    """DB_BACKEND: CLI, then the config file, then the env, then the default."""
    if FLAG_DB_BACKEND in flag_values:
        return Setting(flag_values[FLAG_DB_BACKEND], "cli", cwd)
    if FILE_KEY_DB_BACKEND in file_entries:
        value, line = file_entries[FILE_KEY_DB_BACKEND]
        return Setting(value, f"file {config_file}:{line}", config_file.parent)
    from_env = os.environ.get(ENV_DB_BACKEND)
    if from_env:
        return Setting(from_env, f"env {ENV_DB_BACKEND}", cwd)
    return Setting(DB_BACKEND_ENABLED, "default", None)


def resolve_db_uri(
    flag_values: dict[str, str],
    file_entries: dict[str, tuple[str, int]],
    config_file: Path,
    cwd: Path,
) -> Setting:
    """DB_URI: CLI, then the config file, then the env. No default."""
    if FLAG_DB_URI in flag_values:
        return Setting(flag_values[FLAG_DB_URI], "cli", cwd)
    if FILE_KEY_DB_URI in file_entries:
        value, line = file_entries[FILE_KEY_DB_URI]
        return Setting(value, f"file {config_file}:{line}", config_file.parent)
    from_env = os.environ.get(ENV_DB_URI)
    if from_env:
        return Setting(from_env, f"env {ENV_DB_URI}", cwd)
    return Setting(None, "unset", None)


def resolve_db_schema(
    flag_values: dict[str, str],
    file_entries: dict[str, tuple[str, int]],
    config_file: Path,
    cwd: Path,
) -> Setting:
    """DB_SCHEMA: CLI, then the config file, then the env, then the default."""
    if FLAG_DB_SCHEMA in flag_values:
        return Setting(flag_values[FLAG_DB_SCHEMA], "cli", cwd)
    if FILE_KEY_DB_SCHEMA in file_entries:
        value, line = file_entries[FILE_KEY_DB_SCHEMA]
        return Setting(value, f"file {config_file}:{line}", config_file.parent)
    from_env = os.environ.get(ENV_DB_SCHEMA)
    if from_env:
        return Setting(from_env, f"env {ENV_DB_SCHEMA}", cwd)
    return Setting(DEFAULT_DB_SCHEMA, "default", None)


def resolve_db_adapter(
    flag_values: dict[str, str],
    file_entries: dict[str, tuple[str, int]],
    config_file: Path,
    cwd: Path,
) -> Setting:
    """DB_ADAPTER: CLI, then the config file, then the env. Unset (None) means the
    default adapter for DB_URI."""
    if FLAG_DB_ADAPTER in flag_values:
        return Setting(flag_values[FLAG_DB_ADAPTER], "cli", cwd)
    if FILE_KEY_DB_ADAPTER in file_entries:
        value, line = file_entries[FILE_KEY_DB_ADAPTER]
        return Setting(value, f"file {config_file}:{line}", config_file.parent)
    from_env = os.environ.get(ENV_DB_ADAPTER)
    if from_env:
        return Setting(from_env, f"env {ENV_DB_ADAPTER}", cwd)
    return Setting(None, "default", None)


# --------------------------------------------------------------------------------------
# Step 5: values
# --------------------------------------------------------------------------------------


def validate_db_backend(setting: Setting) -> Setting:
    value = (setting.value or "").strip().lower()
    if value not in (DB_BACKEND_ENABLED, DB_BACKEND_DISABLED):
        raise ConfigError(
            f"DB_BACKEND ({setting.source}): {setting.value!r} is not "
            f"{DB_BACKEND_ENABLED!r} or {DB_BACKEND_DISABLED!r}"
        )
    return Setting(value, setting.source, setting.origin_dir)


def validate_db_adapter_name(setting: Setting) -> None:
    """DB_ADAPTER, when set, must name an installed adapter, even without a DB_URI.
    Whether it serves DB_URI is checked by validate_db_uri."""
    if setting.value is None:
        return

    from dbtx.store.adapters import adapter_class_by_name
    from dbtx.store.errors import StoreError

    try:
        adapter_class_by_name(setting.value)
    except StoreError as exc:
        raise ConfigError(f"DB_ADAPTER ({setting.source}): {exc}") from None


def validate_db_uri(setting: Setting, db_adapter: Setting, check_driver: bool):
    """Check DB_URI and DB_ADAPTER against the adapter registry. Returns the adapter
    class.

    An adapter must serve the URI's backend and driver: the one DB_ADAPTER names, or
    else the default for the pair. When `check_driver`, the package the driver needs
    must be installed. Validation never imports a driver.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    from dbtx.store.adapters import (
        adapter_class_by_name,
        check_adapter_serves,
        default_adapter,
        driver_package_for,
    )
    from dbtx.store.errors import StoreError

    try:
        url = make_url(setting.value)
    except ArgumentError:
        raise ConfigError(
            f"DB_URI ({setting.source}): not a valid SQLAlchemy URL "
            f"(for example sqlite:///.dbtx/dbtx.db or "
            f"postgresql+psycopg://user@host:5432/dbtx)"
        ) from None
    backend = url.get_backend_name()

    driver = uri_driver(setting, url, db_adapter, check_driver)

    if db_adapter.value is not None:
        adapter_class = adapter_class_by_name(db_adapter.value)
        try:
            check_adapter_serves(db_adapter.value, adapter_class, backend, driver)
        except StoreError as exc:
            raise ConfigError(f"DB_ADAPTER ({db_adapter.source}): {exc}") from None
    else:
        try:
            _, adapter_class = default_adapter(backend, driver)
        except StoreError as exc:
            raise ConfigError(f"DB_URI ({setting.source}): {exc}") from None

    package = driver_package_for(adapter_class, driver)
    if check_driver and package is not None:
        # find_spec rather than import: validation never imports a driver.
        if importlib.util.find_spec(package) is None:
            raise ConfigError(
                f"DB_URI ({setting.source}) is a {adapter_class.display_name} URL but "
                f"{package} is not installed; {adapter_class.install_hint}"
            )

    return adapter_class


def uri_driver(setting: Setting, url, db_adapter: Setting, check_driver: bool) -> str:
    """The driver DB_URI names, or else the default driver of its SQLAlchemy dialect.

    Without `+driver`, SQLAlchemy loads the backend's dialect to find its default
    driver, and a plugin backend's dialect may not be installed. Only that case
    (NoSuchModuleError, no `+`) becomes a ConfigError, with the hints of the one
    adapter that can serve the backend, when there is exactly one.
    """
    from sqlalchemy.exc import NoSuchModuleError

    from dbtx.store.adapters import adapters_for_backend, driver_package_for, supported_drivers
    from dbtx.store.errors import StoreError

    if "+" in url.drivername:
        return url.get_driver_name()
    try:
        return url.get_driver_name()
    except NoSuchModuleError:
        pass

    backend = url.get_backend_name()
    try:
        candidates = adapters_for_backend(backend, db_adapter.value)
    except StoreError as exc:
        raise ConfigError(f"DB_URI ({setting.source}): {exc}") from None
    if len(candidates) != 1:
        raise ConfigError(
            f"DB_URI ({setting.source}): the URI names no driver and SQLAlchemy has no "
            f"{backend!r} dialect installed to pick one; write {backend}+<driver>://"
        )

    adapter_class = candidates[0][1]
    if check_driver:
        packages = []
        for driver in supported_drivers(adapter_class):
            package = driver_package_for(adapter_class, driver)
            if package is not None and package not in packages:
                packages.append(package)
        for package in packages:
            if importlib.util.find_spec(package) is None:
                raise ConfigError(
                    f"DB_URI ({setting.source}) is a {adapter_class.display_name} URL but "
                    f"{package} is not installed; {adapter_class.install_hint}"
                )
    raise ConfigError(
        f"DB_URI ({setting.source}): the {adapter_class.display_name} URI names no "
        f"driver and SQLAlchemy has no {backend!r} dialect installed to pick one; use "
        f"{adapter_class.driver_hint}"
    )


def validate_db_schema(setting: Setting, adapter_class) -> None:
    """DB_SCHEMA must be a plain lowercase name. Setting it for a backend without
    schemas (SQLite) is an error; the default is not "setting it"."""
    if DB_SCHEMA_PATTERN.fullmatch(setting.value or "") is None:
        raise ConfigError(
            f"DB_SCHEMA ({setting.source}): {setting.value!r} is not a valid schema name; "
            f"use lowercase letters, digits and _, not starting with a digit, at most 63 "
            f"characters"
        )
    if setting.source == "default":
        return
    if adapter_class is not None and not adapter_class.supports_schemas:
        raise ConfigError(
            f"DB_SCHEMA ({setting.source}): {adapter_class.display_name} has no schemas; "
            f"remove DB_SCHEMA, or use a DB_URI whose backend has schemas"
        )


def mask_uri(uri: str) -> str:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        return make_url(uri).render_as_string(hide_password=True)
    except ArgumentError:
        return "<invalid URL>"


# --------------------------------------------------------------------------------------
# Step 7: SQLite paths
# --------------------------------------------------------------------------------------


def normalise_sqlite_path(setting: Setting) -> Setting:
    """Make a relative SQLite path absolute, against the directory of its source.

    That is the config file's directory for a value from the file, and the cwd for the
    CLI and env. In platform mode `DB_URI=sqlite:///.dbtx/dbtx.db` is therefore
    `<platform_root>/.dbtx/dbtx.db`.
    """
    from sqlalchemy.engine import make_url

    url = make_url(setting.value)
    if url.get_backend_name() != "sqlite":
        return setting
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return setting
    if Path(database).is_absolute():
        return setting
    assert setting.origin_dir is not None
    absolute = (setting.origin_dir / database).resolve()
    new_value = url.set(database=str(absolute)).render_as_string(hide_password=False)
    return Setting(new_value, setting.source, setting.origin_dir)


# --------------------------------------------------------------------------------------
# Isolated runs (plan Phase 6)
# --------------------------------------------------------------------------------------


def resolve_log_root_location(
    flag_values: dict[str, str],
    file_entries: dict[str, tuple[str, int]],
    config_file: Path,
    cwd: Path,
) -> Setting:
    """LOG_ROOT_LOCATION: CLI, then the config file, then the env, made absolute against
    the layer that set it (the config file's folder; the cwd for the CLI and the env)."""
    if FLAG_LOG_ROOT_LOCATION in flag_values:
        raw = flag_values[FLAG_LOG_ROOT_LOCATION]
        source = "cli"
        origin = cwd
    elif FILE_KEY_LOG_ROOT_LOCATION in file_entries:
        raw, line = file_entries[FILE_KEY_LOG_ROOT_LOCATION]
        source = f"file {config_file}:{line}"
        origin = config_file.parent
    elif os.environ.get(ENV_LOG_ROOT_LOCATION):
        raw = os.environ[ENV_LOG_ROOT_LOCATION]
        source = f"env {ENV_LOG_ROOT_LOCATION}"
        origin = cwd
    else:
        return Setting(None, "unset", None)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = origin / path
    return Setting(str(path.resolve()), source, origin)


def project_target_dir_for(project_dir: Path) -> Path:
    """The project's own target folder, as dbt resolves it without --target-path:
    DBT_TARGET_PATH, else `target-path` in dbt_project.yml, else `target`; relative to
    the project. Runs no longer write there: it holds the warm partial-parse cache and
    the docs site."""
    target_path = os.environ.get("DBT_TARGET_PATH")
    if not target_path:
        target_path = "target"
        try:
            data = yaml.safe_load((project_dir / DBT_PROJECT_FILE_NAME).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("target-path"), str):
            target_path = data["target-path"]
    path = Path(target_path).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path


def check_no_dbtx_job_id_in_vars(argv: list[str]) -> None:
    """In platform mode dbtx sets the job id; DBTX_JOB_ID in --vars would contradict it.
    Vars that are not valid YAML are left for dbt to report."""
    raw = last_option_value(argv, "--vars")
    if raw is None:
        return
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return
    if isinstance(parsed, dict) and DBTX_JOB_ID_VAR in parsed:
        raise ConfigError(
            f"{DBTX_JOB_ID_VAR} in --vars conflicts with platform mode: dbtx sets the job "
            f"id. Use {FLAG_JOB_ID} ID instead"
        )


def check_no_own_target_or_log_path(argv: list[str], command: str) -> None:
    """A command with a run folder writes there; a --target-path / --log-path of the
    user's own would be silently overridden, so it is refused."""
    for option in ("--target-path", "--log-path"):
        if last_option_value(argv, option) is not None:
            raise ConfigError(
                f"{option} conflicts with platform mode: `dbtx {command}` writes to its "
                f"run folder under LOG_ROOT_LOCATION"
            )


def choose_job_id(flag_values: dict[str, str]) -> str:
    """--job-id (a plain folder name), else a new one."""
    if FLAG_JOB_ID not in flag_values:
        return new_job_id()
    job_id = flag_values[FLAG_JOB_ID]
    if job_id in (".", "..") or "/" in job_id or "\\" in job_id:
        raise ConfigError(f"{FLAG_JOB_ID} {job_id!r} is not a plain folder name")
    return job_id


def run_folder_for(log_root: Path, project_dir: Path, job_id: str) -> Path:
    """`<root>/<dbt project name>/<YYYY-MM-DD>/<HH>/<job id>` (UTC). Refused when it
    already exists: every run needs its own job id."""
    project_name = read_project_name(project_dir)
    if project_name is None:
        # dbt reports the unusable name; the folder still needs one.
        project_name = project_dir.name
    started = run_started_at().astimezone(timezone.utc)
    folder = log_root / project_name / started.strftime("%Y-%m-%d") / started.strftime("%H") / job_id
    if folder.exists():
        raise ConfigError(
            f"run folder {folder} already exists: job id {job_id!r} has already run this "
            f"hour. Every run needs its own job id"
        )
    return folder


# --------------------------------------------------------------------------------------
# Step 8 (Phase 0 part): guard G1
# --------------------------------------------------------------------------------------


def read_project_name(project_dir: Path) -> Optional[str]:
    """`name:` from dbt_project.yml, rendered like dbt does when it holds Jinja.

    None when the file has no usable name; dbt reports that problem itself.
    """
    path = project_dir / DBT_PROJECT_FILE_NAME
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or name.strip() == "":
        return None
    if "{{" in name:
        name = render_project_value(name)
    return name


def render_project_value(value: str) -> Optional[str]:
    """Render a Jinja value from dbt_project.yml with dbt's own renderer.

    Outside a dbt invocation, `env_var()` needs dbt's invocation context set first.
    dbt's preflight resets that context on every invoke, so this does not leak.
    """
    from dbt.config.renderer import DbtProjectYamlRenderer
    from dbt_common.context import set_invocation_context

    set_invocation_context(dict(os.environ))
    try:
        rendered = DbtProjectYamlRenderer(None, {}).render_value(value)
    except Exception:
        return None
    return str(rendered)


def project_name_collision_message(name: str, folders: tuple[str, ...], platform_root: Path) -> str:
    """Guard G1's message: one dbt project name declared by several folders."""
    return (
        f"project name {name!r} is declared by {len(folders)} folders under "
        f"{platform_root}: {', '.join(folders)}. Each project on a platform needs "
        f"a unique name: in {DBT_PROJECT_FILE_NAME}"
    )


def check_project_name_collisions(
    platform_root: Path, project_dir: Path, warnings: list[str]
) -> tuple[Optional[str], tuple[str, ...]]:
    """Guard G1 (plan 6.5.4): one dbt project name per project folder on a platform.

    Returns the current project's dbt name (None if unreadable) and, when other folders
    declare it too, every folder declaring it (sorted). A collision between two other
    projects is only a warning, so a newly added folder cannot stop unrelated projects.
    A collision involving the current project is decided by the start-up checks,
    because the folder that holds the name in the database (guard G2) may still run.
    """
    folders_by_name: dict[str, list[str]] = {}
    for folder in project_folders(platform_root):
        name = read_project_name(platform_root / folder)
        if name is None:
            continue
        folders_by_name.setdefault(name, []).append(folder)

    current_name = read_project_name(project_dir)
    colliding_folders: tuple[str, ...] = ()

    for name, folders in sorted(folders_by_name.items()):
        if len(folders) < 2:
            continue
        message = project_name_collision_message(name, tuple(folders), platform_root)
        if name == current_name:
            colliding_folders = tuple(folders)
        else:
            warnings.append(message)

    return current_name, colliding_folders
