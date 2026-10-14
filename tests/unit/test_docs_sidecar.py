"""Sidecar accumulation across partial runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbtx.docs import injector, sidecar
from dbtx.docs.catalog import read_job_id
from dbtx.docs.patch import PatchResult, patch, status

NODES = ["a", "b", "c", "d", "e", "f"]


def _manifest(names: list[str], invocation: str = "docs-inv", checksums: dict[str, str] | None = None) -> dict:
    checksums = checksums or {}
    return {
        "metadata": {"invocation_id": invocation, "generated_at": "2026-01-01T00:00:00Z"},
        "nodes": {
            f"model.proj.{n}": {
                "resource_type": "model",
                "checksum": {"name": "sha256", "checksum": checksums.get(n, f"sum-{n}")},
            }
            for n in names
        },
    }


def _run_results(names: list[str], invocation: str, generated_at: str, status_: str = "success",
                 job_id: str | None = None, args_vars: dict | None = None,
                 env: dict[str, str] | None = None) -> dict:
    """Mirrors what dbt writes: `--vars` land verbatim in top-level `args`."""
    if args_vars is None:
        args_vars = {"job_id": job_id} if job_id is not None else {}
    return {
        "metadata": {
            "invocation_id": invocation,
            "generated_at": generated_at,
            "env": env or {},
        },
        "args": {"which": "build", "vars": args_vars},
        "results": [
            {
                "unique_id": f"model.proj.{n}",
                "status": status_,
                "execution_time": 1.0,
                "timing": [
                    {"name": "compile", "completed_at": f"{generated_at[:-1]}.1Z"},
                    {"name": "execute", "completed_at": f"{generated_at[:-1]}.2Z"},
                ],
            }
            for n in names
        ],
    }


def _write(loc: Path, name: str, dat: dict) -> None:
    loc.mkdir(parents=True, exist_ok=True)
    (loc / name).write_text(json.dumps(dat), encoding="utf-8")


@pytest.fixture
def docs_loc(tmp_path: Path) -> Path:
    loc = tmp_path / "loc_docs"
    _write(loc, "manifest.json", _manifest(NODES))
    # A minimal stand-in for the dbt docs bundle, enough for the injector.
    (loc / "index.html").write_text(
        '<html><body><script src="manifest.json"></script></body></html>', encoding="utf-8"
    )
    return loc


def _do_run(tmp_path: Path, docs_loc: Path, names: list[str], invocation: str, generated_at: str,
            job_id: str | None = None, args_vars: dict | None = None,
            env: dict[str, str] | None = None, **kw: object) -> PatchResult:
    run_loc = tmp_path / "loc_run"
    _write(run_loc, "manifest.json", _manifest(NODES, invocation=invocation))
    _write(run_loc, "run_results.json",
           _run_results(names, invocation, generated_at, job_id=job_id,
                        args_vars=args_vars, env=env))
    return patch(docs_loc=docs_loc, run_loc=run_loc, **kw)


def _recorded(docs_loc: Path) -> dict:
    return json.loads((docs_loc / sidecar.SIDECAR_FILENAME).read_text(encoding="utf-8"))["nodes"]


def test_state0_has_no_sidecar(docs_loc: Path) -> None:
    assert not (docs_loc / sidecar.SIDECAR_FILENAME).exists()
    assert status(docs_loc).with_runs == 0


def test_partial_runs_accumulate(tmp_path: Path, docs_loc: Path) -> None:
    # STATE-1: run 1 covers a, b, c
    _do_run(tmp_path, docs_loc, ["a", "b", "c"], "inv-1", "2026-01-02T00:00:00Z")
    assert set(_recorded(docs_loc)) == {"model.proj.a", "model.proj.b", "model.proj.c"}

    # STATE-2: run 2 covers c, d, e -- a and b must survive untouched
    _do_run(tmp_path, docs_loc, ["c", "d", "e"], "inv-2", "2026-01-03T00:00:00Z")
    recorded = _recorded(docs_loc)

    assert set(recorded) == {f"model.proj.{n}" for n in "abcde"}
    assert "model.proj.f" not in recorded, "f never ran and must not appear"

    # a kept run 1; c advanced to run 2 and kept run 1 as history
    assert recorded["model.proj.a"]["runs"][0]["run_invocation_id"] == "inv-1"
    assert recorded["model.proj.c"]["runs"][0]["run_invocation_id"] == "inv-2"
    assert recorded["model.proj.c"]["runs"][1]["run_invocation_id"] == "inv-1"
    assert len(recorded["model.proj.d"]["runs"]) == 1


def test_history_is_capped_and_configurable(tmp_path: Path, docs_loc: Path) -> None:
    for i in range(1, 6):
        _do_run(tmp_path, docs_loc, ["a"], f"inv-{i}", f"2026-01-0{i}T00:00:00Z", history_limit=3)
    runs = _recorded(docs_loc)["model.proj.a"]["runs"]
    assert len(runs) == 3
    assert [r["run_invocation_id"] for r in runs] == ["inv-5", "inv-4", "inv-3"]

    # The cap persists in the sidecar, so later patches need not repeat it.
    assert sidecar.resolve_history_limit(docs_loc) == 3
    _do_run(tmp_path, docs_loc, ["a"], "inv-6", "2026-01-06T00:00:00Z")
    assert len(_recorded(docs_loc)["model.proj.a"]["runs"]) == 3

    # Lowering the cap truncates on the next write.
    _do_run(tmp_path, docs_loc, ["a"], "inv-7", "2026-01-07T00:00:00Z", history_limit=1)
    assert len(_recorded(docs_loc)["model.proj.a"]["runs"]) == 1


def test_repatching_same_run_is_idempotent(tmp_path: Path, docs_loc: Path) -> None:
    _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z")
    _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z")
    assert len(_recorded(docs_loc)["model.proj.a"]["runs"]) == 1


def test_older_run_does_not_roll_back(tmp_path: Path, docs_loc: Path) -> None:
    _do_run(tmp_path, docs_loc, ["a"], "inv-2", "2026-01-03T00:00:00Z")
    result = _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z")

    assert result.tally.superseded == ["model.proj.a"]
    assert _recorded(docs_loc)["model.proj.a"]["runs"][0]["run_invocation_id"] == "inv-2"

    forced = _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z", force=True)
    assert forced.tally.patched == ["model.proj.a"]


def test_nodes_absent_from_docs_are_ignored(tmp_path: Path, docs_loc: Path) -> None:
    run_loc = tmp_path / "loc_run"
    _write(run_loc, "manifest.json", _manifest(NODES + ["ghost"]))
    _write(run_loc, "run_results.json",
           _run_results(["a", "ghost"], "inv-1", "2026-01-02T00:00:00Z"))

    result = patch(docs_loc=docs_loc, run_loc=run_loc)

    assert result.tally.patched == ["model.proj.a"]
    assert result.tally.ignored == ["model.proj.ghost"]
    assert "model.proj.ghost" not in _recorded(docs_loc)


def test_checksum_change_marks_result_stale(tmp_path: Path, docs_loc: Path) -> None:
    _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z")
    assert _recorded(docs_loc)["model.proj.a"]["runs"][0]["stale"] is False

    # a's SQL changes in the docs, with no corresponding re-run
    _write(docs_loc, "manifest.json", _manifest(NODES, checksums={"a": "sum-a-v2"}))
    _do_run(tmp_path, docs_loc, ["b"], "inv-2", "2026-01-03T00:00:00Z")

    recorded = _recorded(docs_loc)
    assert recorded["model.proj.a"]["runs"][0]["stale"] is True
    assert recorded["model.proj.b"]["runs"][0]["stale"] is False
    assert status(docs_loc).stale == 1


def test_overlay_injection_is_idempotent(docs_loc: Path) -> None:
    injector.install(docs_loc)
    injector.install(docs_loc)

    html = (docs_loc / "index.html").read_text(encoding="utf-8")
    assert html.count(injector.MARKER_START) == 1
    assert (docs_loc / injector.ASSET_FILENAME).exists()

    assert injector.uninstall(docs_loc) is True
    html = (docs_loc / "index.html").read_text(encoding="utf-8")
    assert injector.MARKER_START not in html
    assert not (docs_loc / injector.ASSET_FILENAME).exists()


def test_script_tag_carries_the_asset_version(docs_loc: Path) -> None:
    injector.install(docs_loc)

    html = (docs_loc / "index.html").read_text(encoding="utf-8")
    source = (docs_loc / injector.ASSET_FILENAME).read_text(encoding="utf-8")
    version = injector.asset_version(source)

    assert f'src="{injector.ASSET_FILENAME}?v={version}"' in html
    # A hash, not a timestamp: reinstalling identical JS must not change the URL,
    # so the browser can keep caching it.
    injector.install(docs_loc)
    assert (docs_loc / "index.html").read_text(encoding="utf-8") == html


def test_asset_version_tracks_asset_contents(
    docs_loc: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector.install(docs_loc)
    before = (docs_loc / "index.html").read_text(encoding="utf-8")

    # Ship different JS; the query must change so readers stop getting the old
    # file from cache.
    new_source = "/* new overlay */\n"
    changed = docs_loc / "changed.js"
    changed.write_text(new_source, encoding="utf-8")
    monkeypatch.setattr(injector, "asset_source", lambda: changed)
    injector.install(docs_loc)

    after = (docs_loc / "index.html").read_text(encoding="utf-8")
    assert after != before
    assert after.count(injector.MARKER_START) == 1
    assert f"?v={injector.asset_version(new_source)}" in after
    assert (docs_loc / injector.ASSET_FILENAME).read_text(encoding="utf-8") == new_source


def test_patch_survives_docs_regeneration(tmp_path: Path, docs_loc: Path) -> None:
    """`dbt docs generate` overwrites manifest.json but not the sidecar."""
    _do_run(tmp_path, docs_loc, ["a", "b"], "inv-1", "2026-01-02T00:00:00Z")
    _write(docs_loc, "manifest.json", _manifest(NODES, invocation="docs-inv-2"))

    assert set(_recorded(docs_loc)) == {"model.proj.a", "model.proj.b"}
    assert status(docs_loc).with_runs == 2


# --------------------------------------------------------------- job ids
#
# `--vars` are recorded only in run_results' top-level `args`; manifest.json
# carries no args at all, so that is the one artifact a job id can be read from.


@pytest.mark.parametrize(
    "rr, expected",
    [
        ({"args": {"vars": {"job_id": "123v1"}}}, "123v1"),
        # Some dbt versions, and CI wrappers that rebuild the file, leave
        # `vars` as the YAML string it arrived as.
        ({"args": {"vars": "{job_id: 123v1}"}}, "123v1"),
        # Non-string values are normalised, so the pane never has to format them.
        ({"args": {"vars": {"job_id": 1234}}}, "1234"),
        # Fallback for projects tagging runs via DBT_ENV_CUSTOM_ENV_job_id.
        ({"metadata": {"env": {"job_id": "from-env"}}}, "from-env"),
        # An explicit var wins over the environment.
        (
            {"args": {"vars": {"job_id": "from-vars"}},
             "metadata": {"env": {"job_id": "from-env"}}},
            "from-vars",
        ),
        ({"args": {"vars": {}}, "metadata": {"env": {}}}, None),
        ({"args": {"vars": {"job_id": ""}}}, None),
        ({"args": {"vars": "::not valid yaml::"}}, None),
        ({"args": {"vars": None}}, None),
        ({}, None),
    ],
)
def test_read_job_id_sources(rr: dict, expected: str | None) -> None:
    assert read_job_id(rr) == expected


@pytest.mark.parametrize(
    "rr, expected",
    [
        # Platform mode: dbtx hands dbt DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID, recorded as DBTX_JOB_ID.
        ({"metadata": {"env": {"DBTX_JOB_ID": "platform-1"}}}, "platform-1"),
        # Standalone mode: the user may name the job with --vars '{DBTX_JOB_ID: ...}'.
        ({"args": {"vars": {"DBTX_JOB_ID": "standalone-1"}}}, "standalone-1"),
        ({"args": {"vars": "{DBTX_JOB_ID: standalone-2}"}}, "standalone-2"),
        # DBTX_JOB_ID wins over the legacy job_id var, from either source.
        ({"args": {"vars": {"job_id": "legacy"}}, "metadata": {"env": {"DBTX_JOB_ID": "platform-1"}}}, "platform-1"),
        ({"args": {"vars": {"job_id": "legacy", "DBTX_JOB_ID": "standalone-1"}}}, "standalone-1"),
        # The environment (set by dbtx) wins over vars.
        ({"args": {"vars": {"DBTX_JOB_ID": "from-vars"}}, "metadata": {"env": {"DBTX_JOB_ID": "from-env"}}}, "from-env"),
        ({"metadata": {"env": {"DBTX_JOB_ID": ""}}, "args": {"vars": {"job_id": "legacy"}}}, "legacy"),
    ],
)
def test_read_job_id_prefers_dbtx_job_id(rr: dict, expected: str | None) -> None:
    assert read_job_id(rr) == expected


def test_read_job_id_dbtx_job_id_wins_over_a_configured_var_name() -> None:
    rr = {"args": {"vars": {"ci_run": "RUN-9"}}, "metadata": {"env": {"DBTX_JOB_ID": "platform-1"}}}
    assert read_job_id(rr, "ci_run") == "platform-1"


def test_read_job_id_var_name_is_configurable() -> None:
    rr = {"args": {"vars": {"ci_run": "RUN-9", "job_id": "ignored"}}}
    assert read_job_id(rr, "ci_run") == "RUN-9"


def test_job_id_recorded_for_current_and_previous_runs(tmp_path: Path, docs_loc: Path) -> None:
    result = _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z",
                     job_id="123v1")
    assert result.job_id == "123v1"

    _do_run(tmp_path, docs_loc, ["a"], "inv-2", "2026-01-03T00:00:00Z", job_id="124v1")

    runs = _recorded(docs_loc)["model.proj.a"]["runs"]
    # Both identifiers travel together: the job id a reader recognises, and the
    # invocation id that uniquely pins the run.
    assert [(r["job_id"], r["run_invocation_id"]) for r in runs] == [
        ("124v1", "inv-2"),
        ("123v1", "inv-1"),
    ]


def test_run_without_the_var_records_no_job_id(tmp_path: Path, docs_loc: Path) -> None:
    result = _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z")

    assert result.job_id is None
    assert _recorded(docs_loc)["model.proj.a"]["runs"][0]["job_id"] is None


def test_job_id_var_name_flows_through_patch(tmp_path: Path, docs_loc: Path) -> None:
    result = _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z",
                     args_vars={"ci_run": "RUN-9"}, job_id_var="ci_run")

    assert result.job_id == "RUN-9"
    assert _recorded(docs_loc)["model.proj.a"]["runs"][0]["job_id"] == "RUN-9"


def test_job_id_read_from_env_fallback(tmp_path: Path, docs_loc: Path) -> None:
    result = _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z",
                     env={"job_id": "from-env"})

    assert result.job_id == "from-env"


def test_sidecar_without_job_ids_still_hydrates(tmp_path: Path, docs_loc: Path) -> None:
    """History written before job ids were tracked must survive a patch.

    The field is additive and the schema version is deliberately unchanged, so
    an existing sidecar keeps its accumulated runs instead of being rejected.
    """
    legacy = {
        "dbtx_schema_version": sidecar.SIDECAR_SCHEMA_VERSION,
        "history_limit": 3,
        "nodes": {
            "model.proj.a": {
                "runs": [{
                    "status": "success",
                    "execution_time": 1.0,
                    "last_compiled_at": "2026-01-01T00:00:00Z",
                    "last_ran_at": "2026-01-01T00:00:00Z",
                    "checksum": "sum-a",
                    "run_invocation_id": "inv-0",
                    "run_generated_at": "2026-01-01T00:00:00Z",
                    "stale": False,
                }]
            }
        },
    }
    _write(docs_loc, sidecar.SIDECAR_FILENAME, legacy)

    _do_run(tmp_path, docs_loc, ["a"], "inv-1", "2026-01-02T00:00:00Z", job_id="123v1")

    runs = _recorded(docs_loc)["model.proj.a"]["runs"]
    assert [(r["job_id"], r["run_invocation_id"]) for r in runs] == [
        ("123v1", "inv-1"),
        (None, "inv-0"),
    ]


def test_patch_command_reports_dbtx_job_id(
    tmp_path: Path, docs_loc: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dbtx.docs.command import run as docs_command

    run_loc = tmp_path / "loc_run"
    _write(run_loc, "run_results.json",
           _run_results(["a"], "inv-1", "2026-01-02T00:00:00Z", env={"DBTX_JOB_ID": "platform-1"}))
    assert docs_command(["patch", "--docs-loc", str(docs_loc), "--run-loc", str(run_loc)]) == 0
    assert "  job platform-1\n" in capsys.readouterr().out


def test_patch_command_names_both_job_id_sources_when_there_is_none(
    tmp_path: Path, docs_loc: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dbtx.docs.command import run as docs_command

    run_loc = tmp_path / "loc_run"
    _write(run_loc, "run_results.json", _run_results(["a"], "inv-1", "2026-01-02T00:00:00Z"))
    assert docs_command(["patch", "--docs-loc", str(docs_loc), "--run-loc", str(run_loc)]) == 0
    assert "  no job id (the run has no DBTX_JOB_ID and no 'job_id' var)\n" in capsys.readouterr().out
