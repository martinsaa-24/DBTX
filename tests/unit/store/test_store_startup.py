"""Start-up checks (plan 6.6 step 8, database part), run by `cli.main` before dbt starts.

Behaviour:

Gates: the checks run only in platform mode, with the backend enabled, for a command
that uses the database, with DB_URI set, and (until plan Phase 5, while nothing is
recorded) only when `ENFORCE_DB_REQUIREMENTS` is on. Otherwise no store is opened, no
driver is used and nothing is connected to.

Checks, in order; each failure exits 2 with a message and dbt is never started:
1. connect (an unreachable server, or a SQLite file that cannot be created; the URI is
   shown with its password masked)
2. the server version floor
3. dbtx's tables exist (until migrations, plan Phase 3: "run `dbtx db init`").
   `dbtx db init` and `dbtx db upgrade` skip this check, since fixing it is their job
4. guard G2, for a command with a project: the project folder claims its dbt project
   name, or confirms its claim. Another folder's claim → exit 2. Warnings from the
   claim (a renamed project) are printed on stderr
5. guard G1 together with G2 ("established owner wins"): a name declared by several
   folders on this platform
   * the current folder holds the claim → the run continues, with a warning naming the
     folders that will be refused
   * nobody holds it → exit 2 with G1's message, and nothing is claimed
   * another folder holds it → exit 2 with G2's message

Tests use `--db-uri` / `--db-schema` to point dbtx at the test's own database; only the
backends with a database another engine can open (`sqlite-file`, `postgres`) apply.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

import dbtx.store.startup as startup
from dbtx import cli
from dbtx.config import DbtxConfig
from dbtx.store import Store
from dbtx.store import models as m
from tests.unit.conftest import FakeDbtRunner, LayoutBuilder

from .conftest import CLOCK_START, ON_SQLITE_FILE_AND_POSTGRES, TEST_HOST, FakeClock, store_config

OCT_1 = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)


def db_flags(store: Store) -> list[str]:
    flags = ["--db-uri", store.uri]
    if store.schema is not None:
        flags += ["--db-schema", store.schema]
    return flags


def claims(store: Store) -> list[tuple]:
    with store.session() as session:
        rows = session.scalars(sa.select(m.Project).order_by(m.Project.project)).all()
        result = []
        for row in rows:
            result.append((row.project, row.folder, row.claimed_at, row.claimed_by_host, row.last_seen_at))
        return result


def add_claim(store: Store, project: str, folder: str, host: str | None = "build-01") -> None:
    """Write a claim straight into the `project` table: `folder` (a project folder under
    the platform root) owns the dbt project name `project`, claimed by `host` on OCT_1."""
    with store.begin() as session:
        session.add(
            m.Project(project=project, folder=folder, claimed_at=OCT_1, claimed_by_host=host, last_seen_at=OCT_1)
        )


@pytest.fixture
def opened_stores(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every store the start-up checks open (the real one is still opened)."""
    opened = []
    real_open_store = startup.open_store

    def recording_open_store(config: DbtxConfig) -> Store:
        opened.append(config.db_uri.value)
        return real_open_store(config)

    monkeypatch.setattr(startup, "open_store", recording_open_store)
    return opened


@pytest.fixture
def colliding_platform(platform: Path, layout: LayoutBuilder) -> Path:
    """The standard platform plus jaffle-shop-2, which reuses the name jaffle_shop."""
    layout.project(platform, "jaffle-shop-2", "jaffle_shop")
    return platform


