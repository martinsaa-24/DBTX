"""Guard G2's registry (`dbtx.store.repositories.projects`): one folder per project name.

Behaviour (plan 6.5.4 and 6.5.5):

`claim_project(session, adapter, project, folder)`
* first use: the name is unclaimed, so a claim is inserted for the folder, with
  `claimed_at` = `last_seen_at` = now and this host's name
* same folder again (from any host): `last_seen_at` is bumped; `claimed_at` and the
  host stay those of the first claim
* another folder: StoreError naming the owner, the claim date and host, and the folder
  refused; the claim is not touched
* a folder that already owns other names (its dbt project was renamed) gains the new
  claim, keeps the old ones, and a warning names each old name, only when the new claim
  is made
* race-safe: two first claims for one name at the same time, from different folders:
  exactly one wins and the other is refused; from the same folder: both succeed
* the caller owns the transaction: rolled back, nothing is claimed

`find_claim` / `list_claims`: one claim, or None; every claim, by project name.

`rebind_project(session, project, folder)`: moves a claim to another folder, with a new
`claimed_at` and host and the same `last_seen_at`; returns (before, after). Same folder:
nothing changes. An unclaimed name is a StoreError. (The filesystem checks of
`dbtx db projects rebind` belong to the command, test_store_startup.py.)
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

import dbtx.store.repositories.projects as projects
from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store.errors import StoreError
from dbtx.store.repositories.projects import Claim, ClaimResult

from .conftest import CLOCK_START, ON_SQLITE_FILE_AND_POSTGRES, TEST_HOST, FakeClock

OCT_1 = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)


def claim(store: Store, project: str = "jaffle_shop", folder: str = "jaffle-shop") -> ClaimResult:
    with store.begin() as session:
        return projects.claim_project(session, store.adapter, project, folder)


def add_claim(
    store: Store,
    project: str,
    folder: str,
    claimed_at: datetime = OCT_1,
    host: str | None = "build-01",
    last_seen_at: datetime = OCT_1,
) -> None:
    """Write a claim straight into the `project` table, bypassing `claim_project`, to set
    up the state a test starts from.

    * `store`: the test's open Store (the `store` fixture)
    * `project`: the dbt project name being claimed (`name:` in dbt_project.yml)
    * `folder`: the project folder under the platform root that owns the name
    * `claimed_at`: when the folder first claimed the name
    * `host`: the host that made the claim (`claimed_by_host`); None if unknown
    * `last_seen_at`: when the owning folder last ran
    """
    with store.begin() as session:
        session.add(
            m.Project(
                project=project,
                folder=folder,
                claimed_at=claimed_at,
                claimed_by_host=host,
                last_seen_at=last_seen_at,
            )
        )


def stored_claims(store: Store) -> list[Claim]:
    with store.session() as session:
        rows = session.scalars(sa.select(m.Project).order_by(m.Project.project)).all()
        claims = []
        for row in rows:
            claims.append(
                Claim(row.project, row.folder, row.claimed_at, row.claimed_by_host, row.last_seen_at)
            )
        return claims


# --------------------------------------------------------------------------------------
# claim_project
# --------------------------------------------------------------------------------------


def test_first_use_claims_the_name(store: Store) -> None:
    result = claim(store)
    expected = Claim("jaffle_shop", "jaffle-shop", CLOCK_START, TEST_HOST, CLOCK_START)
    assert result.claim == expected
    assert result.is_new is True
    assert result.warnings == ()
    assert stored_claims(store) == [expected]


def test_same_folder_bumps_last_seen_at(
    store: Store,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim(store)
    later = clock.advance(hours=3)
    monkeypatch.setattr(projects, "current_host", lambda: "build-02")

    result = claim(store)

    expected = Claim("jaffle_shop", "jaffle-shop", CLOCK_START, TEST_HOST, later)
    assert (result.claim, result.is_new, result.warnings) == (expected, False, ())
    assert stored_claims(store) == [expected]


def test_another_folder_is_refused(store: Store) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    with pytest.raises(StoreError) as info:
        claim(store, folder="jaffle-shop-2")
    assert str(info.value) == (
        "project name 'jaffle_shop' is owned by folder 'jaffle-shop' in this database "
        "(claimed 2026-10-01 by host build-01). Folder 'jaffle-shop-2' cannot use it. "
        "If the project moved, run `dbtx db projects rebind jaffle_shop --folder "
        "jaffle-shop-2`"
    )
    assert stored_claims(store) == [Claim("jaffle_shop", "jaffle-shop", OCT_1, "build-01", OCT_1)]


def test_refusal_without_a_recorded_host(store: Store) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop", host=None)
    with pytest.raises(StoreError, match=r"\(claimed 2026-10-01\)\. Folder 'other' cannot use it"):
        claim(store, folder="other")


def test_folder_names_are_compared_exactly(store: Store) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    with pytest.raises(StoreError, match="cannot use it"):
        claim(store, folder="Jaffle-Shop")


def test_a_renamed_project_keeps_its_old_claim_and_warns_once(
    store: Store,
    clock: FakeClock,
) -> None:
    add_claim(store, "jaffle_shop_old", "jaffle-shop")
    add_claim(store, "zzz_older", "jaffle-shop")
    add_claim(store, "other_shop", "other-shop")

    result = claim(store, project="jaffle_shop", folder="jaffle-shop")

    assert result.is_new is True
    assert result.warnings == (
        "folder 'jaffle-shop' previously recorded project 'jaffle_shop_old'",
        "folder 'jaffle-shop' previously recorded project 'zzz_older'",
    )
    assert [c.project for c in stored_claims(store)] == [
        "jaffle_shop", "jaffle_shop_old", "other_shop", "zzz_older",
    ]

    clock.advance(minutes=1)
    assert claim(store, project="jaffle_shop", folder="jaffle-shop").warnings == ()


def test_a_rolled_back_claim_claims_nothing(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.begin() as session:
            projects.claim_project(session, store.adapter, "jaffle_shop", "jaffle-shop")
            raise RuntimeError("the start-up checks failed later")
    assert stored_claims(store) == []


# --------------------------------------------------------------------------------------
# Concurrent first claims
# --------------------------------------------------------------------------------------


def claim_at_the_same_time(store: Store, folders: list[str]) -> list:
    """Each folder claims `jaffle_shop` in its own thread, started together.
    Returns, per folder, the ClaimResult or the StoreError."""
    barrier = threading.Barrier(len(folders))
    outcomes = [None] * len(folders)

    def run(index: int, folder: str) -> None:
        barrier.wait()
        try:
            outcomes[index] = claim(store, folder=folder)
        except StoreError as exc:
            outcomes[index] = exc

    threads = []
    for index, folder in enumerate(folders):
        threads.append(threading.Thread(target=run, args=(index, folder)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    return outcomes


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_two_first_claims_from_different_folders_one_wins(store: Store) -> None:
    outcomes = claim_at_the_same_time(store, ["jaffle-shop", "jaffle-shop-2"])

    errors = [o for o in outcomes if isinstance(o, StoreError)]
    wins = [o for o in outcomes if not isinstance(o, StoreError)]
    assert len(errors) == 1 and len(wins) == 1
    winner = wins[0].claim.folder
    assert wins[0].is_new is True
    assert f"is owned by folder '{winner}'" in str(errors[0])
    assert [(c.project, c.folder) for c in stored_claims(store)] == [("jaffle_shop", winner)]


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_two_first_claims_from_the_same_folder_both_succeed(store: Store) -> None:
    outcomes = claim_at_the_same_time(store, ["jaffle-shop", "jaffle-shop"])

    assert not any(isinstance(o, StoreError) for o in outcomes)
    assert sorted(o.is_new for o in outcomes) == [False, True]
    assert [(c.project, c.folder) for c in stored_claims(store)] == [("jaffle_shop", "jaffle-shop")]


# --------------------------------------------------------------------------------------
# find_claim / list_claims
# --------------------------------------------------------------------------------------


def test_find_claim(store: Store) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    with store.session() as session:
        assert projects.find_claim(session, "jaffle_shop") == Claim(
            "jaffle_shop", "jaffle-shop", OCT_1, "build-01", OCT_1
        )
        assert projects.find_claim(session, "no_such_project") is None


def test_list_claims_by_project_name(store: Store) -> None:
    add_claim(store, "zeta", "z")
    add_claim(store, "alpha", "a", host=None)
    with store.session() as session:
        assert projects.list_claims(session) == [
            Claim("alpha", "a", OCT_1, None, OCT_1),
            Claim("zeta", "z", OCT_1, "build-01", OCT_1),
        ]


def test_list_claims_when_there_are_none(store: Store) -> None:
    with store.session() as session:
        assert projects.list_claims(session) == []


# --------------------------------------------------------------------------------------
# rebind_project
# --------------------------------------------------------------------------------------


def test_rebind_moves_the_claim(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop", last_seen_at=OCT_1)
    add_claim(store, "other_shop", "other-shop")
    monkeypatch.setattr(projects, "current_host", lambda: "admin-box")

    with store.begin() as session:
        before, after = projects.rebind_project(session, "jaffle_shop", "jaffle-shop-new")

    assert before == Claim("jaffle_shop", "jaffle-shop", OCT_1, "build-01", OCT_1)
    assert after == Claim("jaffle_shop", "jaffle-shop-new", CLOCK_START, "admin-box", OCT_1)
    assert stored_claims(store) == [after, Claim("other_shop", "other-shop", OCT_1, "build-01", OCT_1)]


def test_rebind_to_the_same_folder_changes_nothing(store: Store) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    with store.begin() as session:
        before, after = projects.rebind_project(session, "jaffle_shop", "jaffle-shop")
    unchanged = Claim("jaffle_shop", "jaffle-shop", OCT_1, "build-01", OCT_1)
    assert (before, after) == (unchanged, unchanged)
    assert stored_claims(store) == [unchanged]


def test_rebind_an_unclaimed_name_is_an_error(store: Store) -> None:
    with pytest.raises(StoreError, match="^project name 'jaffle_shop' has no claim in this database$"):
        with store.begin() as session:
            projects.rebind_project(session, "jaffle_shop", "jaffle-shop")
