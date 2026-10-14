# Continuous integration

dbtx's CI is a few shell scripts in [`ci/`](../../ci/). GitHub Actions runs them
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) on every pull request
into `main`, testing the PR's merge commit (the branch merged into the current `main`:
the code that would land), again on every push to the PR branch; and on demand (Actions
tab → CI → Run workflow, on any branch). Pushes alone do not start it. Anyone can run
exactly the same thing locally. The workflow file only provides the
machine (a RAM disk at `/mnt/cicd` and uv); everything else is in the scripts.

| File | Role |
|---|---|
| `ci/run.sh` | the whole run: `setup.sh`, then `build.sh` |
| `ci/setup.sh` | clean the workspace, clone the repository, install the dependencies |
| `ci/build.sh` | lint, unit tests, live tests, package check |
| `ci/env.sh` | the settings (sourced by the scripts) |
| `ci/lib.sh` | output helpers: colors, `header`, `info`, `ok`, `warn`, `fail` (sourced) |

## Running it locally

```console
ci/run.sh                                  # the committed HEAD, Python 3.14
ci/run.sh --sequential                     # unit, then live tests, on the console
CICD_WORKING_TREE=1 ci/run.sh              # ... with your uncommitted changes
CICD_PYTHON=3.10 ci/run.sh                 # ... on Python 3.10 (CI runs 3.10 and 3.14)
ci/build.sh                                # the checks again, on the prepared workspace
```

`sh ci/run.sh` and `bash ci/run.sh` work too: started by a shell that is not bash, each
script starts itself again under bash.

**Parallel by default.** The unit and live tests run at the same time (about a minute
faster), each writing its output to `.build/unit.log` and `.build/live.log` at the
repository root (ignored by git; replaced on every run). The logs are verbose (`pytest
-v`: one line per test, with its name and result), so they show what ran and how far a
run has got; `--sequential` keeps the console compact (`-q`: one dot per test). When both finish, each run's
pytest summary is printed; a failed run's last 40 lines are printed too, then the build
fails (two failures are both shown). `--sequential` runs them one after the other, with
their output on the console and no log files. They can run together because they share
nothing they write: separate processes, temporary folders (`tmp/unit`, `tmp/live`) and
reports, Postgres only in the unit run, and pytest started with `uv run --no-sync`, so
neither touches the shared venv.

It needs `git`, `uv` and a writable `CICD_ROOT` (`/mnt/cicd` by default; a tmpfs works
well: a run uses about 850 MB). Run it as a normal user: the tests start a throwaway
Postgres server, and Postgres refuses to run as root. To use a server of your own
instead, set `CICD_POSTGRES_URI`.

To re-run a single check, run its command (in `ci/build.sh`) by hand in
`$CICD_ROOT/src`, for example `uv run pytest -m "not live"`.

Colors are on in a terminal and in GitHub Actions, off when the output goes to a file;
`NO_COLOR=1` turns them off.

## Settings

Every setting is an environment variable with a default ([`ci/env.sh`](../../ci/env.sh)).
They all start with `CICD_`: dbtx warns about unknown `DBTX_*` variables, and dbt reads
`DBT_*` ones.

| Variable | Default | Meaning |
|---|---|---|
| `CICD_ROOT` | `/mnt/cicd` | the workspace, emptied by `setup.sh` |
| `CICD_REPO` | this repository | the git repository to test (a local path) |
| `CICD_REF` | `HEAD` | the commit to test (CI: the PR's merge commit, or the chosen branch's latest commit when run on demand) |
| `CICD_WORKING_TREE` | `0` | `1`: also copy uncommitted changes (only with `CICD_REF=HEAD`) |
| `CICD_PYTHON` | `3.14` | the Python version |
| `CICD_POSTGRES_URI` | unset | a Postgres server for the tests; unset starts a throwaway one |

`build.sh` also removes `DBTX_*` and dbt's location variables (`DBT_PROJECT_DIR`,
`DBT_PROFILES_DIR`, `DBT_TARGET_PATH`, `DBT_LOG_PATH`, `DBT_TARGET`, `DBT_PROFILE`)
from its environment, so a developer's shell cannot change the result.

