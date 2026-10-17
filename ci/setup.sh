#!/usr/bin/env bash
# Prepare a clean CI workspace under CICD_ROOT (settings: ci/env.sh).
#
#   ci/setup.sh                                  Python 3.14
#   CICD_PYTHON=3.10 ci/setup.sh                 ... on Python 3.10
#
# Four steps, in order:
#   mount     a tmpfs at CICD_ROOT, unless it is a mount point already
#   clear     empty CICD_ROOT and mark it as dbtx's CI workspace
#   copy      the repository, as it is on disk, into CICD_ROOT/src
#   install   Python and the locked dependencies
# Then run ci/build.sh (ci/run.sh does both, and cleans up after).
#
# Errors: every command is checked with `|| fail "..."` (ci/lib.sh explains why).

# Started with `sh`? sh is often not bash: start again under bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

# shellcheck source=ci/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# mount_workspace: a tmpfs (RAM disk) at CICD_ROOT, unless something is mounted there
# already (a tmpfs from an earlier run, or one you mounted yourself). Mounting needs root:
# sudo is used when not running as root (GitHub's runners allow it without a password).
mount_workspace() {
    header "workspace $CICD_ROOT"
    case "$CICD_ROOT" in
        /*) ;;
        *) fail "refusing CICD_ROOT='$CICD_ROOT': not an absolute path" ;;
    esac

    if mountpoint -q "$CICD_ROOT"; then
        ok "$CICD_ROOT is mounted already"
        return
    fi

    local uid
    uid="$(id -u)" || fail "id -u failed"
    local as_root=()
    if [ "$uid" -ne 0 ]; then
        as_root=(sudo)
    fi
    if [ ! -d "$CICD_ROOT" ]; then
        "${as_root[@]}" mkdir -p "$CICD_ROOT" || fail "cannot create $CICD_ROOT (needs root or sudo)"
    fi
    "${as_root[@]}" mount -t tmpfs -o "size=$CICD_TMPFS_SIZE,mode=0777" tmpfs "$CICD_ROOT" \
        || fail "cannot mount a tmpfs at $CICD_ROOT (needs root or sudo; or mount one yourself)"
    ok "mounted a $CICD_TMPFS_SIZE tmpfs at $CICD_ROOT"
}

# clean_workspace: empty CICD_ROOT (clear_workspace, in ci/lib.sh, refuses anything that
# is not dbtx's CI workspace), mark it, and lay out the folders the steps use.
clean_workspace() {
    header "clear $CICD_ROOT"
    clear_workspace
    touch "$CICD_MARKER" || fail "cannot create the marker $CICD_MARKER"
    mkdir -p "$CICD_SRC" "$CICD_OUT" "$CICD_TMP" || fail "cannot create the workspace folders in $CICD_ROOT"
    ok "cleared $CICD_ROOT"
}

# copy_repository: the repository as it is on disk, into CICD_ROOT/src: every file git
# tracks and every untracked file git does not ignore (so a local `.venv/`, `target/` or
# `examples/.dbtx/` never reaches the tests). A tracked file deleted from the working tree
# is not copied. Writes what was tested to out/source.txt.
copy_repository() {
    header "copy $CICD_REPO"
    local listed="$CICD_TMP/copy-listed"
    local existing="$CICD_TMP/copy-existing"
    git -C "$CICD_REPO" ls-files -z --cached --others --exclude-standard > "$listed" \
        || fail "cannot list the files of $CICD_REPO (is it a git repository?)"

    # Leave out the tracked files that were deleted from the working tree.
    : > "$existing" || fail "cannot create $existing"
    local path
    local count=0
    while IFS= read -r -d '' path; do
        if [ -e "$CICD_REPO/$path" ] || [ -L "$CICD_REPO/$path" ]; then
            printf '%s\0' "$path" >> "$existing" || fail "cannot write $existing"
            count=$((count + 1))
        fi
    done < "$listed"

    # No `cd`: inside a pipeline checked by `|| fail`, a failed `cd` would not stop anything.
    tar -C "$CICD_REPO" --null --files-from="$existing" -cf - | tar -C "$CICD_SRC" -xf - \
        || fail "cannot copy $CICD_REPO into $CICD_SRC"
    rm -f "$listed" "$existing" || fail "cannot remove the file lists in $CICD_TMP"
    info "copied $count files"

    local commit
    local changes
    commit="$(git -C "$CICD_REPO" rev-parse HEAD)" || fail "cannot read the commit of $CICD_REPO"
    changes="$(git -C "$CICD_REPO" status --porcelain)" || fail "cannot read the status of $CICD_REPO"
    local uncommitted="no"
    if [ -n "$changes" ]; then
        uncommitted="yes"
    fi
    printf 'repository: %s\ncommit: %s\nuncommitted changes: %s\n' \
        "$CICD_REPO" "$commit" "$uncommitted" > "$CICD_OUT/source.txt" \
        || fail "cannot write $CICD_OUT/source.txt"
    ok "copied $commit (uncommitted changes: $uncommitted)"
}

# install_dependencies: install CICD_PYTHON and the locked dependencies into the
# workspace's venv. `--frozen` uses uv.lock as it is: an outdated lock file fails here.
install_dependencies() {
    header "install Python $CICD_PYTHON and the dependencies"
    uv python install "$CICD_PYTHON" || fail "cannot install Python $CICD_PYTHON"
    uv --directory "$CICD_SRC" sync --frozen --extra postgres --python "$CICD_PYTHON" \
        || fail "uv sync failed (is uv.lock up to date?)"

    local python_version
    local uv_version
    python_version="$("$CICD_VENV/bin/python" --version)" || fail "the venv's python does not run"
    uv_version="$(uv --version)" || fail "uv --version failed"
    printf 'python: %s\nuv: %s\n' "$python_version" "$uv_version" >> "$CICD_OUT/source.txt" \
        || fail "cannot write $CICD_OUT/source.txt"
    ok "installed into $CICD_VENV ($python_version)"
}

# The steps, called plainly (never inside if / || / &&: see ci/lib.sh).
mount_workspace
clean_workspace
copy_repository
install_dependencies
