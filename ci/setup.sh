#!/usr/bin/env bash
# Prepare a clean CI workspace under CICD_ROOT (settings: ci/env.sh).
#
#   ci/setup.sh                                  the committed HEAD, Python 3.14
#   CICD_WORKING_TREE=1 ci/setup.sh              ... with your uncommitted changes
#   CICD_PYTHON=3.10 ci/setup.sh                 ... on Python 3.10
#
# Three steps, in order: clean the workspace, clone the repository, install the
# dependencies. Then run ci/build.sh (ci/run.sh does both).
#
# Errors: every command is checked with `|| fail "..."` (ci/lib.sh explains why).

# Started with `sh`? sh is often not bash: start again under bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail
# shellcheck source=ci/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# clean_workspace: empty CICD_ROOT and mark it as ours.
#
# The only step that deletes, so it refuses anything that does not look like dbtx's CI
# workspace: a relative path or `/`, a folder containing the repository under test, and a
# non-empty folder without the marker file (for example a mistyped CICD_ROOT). The folder
# itself is kept and only its contents removed: it may be a tmpfs mount point.
clean_workspace() {
    header "clean $CICD_ROOT"

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
    repo="$(realpath -m "$CICD_REPO")" || fail "cannot resolve CICD_REPO='$CICD_REPO'"
    case "$repo/" in
        "$root"/*) fail "refusing to clean $root: it contains the repository $repo" ;;
    esac

    # A folder with something in it must carry our marker. `ls` runs on its own line, so
    # a failure to list it stops here instead of looking like an empty folder.
    if [ -d "$root" ]; then
        local contents
        contents="$(ls -A "$root")" || fail "cannot list $root"
        if [ -n "$contents" ] && [ ! -f "$root/.dbtx-cicd" ]; then
            fail "refusing to clean $root: it is not empty and has no .dbtx-cicd marker (not created by dbtx's CI). Empty it yourself, or set CICD_ROOT to another folder"
        fi
    fi

    # Empty it (keeping the folder), mark it, and lay out the folders the steps use.
    mkdir -p "$root" || fail "cannot create $root"
    find "$root" -mindepth 1 -delete || fail "cannot empty $root"
    touch "$root/.dbtx-cicd" || fail "cannot create the marker $root/.dbtx-cicd"
    mkdir -p "$CICD_OUT" "$CICD_TMP" || fail "cannot create $CICD_OUT and $CICD_TMP"
    ok "cleaned $root"
}

# clone_repository: clone CICD_REPO at CICD_REF into CICD_ROOT/src.
#
# With CICD_WORKING_TREE=1 it then copies the repository's uncommitted changes over the
# commit: changed (staged or not) and untracked files that git does not ignore are
# copied, and deleted files are removed. Writes what was tested to out/source.txt.
clone_repository() {
    header "clone $CICD_REPO"

    local commit
    commit="$(git -C "$CICD_REPO" rev-parse --verify "$CICD_REF^{commit}")" \
        || fail "$CICD_REPO has no commit '$CICD_REF'"
    git clone --quiet --no-hardlinks "$CICD_REPO" "$CICD_SRC" \
        || fail "cannot clone $CICD_REPO into $CICD_SRC"
    git -C "$CICD_SRC" -c advice.detachedHead=false checkout --quiet --detach "$commit" \
        || fail "cannot check out $commit in $CICD_SRC"
    info "checked out $commit"

    local uncommitted="no"
    if [ "$CICD_WORKING_TREE" = "1" ]; then
        if [ "$CICD_REF" != "HEAD" ]; then
            fail "CICD_WORKING_TREE=1 needs CICD_REF=HEAD (the changes are relative to HEAD)"
        fi
        # Changed and untracked files: listed by git (NUL-separated), packed by tar from
        # the repository, unpacked into the clone. No `cd`: inside a pipeline checked by
        # `|| fail`, a failed `cd` would not stop anything.
        {
            git -C "$CICD_REPO" diff --name-only -z --diff-filter=d HEAD \
                && git -C "$CICD_REPO" ls-files -z --others --exclude-standard
        } | tar -C "$CICD_REPO" --null --files-from=- -cf - \
            | tar -C "$CICD_SRC" -xf - \
            || fail "cannot copy the uncommitted changes of $CICD_REPO"
        # Files deleted in the working tree: removed from the clone.
        git -C "$CICD_REPO" diff --name-only -z --diff-filter=D HEAD \
            | xargs -0 -r -I{} rm -f -- "$CICD_SRC/{}" \
            || fail "cannot remove the files deleted in $CICD_REPO"
        uncommitted="yes (CICD_WORKING_TREE=1)"
        info "copied the uncommitted changes"
    fi

    printf 'repository: %s\ncommit: %s\nuncommitted changes: %s\n' \
        "$CICD_REPO" "$commit" "$uncommitted" > "$CICD_OUT/source.txt" \
        || fail "cannot write $CICD_OUT/source.txt"
    ok "cloned $commit (uncommitted changes: $uncommitted)"
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
clean_workspace
clone_repository
install_dependencies
