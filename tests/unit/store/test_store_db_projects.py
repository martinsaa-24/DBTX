"""`dbtx db projects` and `dbtx db projects rebind` (plan 6.5.4, "legitimate moves").

Behaviour:

`dbtx db projects`
* lists every claim (project, folder, claimed at, by host, last seen), by project name;
  says so when there is none
* uses the database whenever it runs (a `dbtx db` command)
* exit 2, with the reason, and no store opened where none is configured: standalone
  mode, a disabled backend, no DB_URI. Exit 2 when the database cannot be used: an
  unreachable server, no dbtx tables
* an unknown word after `projects` is a usage error

`dbtx db projects rebind <name> --folder <folder> [--force]`
* moves the claim on <name> to <folder>, printing the old and new folder
* refused while the old folder still exists under the platform root, and when <folder>
  is not a project folder (with a dbt_project.yml) under the platform root; `--force`
  overrides both
* <folder> must be a folder name, not a path
* an unclaimed name is an error; the folder that already owns it changes nothing
* bad arguments are a usage error (exit 2)
* after a rebind, the new folder's runs pass guard G2
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

import dbtx.store.cli as store_cli
from dbtx import cli
from dbtx.config import DbtxConfig
from dbtx.store import Store
from dbtx.store import models as m
from tests.unit.conftest import FakeDbtRunner, LayoutBuilder

from .conftest import (
    CLOCK_START,
    ON_POSTGRES,
    ON_SQLITE_FILE,
    ON_SQLITE_FILE_AND_POSTGRES,
    TEST_HOST,
    FakeClock,
)

OCT_1 = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)
USAGE = "usage: dbtx db projects [rebind <name> --folder <folder> [--force]]"


def db_flags(store: Store) -> list[str]:
    flags = ["--db-uri", store.uri]
    if store.schema is not None:
        flags += ["--db-schema", store.schema]
    return flags


def add_claim(
    store: Store,
    project: str,
    folder: str,
    host: str | None = "build-01",
    last_seen_at: datetime = OCT_1,
) -> None:
    """Write a claim straight into the `project` table: `folder` (a project folder under
    the platform root) owns the dbt project name `project`, claimed by `host` on OCT_1
    and last run at `last_seen_at`."""
    with store.begin() as session:
        session.add(
            m.Project(project=project, folder=folder, claimed_at=OCT_1, claimed_by_host=host, last_seen_at=last_seen_at)
        )


def claims(store: Store) -> list[tuple]:
    with store.session() as session:
        rows = session.scalars(sa.select(m.Project).order_by(m.Project.project)).all()
        result = []
        for row in rows:
            result.append((row.project, row.folder, row.claimed_at, row.claimed_by_host, row.last_seen_at))
        return result


@pytest.fixture
def opened_stores(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every store `dbtx db projects` opens (the real one is still opened)."""
    opened = []
    real_open_store = store_cli.open_store

    def recording_open_store(config: DbtxConfig) -> Store:
        opened.append(config.db_uri.value)
        return real_open_store(config)

    monkeypatch.setattr(store_cli, "open_store", recording_open_store)
    return opened


