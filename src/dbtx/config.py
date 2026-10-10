"""dbtx configuration: loaded and validated before anything else a command does.

See docs/agentic/orm_implementation_plan.md, section 6. In short:

* **Modes.** Platform mode is the default. The platform root is `--platform-dir DIR`,
  or else the current directory when it holds `dbtx.conf` and is not itself a dbt
  project. `--standalone-mode` opts out: no config file, no database, no platform
  checks.
* **Layers** (platform mode only), highest first: CLI flags, the config file, then
  `DBTX_*` environment variables. `.env` files are never read.
* **Options:** `DB_BACKEND` (`enabled` | `disabled`, default `enabled`) and `DB_URI`
  (required when the backend is enabled and the command uses the database).

`load_config(argv)` either returns a `DbtxConfig` or raises `ConfigError`. Callers turn
a `ConfigError` into exit code 2 before dbt is ever started.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# Until recording exists (plan Phase 5), the failures that only protect the database
# are printed as warnings instead of exiting 2: a missing DB_URI, a project-name
# collision (guard G1) and "no platform found". Tests patch this to True. It is
# deliberately not an option or env var, so it never becomes user-facing config.
ENFORCE_DB_REQUIREMENTS = False

CONFIG_FILE_NAME = "dbtx.conf"
DBT_PROJECT_FILE_NAME = "dbt_project.yml"

# dbtx's own command-line flags. They are removed from argv before dbt sees it.
FLAG_DB_BACKEND = "--db-backend"
FLAG_DB_URI = "--db-uri"
FLAG_CONFIG_FILE = "--config-file"
FLAG_PLATFORM_DIR = "--platform-dir"
FLAG_STANDALONE_MODE = "--standalone-mode"

FLAGS_WITH_A_VALUE = (FLAG_DB_BACKEND, FLAG_DB_URI, FLAG_CONFIG_FILE, FLAG_PLATFORM_DIR)

# Keys allowed in dbtx.conf.
FILE_KEY_DB_BACKEND = "DB_BACKEND"
FILE_KEY_DB_URI = "DB_URI"
FILE_KEYS = (FILE_KEY_DB_BACKEND, FILE_KEY_DB_URI)

# Environment variables dbtx reads.
ENV_DB_BACKEND = "DBTX_DB_BACKEND"
ENV_DB_URI = "DBTX_DB_URI"
ENV_CONFIG_FILE = "DBTX_CONFIG_FILE"

# Every DBTX_* name dbtx knows about. Any other DBTX_* variable gets a warning, the env
# counterpart of the strict keys in dbtx.conf. DBTX_DB_STRICT and DBTX_TEST_POSTGRES_URL
# are test switches, read elsewhere.
KNOWN_ENV_VARS = (
    ENV_DB_BACKEND,
    ENV_DB_URI,
    ENV_CONFIG_FILE,
    "DBTX_DB_STRICT",
    "DBTX_TEST_POSTGRES_URL",
)

DB_BACKEND_ENABLED = "enabled"
DB_BACKEND_DISABLED = "disabled"

MODE_PLATFORM = "platform"
MODE_STANDALONE = "standalone"

SUPPORTED_BACKENDS = ("sqlite", "postgresql")

# Commands that use the database: the commands the store records (plan 5.2) and the
# `dbtx db` commands that change it. In platform mode with the backend enabled they
# require DB_URI.
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
    "db init",
    "db upgrade",
    "db load",
)

# Commands that report on the database. They never fail because DB_URI is missing.
COMMANDS_THAT_REPORT_ON_THE_DATABASE = ("config", "db status")

# Commands dbtx implements itself, rather than handing them to dbt.
DBTX_COMMANDS = (
    "templates",
    "config",
    "docs patch",
    "docs install",
    "docs status",
    "docs serve",
    "db init",
    "db upgrade",
    "db status",
    "db load",
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
)

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
    # None in standalone mode, where database options do not exist.
    db_backend: Optional[Setting]
    db_uri: Optional[Setting]
    command: str
    uses_database: bool
    # The argv to hand on: dbtx flags removed and, in platform mode, the absolute
    # --project-dir / --profiles-dir added.
    argv: tuple[str, ...]
    warnings: tuple[str, ...]
    # DBTX_* variables that were set but ignored (standalone mode).
    ignored_env_vars: tuple[str, ...]

    @property
    def is_platform(self) -> bool:
        return self.mode == MODE_PLATFORM

    @property
    def backend_enabled(self) -> bool:
        return self.db_backend is not None and self.db_backend.value == DB_BACKEND_ENABLED

    def masked_db_uri(self) -> Optional[str]:
        if self.db_uri is None or self.db_uri.value is None:
            return None
        return mask_uri(self.db_uri.value)


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
    ) = decide_mode(flag_values, standalone_requested, cwd, is_help, warnings)

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
    if not is_help and command_needs_a_project(command):
        project_dir = resolve_platform_project_dir(rest, platform_root, cwd)
        check_no_project_config_file(project_dir, platform_root)
        if command_is_handed_to_dbt(command):
            check_profiles_dir(rest, project_dir, cwd)
            rest = inject_project_and_profiles_dir(rest, project_dir)
        elif command == "templates":
            rest = inject_project_dir_only(rest, project_dir)

    config_file, config_file_reason = locate_config_file(flag_values, platform_root, cwd)
    file_entries = parse_config_file(config_file)

    # Steps 3-4: read the env and resolve each option by precedence.
    db_backend = resolve_db_backend(flag_values, file_entries, config_file, cwd)
    db_uri = resolve_db_uri(flag_values, file_entries, config_file, cwd)

    # Step 5: validate the values.
    db_backend = validate_db_backend(db_backend)
    backend_enabled = db_backend.value == DB_BACKEND_ENABLED
    if db_uri.value is not None:
        validate_db_uri(db_uri, check_driver=backend_enabled)

    # Step 6: the requirement check, behind its gates.
    uses_database = command in COMMANDS_THAT_USE_THE_DATABASE
    if backend_enabled and uses_database and db_uri.value is None:
        message = (
            f"`dbtx {command}` needs a database: set DB_URI (with --db-uri, "
            f"DB_URI= in {config_file}, or {ENV_DB_URI}), pass --db-backend disabled, "
            f"or pass --standalone-mode"
        )
        if ENFORCE_DB_REQUIREMENTS:
            raise ConfigError(message)
        warnings.append(message + " (not enforced yet: nothing is recorded)")

    # Step 7: make a relative SQLite path absolute.
    if db_uri.value is not None:
        db_uri = normalise_sqlite_path(db_uri)

    # Step 8 (Phase 0 part): guard G1.
    if backend_enabled and uses_database and project_dir is not None:
        check_project_name_collisions(platform_root, project_dir, warnings)

    return DbtxConfig(
        mode=MODE_PLATFORM,
        mode_reason=mode_reason,
        platform_root=platform_root,
        project_dir=project_dir,
        config_file=config_file,
        config_file_reason=config_file_reason,
        db_backend=db_backend,
        db_uri=db_uri,
        command=command,
        uses_database=uses_database,
        argv=tuple(rest),
        warnings=tuple(warnings),
        ignored_env_vars=(),
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
    return "-h" in argv or "--help" in argv or "--version" in argv


# --------------------------------------------------------------------------------------
# Step 2: mode
# --------------------------------------------------------------------------------------


def decide_mode(
    flag_values: dict[str, str],
    standalone_requested: bool,
    cwd: Path,
    is_help: bool,
    warnings: list[str],
) -> tuple[str, str, Optional[Path]]:
    """Returns (mode, reason, platform root). First match wins (plan 6.5.1)."""

    # Rule 0: --standalone-mode.
    if standalone_requested:
        if FLAG_PLATFORM_DIR in flag_values:
            raise ConfigError(
                f"{FLAG_STANDALONE_MODE} and {FLAG_PLATFORM_DIR} contradict each other"
            )
        for flag in (FLAG_DB_BACKEND, FLAG_DB_URI):
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
    if ENFORCE_DB_REQUIREMENTS:
        raise ConfigError(message)
    warnings.append(message + ". Continuing in standalone mode (not enforced yet)")
    for flag in (FLAG_DB_BACKEND, FLAG_DB_URI, FLAG_CONFIG_FILE):
        if flag in flag_values:
            warnings.append(f"{flag} is ignored in standalone mode")
    return MODE_STANDALONE, "no platform found (fallback, not enforced yet)", None


def standalone_config(
    mode_reason: str, rest: list[str], command: str, warnings: list[str]
) -> DbtxConfig:
    """Standalone mode: no config file, no database options, no platform checks."""
    ignored = []
    for name in (ENV_DB_BACKEND, ENV_DB_URI, ENV_CONFIG_FILE):
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
        command=command,
        uses_database=False,
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


def validate_db_uri(setting: Setting, check_driver: bool) -> None:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        url = make_url(setting.value)
    except ArgumentError:
        raise ConfigError(
            f"DB_URI ({setting.source}): not a valid SQLAlchemy URL "
            f"(for example sqlite:///.dbtx/dbtx.db or "
            f"postgresql+psycopg://user@host:5432/dbtx)"
        ) from None

    backend = url.get_backend_name()
    if backend not in SUPPORTED_BACKENDS:
        raise ConfigError(
            f"DB_URI ({setting.source}): unsupported backend {backend!r}; supported: "
            f"{', '.join(SUPPORTED_BACKENDS)}"
        )

    driver = url.get_driver_name()
    if backend == "sqlite" and driver != "pysqlite":
        raise ConfigError(
            f"DB_URI ({setting.source}): unsupported SQLite driver {driver!r}; use "
            f"sqlite:/// (the standard library driver)"
        )
    if backend == "postgresql" and driver != "psycopg":
        raise ConfigError(
            f"DB_URI ({setting.source}): unsupported Postgres driver {driver!r}; use "
            f"postgresql+psycopg:// (psycopg 3)"
        )

    if check_driver and backend == "postgresql":
        # find_spec rather than import: validation never imports a driver.
        if importlib.util.find_spec("psycopg") is None:
            raise ConfigError(
                f"DB_URI ({setting.source}) is a Postgres URL but psycopg is not "
                f"installed; install dbtx[postgres]"
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


def check_project_name_collisions(
    platform_root: Path, project_dir: Path, warnings: list[str]
) -> None:
    """Guard G1 (plan 6.5.4): one dbt project name per project folder on a platform.

    A collision involving the current project is fatal (once enforced). A collision
    between two other projects is only a warning, so a newly added folder cannot stop
    unrelated projects.
    """
    folders_by_name: dict[str, list[str]] = {}
    for folder in project_folders(platform_root):
        name = read_project_name(platform_root / folder)
        if name is None:
            continue
        folders_by_name.setdefault(name, []).append(folder)

    current_name = read_project_name(project_dir)

    for name, folders in sorted(folders_by_name.items()):
        if len(folders) < 2:
            continue
        message = (
            f"project name {name!r} is declared by {len(folders)} folders under "
            f"{platform_root}: {', '.join(folders)}. Each project on a platform needs "
            f"a unique name: in {DBT_PROJECT_FILE_NAME}"
        )
        if name == current_name:
            if ENFORCE_DB_REQUIREMENTS:
                raise ConfigError(message)
            warnings.append(message + " (not enforced yet)")
        else:
            warnings.append(message)
