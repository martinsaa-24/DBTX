# shellcheck shell=bash
# Settings for ci/setup.sh, ci/build.sh and ci/run.sh (sourced, not run). Also loads the
# output helpers (ci/lib.sh).
#
# Every setting is an environment variable with a default, so a CI run can be repeated
# anywhere by setting the same variables. They all start with CICD_: dbtx warns about
# unknown DBTX_* variables, and dbt reads DBT_* ones.
#
#   CICD_ROOT           the workspace; emptied by setup.sh            (/mnt/cicd)
#   CICD_REPO           the git repository to test, a local path      (this repository)
#   CICD_REF            the commit to test                            (HEAD)
#   CICD_WORKING_TREE   1: also copy the repository's uncommitted
#                       changes (only with CICD_REF=HEAD)             (0)
#   CICD_PYTHON         the Python version                            (3.14)
#   CICD_POSTGRES_URI   a Postgres server for the tests; unset starts
#                       a throwaway one (which cannot run as root)    (unset)

# The folder these scripts are in, whoever calls them and from wherever.
CICD_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=ci/lib.sh
source "$CICD_SCRIPTS/lib.sh"

CICD_ROOT="${CICD_ROOT:-/mnt/cicd}"
CICD_REPO="${CICD_REPO:-$(cd "$CICD_SCRIPTS/.." && pwd)}"
CICD_REF="${CICD_REF:-HEAD}"
CICD_WORKING_TREE="${CICD_WORKING_TREE:-0}"
CICD_PYTHON="${CICD_PYTHON:-3.14}"
CICD_POSTGRES_URI="${CICD_POSTGRES_URI:-}"

# The linter, pinned so a new release cannot change the result. Astral's ruff, with
# pyflakes' rules (configured in pyproject.toml, [tool.ruff.lint]).
CICD_RUFF_VERSION="0.17.0"

# The workspace layout (setup.sh creates it, build.sh uses it).
CICD_MARKER="$CICD_ROOT/.dbtx-cicd"   # says: this folder belongs to dbtx's CI
CICD_SRC="$CICD_ROOT/src"             # the clone under test
CICD_VENV="$CICD_ROOT/venv-$CICD_PYTHON"
CICD_TMP="$CICD_ROOT/tmp"             # pytest's temporary folders
CICD_OUT="$CICD_ROOT/out"             # what to keep: reports, the wheel, source.txt
CICD_SMOKE="$CICD_ROOT/smoke"         # the package step's venv and scratch platform

# The test logs of a parallel run (build.sh without --sequential): .build/ at the root of
# the repository the scripts are in (ignored by git), whichever folder they are run from.
CICD_BUILD_LOGS="$(cd "$CICD_SCRIPTS/.." && pwd)/.build"

# uv: the project's venv lives in the workspace. uv's download cache stays on disk;
# packages are copied from it, because a tmpfs workspace cannot hardlink to it.
export UV_PROJECT_ENVIRONMENT="$CICD_VENV"
export UV_LINK_MODE=copy

# dbt: no usage tracking (it makes network calls on every invocation).
export DBT_SEND_ANONYMOUS_USAGE_STATS=false
