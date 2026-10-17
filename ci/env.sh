# shellcheck shell=bash
# Settings for ci/setup.sh, ci/build.sh and ci/run.sh (sourced, not run). Also loads the
# output helpers (ci/lib.sh).
#
# Every setting is an environment variable with a default, so a CI run can be repeated
# anywhere by setting the same variables. They all start with CICD_: dbtx warns about
# unknown DBTX_* variables, and dbt reads DBT_* ones.
#
#   CICD_ROOT           the workspace; a tmpfs is mounted there when
#                       it is not a mount point yet                   (/mnt/cicd)
#   CICD_TMPFS_SIZE     the size of that tmpfs                        (4g)
#   CICD_PYTHON         the Python version                            (3.14)
#   CICD_POSTGRES_URI   a Postgres server for the tests; unset starts
#                       a throwaway one (which cannot run as root)    (unset)

# The folder these scripts are in, whoever calls them and from wherever.
CICD_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=ci/lib.sh
source "$CICD_SCRIPTS/lib.sh"

CICD_ROOT="${CICD_ROOT:-/mnt/cicd}"
CICD_TMPFS_SIZE="${CICD_TMPFS_SIZE:-4g}"
CICD_PYTHON="${CICD_PYTHON:-3.14}"

# The repository under test: the one these scripts are in, as it is on disk (in GitHub
# Actions, the checkout; locally, your working tree, uncommitted changes included).
CICD_REPO="$(cd "$CICD_SCRIPTS/.." && pwd)"
CICD_POSTGRES_URI="${CICD_POSTGRES_URI:-}"

# The linter, pinned so a new release cannot change the result. Astral's ruff, with
# pyflakes' rules (configured in pyproject.toml, [tool.ruff.lint]).
CICD_RUFF_VERSION="0.17.0"

# The workspace layout (setup.sh creates it, build.sh uses it).
CICD_MARKER="$CICD_ROOT/.dbtx-cicd"   # says: this folder belongs to dbtx's CI
CICD_SRC="$CICD_ROOT/src"             # the copy of the repository under test
CICD_VENV="$CICD_ROOT/venv-$CICD_PYTHON"
CICD_TMP="$CICD_ROOT/tmp"             # pytest's temporary folders
CICD_OUT="$CICD_ROOT/out"             # what to keep: reports, logs, the wheel, source.txt
CICD_SMOKE="$CICD_ROOT/smoke"         # the package step's venv and scratch platform

# The test logs of a parallel run (build.sh without --sequential).
CICD_BUILD_LOGS="$CICD_OUT/logs"

# Where run.sh keeps out/ after every run, passed or failed (ignored by git): GitHub
# uploads it as the run's artifact. The workspace itself is emptied after a run that
# passed, and left as it is after one that failed.
CICD_KEEP="$CICD_REPO/.build"

# uv: the project's venv lives in the workspace. uv's download cache stays on disk;
# packages are copied from it, because a tmpfs workspace cannot hardlink to it.
export UV_PROJECT_ENVIRONMENT="$CICD_VENV"
export UV_LINK_MODE=copy

# dbt: no usage tracking (it makes network calls on every invocation).
export DBT_SEND_ANONYMOUS_USAGE_STATS=false