# --------------------------------------------------------------------------------------
# Gates: no store is opened
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--project-dir", "jaffle-shop", "--db-backend", "disabled"],
        ["ls", "--project-dir", "jaffle-shop"],
        ["deps", "--project-dir", "jaffle-shop"],
        ["config"],
        ["--help"],
    ],
    ids=["backend-disabled", "ls", "deps", "config", "help"],
)
def test_no_store_is_opened_behind_the_gates(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    opened_stores: list[str],
    argv: list[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(argv) == 0
    assert opened_stores == []


def test_standalone_mode_never_opens_a_store(
    lone_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    opened_stores: list[str],
) -> None:
    monkeypatch.chdir(lone_project)
    monkeypatch.setenv("DBTX_DB_URI", "sqlite:////nowhere/dbtx.db")
    assert cli.main(["run", "--standalone-mode"]) == 0
    assert opened_stores == []


def test_no_store_is_opened_until_enforced(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_dbt: type[FakeDbtRunner],
    opened_stores: list[str],
) -> None:
    # Until plan Phase 5, a dbt command makes no connection: nothing is recorded yet.
    monkeypatch.chdir(platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop"]) == 0
    assert opened_stores == []
    assert not (platform / ".dbtx").exists()


# --------------------------------------------------------------------------------------
# Guard G2 through cli.main
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_first_run_claims_the_project_name(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    opened_stores: list[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop", *db_flags(store)]) == 0
    assert opened_stores == [store.uri]
    assert len(fake_dbt.calls) == 1
    assert claims(store) == [("jaffle_shop", "jaffle-shop", CLOCK_START, TEST_HOST, CLOCK_START)]


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_next_run_from_the_same_folder_bumps_last_seen(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    clock: FakeClock,
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop", *db_flags(store)]) == 0
    later = clock.advance(days=1)
    assert cli.main(["seed", "--project-dir", "jaffle-shop", *db_flags(store)]) == 0
    assert claims(store) == [("jaffle_shop", "jaffle-shop", CLOCK_START, TEST_HOST, later)]


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_another_folders_claim_stops_the_run(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "old-shop")
    monkeypatch.chdir(platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop", *db_flags(store)]) == 2
    assert fake_dbt.calls == []
    assert (
        "dbtx: project name 'jaffle_shop' is owned by folder 'old-shop' in this database "
        "(claimed 2026-10-01 by host build-01). Folder 'jaffle-shop' cannot use it."
    ) in capsys.readouterr().err
    assert claims(store) == [("jaffle_shop", "old-shop", OCT_1, "build-01", OCT_1)]


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_renamed_project_warns(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop_old", "jaffle-shop")
    monkeypatch.chdir(platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop", *db_flags(store)]) == 0
    assert (
        "dbtx: warning: folder 'jaffle-shop' previously recorded project 'jaffle_shop_old'"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_project_without_a_readable_name_is_not_claimed(
    store: Store,
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
) -> None:
    layout.project(platform, "nameless", "unused", dbt_project_text="profile: x\n")
    monkeypatch.chdir(platform)
    # dbt reports the missing name itself.
    assert cli.main(["run", "--project-dir", "nameless", *db_flags(store)]) == 0
    assert claims(store) == []


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_a_command_without_a_project_claims_nothing(store: Store, enforce: None) -> None:
    config = replace(store_config(store.uri, store.schema), command="db upgrade")
    assert startup.run_startup_checks(config) == []
    assert claims(store) == []


# --------------------------------------------------------------------------------------
# Guard G1 with G2: the established owner wins
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_g1_collision_without_a_claim_stops_the_run(
    store: Store,
    colliding_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(colliding_platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop-2", *db_flags(store)]) == 2
    assert fake_dbt.calls == []
    assert (
        f"dbtx: project name 'jaffle_shop' is declared by 2 folders under {colliding_platform}: "
        f"jaffle-shop, jaffle-shop-2. Each project on a platform needs a unique name: in "
        f"dbt_project.yml"
    ) in capsys.readouterr().err
    assert claims(store) == []  # nothing claimed for a colliding name


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_g1_collision_while_holding_the_claim_runs_with_a_warning(
    store: Store,
    colliding_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    monkeypatch.chdir(colliding_platform)
    later = clock.advance(hours=1)

    assert cli.main(["run", "--project-dir", "jaffle-shop", *db_flags(store)]) == 0

    assert len(fake_dbt.calls) == 1
    assert (
        f"dbtx: warning: project name 'jaffle_shop' is also declared by jaffle-shop-2 under "
        f"{colliding_platform}; this folder (jaffle-shop) owns the name in the database, so "
        f"jaffle-shop-2 will be refused"
    ) in capsys.readouterr().err
    assert claims(store) == [("jaffle_shop", "jaffle-shop", OCT_1, "build-01", later)]


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_g1_collision_from_the_folder_without_the_claim_is_refused(
    store: Store,
    colliding_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    monkeypatch.chdir(colliding_platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop-2", *db_flags(store)]) == 2
    assert fake_dbt.calls == []
    assert "is owned by folder 'jaffle-shop' in this database" in capsys.readouterr().err


# --------------------------------------------------------------------------------------
# Connection, server and tables
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("empty_store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_missing_tables_stop_the_run(
    empty_store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop", *db_flags(empty_store)]) == 2
    assert fake_dbt.calls == []
    assert (
        f"dbtx: the database {empty_store.masked_uri()} has no dbtx tables; run `dbtx db init`"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize("command", ["db init", "db upgrade"])
@pytest.mark.parametrize("empty_store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_db_init_and_upgrade_skip_the_tables_check(
    empty_store: Store,
    enforce: None,
    command: str,
) -> None:
    config = replace(store_config(empty_store.uri, empty_store.schema), command=command)
    assert startup.run_startup_checks(config) == []


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_an_old_server_stops_the_run(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(type(store.adapter), "server_version", lambda self, connection: (1, 0))
    monkeypatch.chdir(platform)
    assert cli.main(["run", "--project-dir", "jaffle-shop", *db_flags(store)]) == 2
    assert fake_dbt.calls == []
    assert "1.0 is too old: dbtx needs" in capsys.readouterr().err
    assert claims(store) == []


def test_an_unreachable_server_stops_the_run(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Nothing listens on port 1: the connection is refused at once, no server needed.
    monkeypatch.chdir(platform)
    uri = "postgresql+psycopg://dbtx:s3cret@127.0.0.1:1/dbtx?connect_timeout=2"
    assert cli.main(["run", "--project-dir", "jaffle-shop", "--db-uri", uri]) == 2
    assert fake_dbt.calls == []
    err = capsys.readouterr().err
    assert (
        "dbtx: cannot connect to the database "
        "postgresql+psycopg://dbtx:***@127.0.0.1:1/dbtx?connect_timeout=2: "
    ) in err
    assert "s3cret" not in err


def test_a_sqlite_file_that_cannot_be_created_stops_the_run(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocker = layout.directory("blocked") / "not-a-directory"
    blocker.write_text("a file where the database directory should be")
    monkeypatch.chdir(platform)
    uri = f"sqlite:///{blocker}/dbtx.db"
    assert cli.main(["run", "--project-dir", "jaffle-shop", "--db-uri", uri]) == 2
    assert fake_dbt.calls == []
    assert f"dbtx: cannot open the database {uri}: " in capsys.readouterr().err
