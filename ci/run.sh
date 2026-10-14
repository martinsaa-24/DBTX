#!/usr/bin/env bash
# The whole CI run, as GitHub runs it: ci/setup.sh, then ci/build.sh (settings:
# ci/env.sh). Locally, for example:
#
#   ci/run.sh                                    the committed HEAD, Python 3.14
#   ci/run.sh --sequential                       unit, then live tests, on the console
#   CICD_WORKING_TREE=1 ci/run.sh                ... with your uncommitted changes
#   CICD_PYTHON=3.10 ci/run.sh                   ... on Python 3.10
#
# By default the unit and live tests run in parallel, each logging to .build/<name>.log
# at the repository root; --sequential runs them one after the other, to the console.

# Started with `sh ci/run.sh`? sh is often not bash (dash on Debian / Ubuntu), and these
# scripts need bash: start again under bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail
# shellcheck source=ci/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Each script prints its own error; these only say which one stopped the run.
# setup.sh takes no arguments; build.sh gets this script's (for example --sequential).
"$CICD_SCRIPTS/setup.sh" || fail "ci/setup.sh failed; ci/build.sh was not run"
"$CICD_SCRIPTS/build.sh" "$@" || fail "ci/build.sh failed"
