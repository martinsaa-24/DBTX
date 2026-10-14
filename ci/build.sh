#!/usr/bin/env bash
# Lint, test and package the workspace that ci/setup.sh prepared (settings: ci/env.sh).
#
#   ci/build.sh                    unit and live tests in parallel, logged to .build/
#   ci/build.sh --sequential       unit, then live tests, on the console (no log files)
#
# Four checks; the first failure stops the run:
#   lint      ruff (pyflakes' rules) over src, tests and scripts
#   unit      pytest -m "not live": the unit tests, Postgres ones included
#   live      pytest -m live: real dbt runs of the example project
#   package   build the wheel, install it into a fresh venv, and run that dbtx on a
#             scratch platform: proves the wheel ships the migrations and the schema
#
# By default unit and live run at the same time (they share nothing they write: separate
# temporary folders, reports and processes), each writing its output to
# .build/unit.log and .build/live.log at the repository root. --sequential runs them one
# after the other, with their output on the console.
#
# To re-run one check, run its command by hand in $CICD_ROOT/src (the commands are
# below). JUnit reports and the wheel go to $CICD_ROOT/out.
#
# Errors: every command is checked with `|| fail "..."` (ci/lib.sh explains why).

# Started with `sh`? sh is often not bash: start again under bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail
# shellcheck source=ci/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Arguments: only --sequential.
CICD_SEQUENTIAL=0
for argument in "$@"; do
    case "$argument" in
        --sequential) CICD_SEQUENTIAL=1 ;;
        *) fail "unknown argument '$argument' (the only one is --sequential)" ;;
    esac
done

# check_workspace: ci/setup.sh must have run first.
check_workspace() {
    if [ ! -f "$CICD_MARKER" ] || [ ! -d "$CICD_SRC" ]; then
        fail "$CICD_ROOT is not set up; run ci/setup.sh first"
    fi
}

# clear_dbt_settings: the developer's shell must not change the result. Removes every
# DBTX_* variable and dbt's location / selection variables (the anonymous-usage switch
# set in ci/env.sh stays).
clear_dbt_settings() {
    local name
    for name in $(compgen -e); do
        case "$name" in
            DBTX_* | DBT_PROJECT_DIR | DBT_PROFILES_DIR | DBT_TARGET_PATH | DBT_LOG_PATH | DBT_TARGET | DBT_PROFILE)
                unset "$name" || fail "cannot unset $name"
                info "ignoring $name from the environment"
                ;;
        esac
    done
}

# run_lint: ruff, pinned in ci/env.sh, with the rules in pyproject.toml.
run_lint() {
    header "lint"
    local started=$SECONDS
    uvx "ruff@$CICD_RUFF_VERSION" check --no-cache "$CICD_SRC/src" "$CICD_SRC/tests" "$CICD_SRC/scripts" \
        || fail "lint: ruff found problems (above)"
    ok "lint passed ($((SECONDS - started))s)"
}

# run_pytest <name> <pytest arguments...>: runs pytest in the clone, with its temporary
# folders under $CICD_ROOT/tmp/<name> and a JUnit report at $CICD_ROOT/out/<name>.xml.
# Uses CICD_POSTGRES_URI when set; otherwise the tests start a throwaway Postgres.
#
# The caller passes the verbosity with the other arguments: -q (one dot per test) on the
# console, -v (one line per test, with its name) in the parallel runs' log files.
#
# `--no-sync`: setup.sh installed everything, and two runs in parallel must not both
# check and update the same venv at once.
run_pytest() {
    local name="$1"
    shift
    local options=(-p no:cacheprovider --basetemp "$CICD_TMP/$name" --junitxml "$CICD_OUT/$name.xml")
    if [ -n "$CICD_POSTGRES_URI" ]; then
        options+=(--postgres-uri "$CICD_POSTGRES_URI")
    fi
    uv --directory "$CICD_SRC" run --no-sync pytest "$@" "${options[@]}" \
        || fail "$name: tests failed (report: $CICD_OUT/$name.xml)"
}

# run_unit_tests: everything not marked live (about a minute).
run_unit_tests() {
    header "unit tests"
    local started=$SECONDS
    run_pytest unit -q -m "not live"
    ok "unit tests passed ($((SECONDS - started))s)"
}

# run_live_tests: real dbt runs of the example project, and the frozen-fixture checks.
run_live_tests() {
    header "live tests"
    local started=$SECONDS
    run_pytest live -q -m live
    ok "live tests passed ($((SECONDS - started))s)"
}