## The steps

`setup.sh`, in order:

| Step | Does |
|---|---|
| clean | empties `CICD_ROOT` (keeping the folder: it may be a mount point) and marks it with `.dbtx-cicd`. Refuses, deleting nothing: a relative path or `/`; a non-empty folder without the marker (a mistyped `CICD_ROOT`); a folder containing the repository |
| clone | clones `CICD_REPO` and checks out `CICD_REF`; with `CICD_WORKING_TREE=1` copies your changed and untracked files over it (and removes deleted ones) |
| install | installs `CICD_PYTHON` and the locked dependencies (`uv sync --frozen --extra postgres`: an outdated `uv.lock` fails here) |

`build.sh`, in order (the first failure stops it; unit and live run in parallel unless
`--sequential`):

| Step | Does |
|---|---|
| lint | `ruff check` (Astral's linter, pinned in `env.sh`), with pyflakes' rules (`[tool.ruff.lint]` in `pyproject.toml`) |
| unit tests | `pytest -m "not live"`: the unit tests, the Postgres ones included |
| live tests | `pytest -m live`: real dbt runs of the example project, and the frozen-fixture drift checks |
| package | builds the wheel, installs it into a fresh venv, and runs that `dbtx` on a scratch platform (`--help`, `db init`, `db status`, `config`): proves the wheel ships the migrations and the schema |

## How the scripts handle errors

Every command is followed by `|| fail "<what failed>"`, simple ones (`mkdir`, `cp`,
`rm`, `printf > file`) included:

```bash
uv build --wheel --out-dir "$CICD_OUT/dist" || fail "package: building the wheel failed"
mkdir -p "$platform" || fail "package: cannot create $platform"
```

`fail` (in `ci/lib.sh`) prints the message in red and exits 1, so every failure says
which step and which command it was. `set -euo pipefail` stays on as the backstop: a
command that lacks its check still stops the script (with the tool's own message).

Three rules keep that safe:

1. **Call steps plainly** (`check_package`), never inside `if`, `||`, `&&` or `!`.
   There bash turns `set -e` off, for every command inside the function, so an
   unchecked failure would carry on silently and the step would look successful.
2. **Never `local x=$(command)`**: `local` succeeds and hides the command's failure.
   Write `local x` first, then `x=$(command) || fail "..."`.
3. **No `cd` inside a checked subshell or pipeline**: if the `cd` failed, nothing would
   stop. Use `git -C`, `tar -C`, `uv --directory` instead.

What no script can catch: a tool that reports an error but exits 0 (ruff, pytest, uv
and dbt all exit non-zero on failure).

## The workspace

```
/mnt/cicd/
  .dbtx-cicd        the marker: this folder belongs to dbtx's CI
  src/              the clone under test
  venv-<python>/    its virtual environment
  tmp/unit, live    pytest's temporary folders
  smoke/            the package step's venv and scratch platform
  out/              what to keep (CI uploads it as an artifact):
    source.txt        commit, uncommitted changes or not, Python and uv versions
    unit.xml, live.xml  JUnit reports
    dist/             the built wheel
```

The parallel runs' logs are not in the workspace but at the repository root:
`.build/unit.log` and `.build/live.log`. GitHub uploads them with `out/`.

uv's download cache stays where uv keeps it (on disk), so a fresh workspace does not
download everything again; `UV_LINK_MODE=copy` because a tmpfs cannot hardlink to it.

## Checking a change to the scripts

The scripts have no unit tests (the user's decision, 2026-10-14): check a change by
running `ci/run.sh` end to end, and for the clean step, against a scratch `CICD_ROOT`.

## Adding to it

- A new check is a new function in `ci/build.sh` (a `header`, its commands each with
  `|| fail`, an `ok`), called plainly in the list at the bottom.
- isort's rules (`I` in ruff) can be added to `[tool.ruff.lint]` once the imports are
  sorted repo-wide (`uvx ruff check --select I --fix` sorts them; review the diff).
