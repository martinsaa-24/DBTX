# shellcheck shell=bash
# Helpers for the CI scripts (sourced by ci/env.sh, not run): output, and
# `clear_workspace` (at the end).
#
#   header "lint"                 a step's title, bold
#   info   "copied 412 files"     a progress line
#   ok     "lint passed"          a success, green
#   warn   "no warm cache"        something to notice, yellow
#   fail   "uv build failed"      an error, red, then exit 1
#
# How the scripts handle errors (see docs/contributing/ci.md):
#   * every command is followed by `|| fail "<what failed>"`, so a failure always says
#     which step and which command it was
#   * `set -euo pipefail` stays on as the backstop for a command that lacks its check
#   * steps are called plainly, never inside `if`, `||`, `&&` or `!`: there bash turns
#     `set -e` off for every command inside the function
#   * never `local x=$(cmd)`: `local` succeeds and hides cmd's failure. Declare first,
#     then `x=$(cmd) || fail "..."`

# Colors: on in a terminal and in GitHub Actions (which renders them), off when the output
# goes to a file or NO_COLOR is set (https://no-color.org).
if [ -z "${NO_COLOR:-}" ] && { [ -t 1 ] || [ "${GITHUB_ACTIONS:-}" = "true" ]; }; then
    CI_BOLD=$'\e[1m'
    CI_RED=$'\e[31m'
    CI_GREEN=$'\e[32m'
    CI_YELLOW=$'\e[33m'
    CI_CYAN=$'\e[36m'
    CI_RESET=$'\e[0m'
else
    CI_BOLD=""
    CI_RED=""
    CI_GREEN=""
    CI_YELLOW=""
    CI_CYAN=""
    CI_RESET=""
fi

# header <title>: starts a step, so the output shows where each step begins (and, when
# something fails, which step it was in).
header() {
    echo
    echo "${CI_BOLD}${CI_CYAN}==> $1${CI_RESET}"
}

# info <message>: a progress line inside a step.
info() {
    echo "    $1"
}

# ok <message>: a step or the whole run succeeded.
ok() {
    echo "${CI_GREEN}ok: $1${CI_RESET}"
}

# warn <message>: worth noticing, not an error (on stderr).
warn() {
    echo "${CI_YELLOW}warning: $1${CI_RESET}" >&2
}

# fail <message>: an error. Prints it (on stderr) and stops the script with exit code 1.
fail() {
    echo "${CI_RED}${CI_BOLD}FAILED: $1${CI_RESET}" >&2
    exit 1
}

# clear_workspace: empty CICD_ROOT, keeping the folder (it is a mount point). Used before
# a run (ci/setup.sh) and after one that passed (ci/run.sh).
#
# The only function that deletes, so it refuses anything that does not look like dbtx's
# CI workspace: a relative path or `/`, a folder containing the repository under test,
# and a non-empty folder without the marker file (for example a mistyped CICD_ROOT).
clear_workspace() {
    # `/` (or `//`), and relative paths, are never a workspace.
    if [ -z "${CICD_ROOT%/}" ]; then
        fail "refusing CICD_ROOT='$CICD_ROOT': it is the filesystem root"
    fi
    case "$CICD_ROOT" in
        /*) ;;
        *) fail "refusing CICD_ROOT='$CICD_ROOT': not an absolute path" ;;
    esac

    # Compare real paths (no symlinks, no `..`): the repository must not be inside.
    local root
    local repo
    root="$(realpath -m "$CICD_ROOT")" || fail "cannot resolve CICD_ROOT='$CICD_ROOT'"
    repo="$(realpath -m "$CICD_REPO")" || fail "cannot resolve the repository '$CICD_REPO'"
    case "$repo/" in
        "$root"/*) fail "refusing to clear $root: it contains the repository $repo" ;;
    esac

    # A folder with something in it must carry our marker. `ls` runs on its own line, so
    # a failure to list it stops here instead of looking like an empty folder.
    local contents
    contents="$(ls -A "$root")" || fail "cannot list $root"
    if [ -n "$contents" ] && [ ! -f "$root/.dbtx-cicd" ]; then
        fail "refusing to clear $root: it is not empty and has no .dbtx-cicd marker (not created by dbtx's CI). Empty it yourself, or set CICD_ROOT to another folder"
    fi

    find "$root" -mindepth 1 -delete || fail "cannot empty $root"
}
