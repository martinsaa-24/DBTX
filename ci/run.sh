#!/usr/bin/env bash
# The whole CI run, as GitHub runs it (settings: ci/env.sh):
#
#   1. ci/setup.sh   mount a tmpfs at CICD_ROOT if needed, clear it, copy the repository
#                    into it as it is on disk, install the dependencies
#   2. ci/build.sh   lint, test and package, against that copy
#   3. keep out/ (reports, logs, the wheel) in .build/ at the repository root, whatever
#      happened: GitHub uploads it as the run's artifact
#   4. passed: empty CICD_ROOT. Failed: leave it as it is, to look into
#
# Locally, for example:
#
#   ci/run.sh                                    your working tree, Python 3.14
#   ci/run.sh --sequential                       unit, then live tests, on the console
#   CICD_PYTHON=3.10 ci/run.sh                   ... on Python 3.10
#
# By default the unit and live tests run in parallel, each logging to out/logs/<name>.log;
# --sequential runs them one after the other, to the console.

# Started with `sh ci/run.sh`? sh is often not bash (dash on Debian / Ubuntu), and these
# scripts need bash: start again under bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail
# shellcheck source=ci/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# keep_artifacts: replace .build/ with a copy of the workspace's out/. Only from a
# workspace setup.sh prepared (it has the marker): a refused CICD_ROOT is not ours to read.
keep_artifacts() {
    header "keep the artifacts in $CICD_KEEP"
    rm -rf "$CICD_KEEP" || fail "cannot remove the previous $CICD_KEEP"
    mkdir -p "$CICD_KEEP" || fail "cannot create $CICD_KEEP"
    if [ ! -f "$CICD_MARKER" ] || [ ! -d "$CICD_OUT" ]; then
        warn "no $CICD_OUT to keep"
        return
    fi
    cp -a "$CICD_OUT/." "$CICD_KEEP/" || fail "cannot copy $CICD_OUT to $CICD_KEEP"
    ok "kept $CICD_OUT in $CICD_KEEP"
}

# Each script is its own process and prints its own error; `||` here only records which
# one stopped the run, so the artifacts are kept either way.
# setup.sh takes no arguments; build.sh gets this script's (for example --sequential).
result="passed"
"$CICD_SCRIPTS/setup.sh" || result="ci/setup.sh failed (ci/build.sh was not run)"
if [ "$result" = "passed" ]; then
    "$CICD_SCRIPTS/build.sh" "$@" || result="ci/build.sh failed"
fi

keep_artifacts

if [ "$result" != "passed" ]; then
    fail "$result; $CICD_ROOT is left as it is, to look into (artifacts: $CICD_KEEP)"
fi

header "clean up $CICD_ROOT"
clear_workspace
ok "CI passed; emptied $CICD_ROOT (artifacts: $CICD_KEEP)"