# show_test_result <name> <exit status>: after a parallel run, one line per test run:
# pytest's summary when it passed; when it failed, the end of its log (where pytest
# lists the failures) and where the whole log is.
show_test_result() {
    local name="$1"
    local status="$2"
    local log="$CICD_BUILD_LOGS/$name.log"
    local summary
    summary="$(tail -n 1 "$log")" || fail "$name: cannot read $log"
    if [ "$status" -eq 0 ]; then
        ok "$name tests passed: $summary"
    else
        echo "${CI_RED}$name tests FAILED (exit $status); the end of $log:${CI_RESET}" >&2
        tail -n 40 "$log" >&2 || fail "$name: cannot read $log"
    fi
}

# run_tests_in_parallel: unit and live tests at the same time, each in the background
# with its output in .build/<name>.log; waits for both, then reports both (so two
# failures are both shown) before failing.
run_tests_in_parallel() {
    header "unit and live tests (in parallel)"
    local started=$SECONDS

    # Fresh logs for this run.
    mkdir -p "$CICD_BUILD_LOGS" || fail "cannot create $CICD_BUILD_LOGS"
    rm -f "$CICD_BUILD_LOGS/unit.log" "$CICD_BUILD_LOGS/live.log" \
        || fail "cannot remove the previous logs in $CICD_BUILD_LOGS"
    info "logs: $CICD_BUILD_LOGS/unit.log, $CICD_BUILD_LOGS/live.log"

    # `&` runs each in the background; `$!` is its process id. (Not inside if / ||: set -e
    # stays on inside each run, see ci/lib.sh.)
    # -v: one line per test in the log, so it shows what ran, what failed and how far a
    # run has got.
    run_pytest unit -v -m "not live" > "$CICD_BUILD_LOGS/unit.log" 2>&1 &
    local unit_pid=$!
    run_pytest live -v -m live > "$CICD_BUILD_LOGS/live.log" 2>&1 &
    local live_pid=$!

    # `wait <pid>` returns that run's exit status: 0 passed, anything else failed.
    local unit_status=0
    local live_status=0
    wait "$unit_pid" || unit_status=$?
    wait "$live_pid" || live_status=$?

    show_test_result unit "$unit_status"
    show_test_result live "$live_status"
    if [ "$unit_status" -ne 0 ] || [ "$live_status" -ne 0 ]; then
        fail "tests failed (logs: $CICD_BUILD_LOGS)"
    fi
    ok "unit and live tests passed ($((SECONDS - started))s)"
}

# check_package: build the wheel, install it alone into a fresh venv, and run that dbtx
# against a scratch platform (db init creates the tables from the migrations shipped in
# the wheel).
check_package() {
    header "package"
    local started=$SECONDS

    rm -rf "$CICD_OUT/dist" "$CICD_SMOKE" || fail "package: cannot remove the previous wheel and smoke test"
    uv --directory "$CICD_SRC" build --wheel --out-dir "$CICD_OUT/dist" \
        || fail "package: building the wheel failed"

    # Exactly one wheel is expected.
    local wheels=("$CICD_OUT"/dist/*.whl)
    if [ "${#wheels[@]}" -ne 1 ] || [ ! -f "${wheels[0]}" ]; then
        fail "package: expected one wheel in $CICD_OUT/dist, found: ${wheels[*]}"
    fi
    info "built $(basename "${wheels[0]}")"

    uv venv --quiet --python "$CICD_PYTHON" "$CICD_SMOKE/venv" \
        || fail "package: creating the smoke-test venv failed"
    uv pip install --quiet --python "$CICD_SMOKE/venv/bin/python" "${wheels[0]}" \
        || fail "package: installing the wheel failed"

    # A scratch platform for the installed dbtx: a SQLite database and run folders.
    local dbtx="$CICD_SMOKE/venv/bin/dbtx"
    local platform="$CICD_SMOKE/platform"
    mkdir -p "$platform" || fail "package: cannot create $platform"
    printf 'DB_URI=sqlite:///.dbtx/dbtx.db\nLOG_ROOT_LOCATION=.dbtx/logs\n' > "$platform/dbtx.conf" \
        || fail "package: cannot write $platform/dbtx.conf"

    "$dbtx" --help > /dev/null || fail "package: the installed dbtx --help failed"
    "$dbtx" db init --platform-dir "$platform" || fail "package: the installed dbtx db init failed"
    "$dbtx" db status --platform-dir "$platform" || fail "package: the installed dbtx db status failed"
    "$dbtx" config --platform-dir "$platform" || fail "package: the installed dbtx config failed"
    ok "package passed ($((SECONDS - started))s)"
}

# The steps, called plainly (never inside if / || / && conditions: see ci/lib.sh; inside
# the then / else branches below they run with set -e on, as usual).
check_workspace
clear_dbt_settings
run_lint
if [ "$CICD_SEQUENTIAL" = "1" ]; then
    run_unit_tests
    run_live_tests
else
    run_tests_in_parallel
fi
check_package
echo
ok "all CI steps passed (${SECONDS}s)"
