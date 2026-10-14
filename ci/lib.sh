# shellcheck shell=bash
# Output helpers for the CI scripts (sourced by ci/env.sh, not run).
#
#   header "lint"                 a step's title, bold
#   info   "cloned 5eacb79"       a progress line
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
