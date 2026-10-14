"""The run folder at run time (plan Phase 6).

In platform mode every command that runs in a run folder (`config.run_folder`, from
`dbtx.config`) gets, once the configuration and the start-up checks have passed:

* its folder, `<LOG_ROOT_LOCATION>/<project>/<YYYY-MM-DD>/<HH>/<job id>/`, newly created
  (dbt writes its artifacts and log there: dbtx hands it `--target-path` and
  `--log-path`)
* the project's warm partial-parse cache (`dbtx warm-cache`) copied in, so dbt parses
  only what changed
* its job id, visible to dbt as `DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID` while it runs (dbt
  records it in run_results.json as `metadata.env.DBTX_JOB_ID`)
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from dbtx.config import DbtxConfig

# How dbt receives dbtx's job id: dbt records DBT_ENV_CUSTOM_ENV_* variables in
# run_results.json (`metadata.env`, without the prefix), and they do not invalidate its
# partial-parse cache.
JOB_ID_ENV_VAR = "DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID"

# dbt's partial-parse cache, in a target folder.
PARTIAL_PARSE_FILE_NAME = "partial_parse.msgpack"


class RunFolderError(Exception):
    """The run folder cannot be prepared. The command exits 2 before dbt starts."""


def prepare_run_folder(config: DbtxConfig) -> None:
    """Create the run folder and copy the project's warm cache into it."""
    assert config.run_folder is not None and config.job_id is not None
    folder = config.run_folder
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # Another run with the same job id got here first.
        raise RunFolderError(
            f"run folder {folder} already exists: job id {config.job_id!r} has already run "
            f"this hour. Every run needs its own job id"
        ) from None
    except OSError as exc:
        raise RunFolderError(f"cannot create the run folder {folder}: {exc}") from None

    if config.project_target_dir is None:
        return
    cache = config.project_target_dir / PARTIAL_PARSE_FILE_NAME
    if not cache.is_file():
        return
    # Copied under a temporary name, then renamed: dbt never finds a half-copied cache.
    temporary = folder / (PARTIAL_PARSE_FILE_NAME + ".tmp")
    shutil.copyfile(cache, temporary)
    os.replace(temporary, folder / PARTIAL_PARSE_FILE_NAME)


@contextmanager
def job_id_for_dbt(job_id: Optional[str]) -> Iterator[None]:
    """While the block runs, dbt sees `job_id` as DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID;
    afterwards the environment is as it was. No job id: nothing changes."""
    if job_id is None:
        yield
        return
    # dbt reads DBT_ENV_CUSTOM_ENV_* once per process and caches them; reset its cache on
    # the way in and out, so every command records its own job id.
    from dbt_common.events.functions import reset_metadata_vars

    previous = os.environ.get(JOB_ID_ENV_VAR)
    os.environ[JOB_ID_ENV_VAR] = job_id
    reset_metadata_vars()
    try:
        yield
    finally:
        if previous is None:
            del os.environ[JOB_ID_ENV_VAR]
        else:
            os.environ[JOB_ID_ENV_VAR] = previous
        reset_metadata_vars()


# --------------------------------------------------------------------------------------
# After the run: the docs site (the project's own target folder)
# --------------------------------------------------------------------------------------

# Platform commands that execute nodes: afterwards their rendered reports are copied
# into the docs site and the site is patched with the run.
COMMANDS_THAT_EXECUTE_NODES = ("build", "run", "test", "seed", "snapshot")

# The files of a generated docs site: the page loads the two JSON files beside it.
DOCS_SITE_FILES = ("index.html", "manifest.json", "catalog.json")

REPORTS_FOLDER = "reports"


def update_docs_site(config: DbtxConfig) -> tuple[list[str], list[str]]:
    """After a node-executing run: copy its `reports/` into the docs site, then patch
    the site with the run. Returns (lines to print, warnings). Never raises: the run's
    exit code is dbt's."""
    assert config.run_folder is not None and config.project_target_dir is not None
    run_folder = config.run_folder
    site = config.project_target_dir
    lines: list[str] = []
    warnings: list[str] = []

    reports = run_folder / REPORTS_FOLDER
    if reports.is_dir():
        try:
            shutil.copytree(reports, site / REPORTS_FOLDER, dirs_exist_ok=True)
        except OSError as exc:
            warnings.append(f"the reports in {reports} were not copied to {site / REPORTS_FOLDER}: {exc}")

    if not (site / "index.html").is_file() or not (site / "manifest.json").is_file():
        lines.append(
            f"dbtx docs: no docs site in {site} to patch; run `dbtx docs generate`, then "
            f"`dbtx docs install`"
        )
        return lines, warnings
    if not (run_folder / "run_results.json").is_file():
        lines.append(f"dbtx docs: no run_results.json in {run_folder}; the docs site was not patched")
        return lines, warnings

    from dbtx.docs.patch import patch

    try:
        patch(docs_loc=site, run_loc=run_folder)
    except Exception as exc:
        warnings.append(f"the docs site {site} was not patched: {first_line(exc)}")
        return lines, warnings
    lines.append(f"dbtx docs: patched the docs site {site} with this run")
    return lines, warnings


def publish_docs_site(config: DbtxConfig) -> list[str]:
    """After a successful `docs generate`: copy the site's files from the run folder into
    the project's docs site. Returns the lines to print."""
    assert config.run_folder is not None and config.project_target_dir is not None
    site = config.project_target_dir
    site.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in DOCS_SITE_FILES:
        source = config.run_folder / name
        if source.is_file():
            # Each file whole or not at all: the site may be being served.
            temporary = site / (name + ".tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, site / name)
            copied.append(name)
    if len(copied) == 0:
        return []
    return [
        f"dbtx docs: copied {', '.join(copied)} to {site}; run `dbtx docs install` to add "
        f"dbtx's overlay"
    ]


def first_line(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    if len(lines) == 0:
        return type(exc).__name__
    return lines[0]
