"""`dbtx docs` subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from dbtx.docs import injector, sidecar
from dbtx.docs.catalog import DEFAULT_JOB_ID_VAR
from dbtx.docs.patch import PatchError, patch, status

USAGE = """\
dbtx docs patch   --docs-loc DIR --run-loc DIR [--history N] [--force] [--no-overlay]
                  [--job-id-var NAME]
dbtx docs install --docs-loc DIR [--uninstall]
dbtx docs status  --docs-loc DIR
dbtx docs serve   --docs-loc DIR [--host HOST] [--port N] [--browser]

Any other `dbtx docs` subcommand, `generate` included, is passed through to dbt.
`serve` needs the serve extra: pip install 'dbtx[serve]'
"""


def _rel(path: Path) -> str:
    """Path relative to the working directory where that reads more clearly."""
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _opt(args: List[str], name: str) -> Optional[str]:
    if name not in args:
        return None
    i = args.index(name)
    if i + 1 >= len(args):
        raise PatchError(f"{name} needs a value")
    return args[i + 1]


def _docs_loc(args: List[str]) -> Path:
    value = _opt(args, "--docs-loc")
    if value is None:
        raise PatchError("--docs-loc is required")
    return Path(value)


def run(args: List[str]) -> int:
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0

    sub, rest = args[0], args[1:]
    try:
        if sub == "patch":
            return _patch(rest)
        if sub == "install":
            return _install(rest)
        if sub == "status":
            return _status(rest)
        if sub == "serve":
            return _serve(rest)
    except (PatchError, sidecar.SidecarError, injector.InjectionError) as exc:
        print(f"dbtx docs: {exc}")
        return 2

    print(f"dbtx docs: unknown subcommand {sub!r}\n\n{USAGE}", end="")
    return 2


def _patch(args: List[str]) -> int:
    run_loc = _opt(args, "--run-loc")
    if run_loc is None:
        raise PatchError("--run-loc is required")
    history = _opt(args, "--history")
    job_id_var = _opt(args, "--job-id-var")

    result = patch(
        docs_loc=_docs_loc(args),
        run_loc=Path(run_loc),
        history_limit=int(history) if history is not None else None,
        force="--force" in args,
        install_overlay="--no-overlay" not in args,
        job_id_var=job_id_var or DEFAULT_JOB_ID_VAR,
    )

    print(f"dbtx docs: {result.tally}")
    print(f"  job {result.job_id}" if result.job_id
          else f"  no job id (run passed no {job_id_var or DEFAULT_JOB_ID_VAR!r} var)")
    print(f"  carried forward {result.hydrated} node(s) from previous runs")
    print(f"  history limit {result.history_limit}, wrote {_rel(result.sidecar_file)}")
    if result.overlay_installed:
        print("  installed runtime overlay into index.html")
    if result.tally.ignored:
        preview = ", ".join(result.tally.ignored[:3])
        suffix = ", ..." if len(result.tally.ignored) > 3 else ""
        print(f"  ignored (not in docs): {preview}{suffix}")
    if result.tally.superseded:
        print(f"  {len(result.tally.superseded)} node(s) already had newer data; "
              f"use --force to overwrite")
    return 0


def _install(args: List[str]) -> int:
    docs_loc = _docs_loc(args)
    if "--uninstall" in args:
        changed = injector.uninstall(docs_loc)
        print("dbtx docs: overlay removed" if changed else "dbtx docs: overlay was not installed")
        return 0
    index_html, asset = injector.install(docs_loc)
    print(f"dbtx docs: overlay installed\n  {_rel(index_html)}\n  {_rel(asset)}")
    return 0


def _status(args: List[str]) -> int:
    result = status(_docs_loc(args))
    print(f"dbtx docs: {_rel(result.docs_loc)}")
    print(f"  {result.with_runs} of {result.total_nodes} node(s) have run data")
    if result.stale:
        print(f"  {result.stale} stale (SQL changed since the recorded run)")
    if result.failing:
        print(f"  {len(result.failing)} not passing:")
        for item in result.failing[:10]:
            print(f"    {item}")
        if len(result.failing) > 10:
            print(f"    ... and {len(result.failing) - 10} more")
    print(f"  history limit {result.history_limit}")
    print(f"  last patched {result.updated_at or 'never'}")
    print(f"  overlay {'installed' if result.overlay_installed else 'not installed'}")
    return 0


def _serve(args: List[str]) -> int:
    # Imported here, not at module scope: the server needs the optional
    # `serve` extra, and `dbtx docs patch/install/status` must keep working
    # without it.
    try:
        from dbtx.docs import server
    except ImportError as exc:  # pragma: no cover - depends on install shape
        raise PatchError(
            "`dbtx docs serve` needs the serve extra: pip install 'dbtx[serve]'"
        ) from exc

    docs_loc = _docs_loc(args)
    port = _opt(args, "--port")
    try:
        port = int(port) if port is not None else server.DEFAULT_PORT
    except ValueError:
        raise PatchError(f"--port must be a number, got {port!r}") from None
    # The range itself is server.bind's to enforce, so a programmatic caller
    # gets the same check; this only relabels it as the option it came from.
    if not 0 <= port <= server.MAX_PORT:
        raise PatchError(f"--port must be between 0 and {server.MAX_PORT}, got {port}")

    try:
        server.serve(
            docs_loc,
            host=_opt(args, "--host") or server.DEFAULT_HOST,
            port=port,
            open_browser="--browser" in args,
        )
    except server.ServerError as exc:
        raise PatchError(str(exc)) from exc
    return 0