# --------------------------------------------------------------------------------------
# dbtx db projects
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_lists_every_claim_by_project_name(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "other_shop", "other-shop", host=None, last_seen_at=CLOCK_START)
    add_claim(store, "jaffle_shop", "jaffle-shop")
    monkeypatch.chdir(platform)

    assert cli.main(["db", "projects", *db_flags(store)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "PROJECT      FOLDER       CLAIMED AT               BY HOST   LAST SEEN AT",
        "jaffle_shop  jaffle-shop  2026-10-01 08:30:00 UTC  build-01  2026-10-01 08:30:00 UTC",
        "other_shop   other-shop   2026-10-01 08:30:00 UTC  -         2026-10-11 09:00:00 UTC",
    ]


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_says_so_when_nothing_is_claimed(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", *db_flags(store)]) == 0
    assert capsys.readouterr().out == "no project names are claimed in this database\n"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_uses_the_database(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_stores: list[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", *db_flags(store)]) == 0
    assert opened_stores == [store.uri]


def test_needs_platform_mode(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opened_stores: list[str],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    assert cli.main(["db", "projects", "--standalone-mode"]) == 2
    assert "dbtx: database operations require platform mode" in capsys.readouterr().err
    assert opened_stores == []


def test_needs_the_backend_enabled(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opened_stores: list[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", "--db-backend", "disabled"]) == 2
    assert (
        "dbtx: `dbtx db projects` needs the database backend, which is disabled "
        "(DB_BACKEND=disabled from cli)"
    ) in capsys.readouterr().err
    assert opened_stores == []


def test_needs_a_db_uri(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opened_stores: list[str],
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    monkeypatch.chdir(root)
    assert cli.main(["db", "projects"]) == 2
    assert (
        f"dbtx: `dbtx db projects` needs a database: set DB_URI (with --db-uri, DB_URI= "
        f"in {root / 'dbtx.conf'}, or DBTX_DB_URI)"
    ) in capsys.readouterr().err
    assert opened_stores == []


@pytest.mark.parametrize("empty_store", ON_SQLITE_FILE, indirect=True)
def test_creates_the_tables_of_an_uninitialised_sqlite_store(
    empty_store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", *db_flags(empty_store)]) == 0
    captured = capsys.readouterr()
    assert f"dbtx: warning: created dbtx's tables in {empty_store.masked_uri()} (revision 0001)" in captured.err
    assert captured.out == "no project names are claimed in this database\n"


@pytest.mark.parametrize("empty_store", ON_POSTGRES, indirect=True)
def test_needs_an_initialised_postgres_store(
    empty_store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", *db_flags(empty_store)]) == 2
    assert "has no dbtx tables; run `dbtx db init`" in capsys.readouterr().err


def test_an_unreachable_server_is_reported(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    uri = "postgresql+psycopg://dbtx:s3cret@127.0.0.1:1/dbtx?connect_timeout=2"
    assert cli.main(["db", "projects", "--db-uri", uri]) == 2
    err = capsys.readouterr().err
    assert "dbtx: cannot connect to the database postgresql+psycopg://dbtx:***@127.0.0.1:1/dbtx" in err
    assert "s3cret" not in err


def test_an_unknown_word_is_a_usage_error(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opened_stores: list[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", "remove", "jaffle_shop"]) == 2
    assert USAGE in capsys.readouterr().err
    assert opened_stores == []


# --------------------------------------------------------------------------------------
# dbtx db projects rebind
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_rebind_moves_the_claim(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
    fake_dbt: type[FakeDbtRunner],
) -> None:
    # jaffle-shop was renamed on disk to jaffle-shop-new.
    (platform / "jaffle-shop").rename(platform / "jaffle-shop-new")
    add_claim(store, "jaffle_shop", "jaffle-shop")
    monkeypatch.chdir(platform)

    argv = ["db", "projects", "rebind", "jaffle_shop", "--folder", "jaffle-shop-new", *db_flags(store)]
    assert cli.main(argv) == 0

    assert capsys.readouterr().out == (
        "moved project name 'jaffle_shop' from folder 'jaffle-shop' to 'jaffle-shop-new'\n"
    )
    assert claims(store) == [("jaffle_shop", "jaffle-shop-new", CLOCK_START, TEST_HOST, OCT_1)]
    # The new folder now passes guard G2.
    assert cli.main(["run", "--project-dir", "jaffle-shop-new", *db_flags(store)]) == 0
    assert len(fake_dbt.calls) == 1


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_rebind_accepts_the_equals_form(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_claim(store, "jaffle_shop", "gone")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", "rebind", "jaffle_shop", "--folder=jaffle-shop", *db_flags(store)]) == 0
    assert claims(store)[0][1] == "jaffle-shop"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_rebind_is_refused_while_the_old_folder_exists(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "other-shop")  # other-shop exists on the platform
    monkeypatch.chdir(platform)

    argv = ["db", "projects", "rebind", "jaffle_shop", "--folder", "jaffle-shop", *db_flags(store)]
    assert cli.main(argv) == 2
    assert (
        f"dbtx: folder 'other-shop' still exists under {platform}; move or remove it "
        f"first, or pass --force"
    ) in capsys.readouterr().err
    assert claims(store)[0][1] == "other-shop"

    assert cli.main([*argv, "--force"]) == 0
    assert claims(store)[0][1] == "jaffle-shop"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_rebind_to_a_folder_that_is_not_a_project_is_refused(
    store: Store,
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "gone")
    layout.directory("platform/not-a-project")
    monkeypatch.chdir(platform)

    for folder in ["no-such-folder", "not-a-project"]:
        argv = ["db", "projects", "rebind", "jaffle_shop", "--folder", folder, *db_flags(store)]
        assert cli.main(argv) == 2
        assert (
            f"dbtx: {platform / folder} is not a project folder (no dbt_project.yml); "
            f"check the name, or pass --force"
        ) in capsys.readouterr().err
    assert claims(store)[0][1] == "gone"

    argv = ["db", "projects", "rebind", "jaffle_shop", "--folder", "no-such-folder", "--force", *db_flags(store)]
    assert cli.main(argv) == 0
    assert claims(store)[0][1] == "no-such-folder"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_rebind_an_unclaimed_name(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    argv = ["db", "projects", "rebind", "jaffle_shop", "--folder", "jaffle-shop", *db_flags(store)]
    assert cli.main(argv) == 2
    assert "dbtx: project name 'jaffle_shop' has no claim in this database" in capsys.readouterr().err
    assert claims(store) == []


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_rebind_to_the_owner_changes_nothing(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    monkeypatch.chdir(platform)
    argv = ["db", "projects", "rebind", "jaffle_shop", "--folder", "jaffle-shop", *db_flags(store)]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == "project name 'jaffle_shop' is already owned by folder 'jaffle-shop'\n"
    assert claims(store) == [("jaffle_shop", "jaffle-shop", OCT_1, "build-01", OCT_1)]


@pytest.mark.parametrize(
    "args",
    [
        ["rebind"],
        ["rebind", "jaffle_shop"],
        ["rebind", "--folder", "jaffle-shop"],
        ["rebind", "jaffle_shop", "--folder"],
        ["rebind", "jaffle_shop", "--folder="],
        ["rebind", "jaffle_shop", "extra", "--folder", "jaffle-shop"],
        ["rebind", "jaffle_shop", "--folder", "jaffle-shop", "--bogus"],
        ["rebind", "jaffle_shop", "--folder", "a", "--folder", "b"],
    ],
    ids=[
        "nothing", "no-folder", "no-name", "folder-without-value", "empty-folder",
        "extra-argument", "unknown-option", "folder-twice",
    ],
)
def test_rebind_usage_errors(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opened_stores: list[str],
    args: list[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", *args]) == 2
    assert USAGE in capsys.readouterr().err
    assert opened_stores == []


@pytest.mark.parametrize("folder", ["a/b", "../jaffle-shop", "..", ".", "/srv/dbtx/jaffle-shop"])
def test_rebind_needs_a_folder_name_not_a_path(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opened_stores: list[str],
    folder: str,
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "projects", "rebind", "jaffle_shop", "--folder", folder]) == 2
    assert (
        f"dbtx: --folder {folder!r} is not a folder name; give the project folder's name "
        f"under the platform root"
    ) in capsys.readouterr().err
    assert opened_stores == []


# --------------------------------------------------------------------------------------
# The rest of `dbtx db`
# --------------------------------------------------------------------------------------


def test_usage_lists_db_projects(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_dbt: type[FakeDbtRunner],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    cli.main(["--help"])
    assert "dbtx db projects [rebind <name> --folder <folder> [--force]]" in capsys.readouterr().out
