# Continuous integration

dbtx's CI is a few shell scripts in [`ci/`](../../ci/). GitHub Actions runs them
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) on every pull request
into `main`, testing the PR's merge commit (the branch merged into the current `main`:
the code that would land), again on every push to the PR branch; and on demand (Actions
tab → CI → Run workflow, on any branch). Pushes alone do not start it. Anyone can run
exactly the same thing locally. The workflow file only provides the machine (uv) and
uploads the artifacts; everything else is in the scripts.

A run, the same in GitHub Actions and locally:

1. **Mount:** a tmpfs (RAM disk) at `CICD_ROOT` (`/mnt/cicd`), unless something is
   mounted there already.
2. **Clear** `CICD_ROOT`.
3. **Copy** the repository into `CICD_ROOT/src`, as it is on disk: in GitHub Actions the
   checkout (a pull request's merge commit), locally your working tree with its
   uncommitted changes. Files git ignores (`.venv/`, `target/`, `examples/.dbtx/`) are not
   copied, so local state cannot reach the tests.
4. **Lint, test and package** that copy. Nothing runs in, or writes to, the repository
   except step 5.
5. **Keep the artifacts:** `CICD_ROOT/out` is copied to `.build/` at the repository root,
   pass or fail. GitHub uploads `.build/`.
6. **Passed:** `CICD_ROOT` is emptied. **Failed:** it is left as it is, to look into; the
   next run clears it.

| File | Role |
|---|---|
| `ci/run.sh` | the whole run: `setup.sh`, `build.sh`, then steps 5 and 6 |
| `ci/setup.sh` | steps 1–3, and installing the dependencies |
| `ci/build.sh` | step 4: lint, unit tests, live tests, package check |
| `ci/env.sh` | the settings (sourced by the scripts) |
| `ci/lib.sh` | helpers: colors, `header`, `info`, `ok`, `warn`, `fail`, and `clear_workspace` (sourced) |

## Running it locally

```console
ci/run.sh                                  # your working tree, Python 3.14
ci/run.sh --sequential                     # unit, then live tests, on the console
CICD_PYTHON=3.10 ci/run.sh                 # ... on Python 3.10 (CI runs 3.10 and 3.14)
ci/setup.sh && ci/build.sh                 # the same, leaving the workspace for you
```

`sh ci/run.sh` and `bash ci/run.sh` work too: started by a shell that is not bash, each
script starts itself again under bash.

**Parallel by default.** The unit and live tests run at the same time (about a minute
faster), each writing its output to `out/logs/unit.log` and `out/logs/live.log` in the
workspace (kept in `.build/logs/`). The logs are verbose (`pytest
-v`: one line per test, with its name and result), so they show what ran and how far a
run has got; `--sequential` keeps the console compact (`-q`: one dot per test). When both finish, each run's
pytest summary is printed; a failed run's last 40 lines are printed too, then the build
fails (two failures are both shown). `--sequential` runs them one after the other, with
their output on the console and no log files. They can run together because they share
nothing they write: separate processes, temporary folders (`tmp/unit`, `tmp/live`) and
reports, Postgres only in the unit run, and pytest started with `uv run --no-sync`, so
neither touches the shared venv.

It needs Linux, `git`, `uv`, and either a mount at `CICD_ROOT` or `sudo` to mount the
tmpfs (a run uses about 850 MB; mount it once yourself, for example `sudo mount -t tmpfs
-o size=4g,mode=0777 tmpfs /mnt/cicd`, and later runs need no `sudo`). Run it as a
normal user: the tests start a throwaway Postgres server, and Postgres refuses to run as
root. To use a server of your own instead, set `CICD_POSTGRES_URI`.

To re-run a single check after a failed run, run its command (in `ci/build.sh`) by hand
in `$CICD_ROOT/src`, for example `uv run pytest -m "not live"`. A copy of a file you
change in the repository is not updated there: run `ci/run.sh` again.

Colors are on in a terminal and in GitHub Actions, off when the output goes to a file;
`NO_COLOR=1` turns them off.

## Settings

Every setting is an environment variable with a default ([`ci/env.sh`](../../ci/env.sh)).
They all start with `CICD_`: dbtx warns about unknown `DBTX_*` variables, and dbt reads
`DBT_*` ones.

| Variable | Default | Meaning |
|---|---|---|
| `CICD_ROOT` | `/mnt/cicd` | the workspace: a tmpfs is mounted there when it is not a mount point; emptied before a run and after one that passed |
| `CICD_TMPFS_SIZE` | `4g` | the size of that tmpfs |
| `CICD_PYTHON` | `3.14` | the Python version |
| `CICD_POSTGRES_URI` | unset | a Postgres server for the tests; unset starts a throwaway one |

`build.sh` also removes `DBTX_*` and dbt's location variables (`DBT_PROJECT_DIR`,
`DBT_PROFILES_DIR`, `DBT_TARGET_PATH`, `DBT_LOG_PATH`, `DBT_TARGET`, `DBT_PROFILE`)
from its environment, so a developer's shell cannot change the result.

## The steps

`setup.sh`, in order:

| Step | Does |
|---|---|
| mount | `CICD_ROOT` a mount point already → used as it is; otherwise a `CICD_TMPFS_SIZE` tmpfs is mounted there (`sudo` unless root). A relative path is refused |
| clear | empties `CICD_ROOT` (keeping the folder: it is a mount point) and marks it with `.dbtx-cicd`. Refuses, deleting nothing: a relative path or `/`; a non-empty folder without the marker (a mistyped `CICD_ROOT`); a folder containing the repository. The same check (`clear_workspace`, `ci/lib.sh`) guards the clean-up after a passing run |
| copy | copies the repository's tracked files and its untracked, not ignored ones, as they are on disk, into `src/` (a tracked file deleted from the working tree is not copied); writes `out/source.txt` |
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
  src/              the copy of the repository under test
  venv-<python>/    its virtual environment
  tmp/unit, live    pytest's temporary folders
  smoke/            the package step's venv and scratch platform
  out/              what to keep (ci/run.sh copies it to .build/; GitHub uploads that):
    source.txt        the commit, uncommitted changes or not, Python and uv versions
    unit.xml, live.xml  JUnit reports
    logs/             the parallel test runs' logs (unit.log, live.log)
    dist/             the built wheel
```

After a run that passed, `/mnt/cicd` is empty (the mount stays); after one that failed,
it holds all of the above as the failure left it.

uv's download cache stays where uv keeps it (on disk), so a fresh workspace does not
download everything again; `UV_LINK_MODE=copy` because a tmpfs cannot hardlink to it.

## Checking a change to the scripts

The scripts have no unit tests (the user's decision, 2026-10-14): check a change by
running `ci/run.sh` end to end: once passing (the workspace is emptied, `.build/` holds
the artifacts) and once failing, for example `CICD_PYTHON=3.99 ci/run.sh` (the workspace
is left, `.build/` holds `source.txt`).

## Adding to it

- A new check is a new function in `ci/build.sh` (a `header`, its commands each with
  `|| fail`, an `ok`), called plainly in the list at the bottom.
- isort's rules (`I` in ruff) can be added to `[tool.ruff.lint]` once the imports are
  sorted repo-wide (`uvx ruff check --select I --fix` sorts them; review the diff).
