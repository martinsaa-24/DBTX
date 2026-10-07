"""Orchestrates a docs patch: docs manifest -> prior state -> this run -> persist.

The ordering here is the contract. A catalog that parses only the incoming run
knows nothing about nodes that earlier runs covered, so hydrating the sidecar
between `parse_manifest` and `parse_run` is what makes a sequence of partial
runs accumulate rather than overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dbtx.docs import injector, sidecar
from dbtx.docs.catalog import DEFAULT_JOB_ID_VAR, DocsCatalog, PatchTally


class PatchError(Exception):
    """Raised when a patch cannot proceed."""


@dataclass
class PatchResult:
    docs_loc: Path
    run_loc: Path
    history_limit: int
    hydrated: int
    tally: PatchTally
    sidecar_file: Path
    overlay_installed: bool
    # The job id carried by the run just merged, or None if it passed no such
    # var. Reported back so a caller can confirm the tag was picked up.
    job_id: Optional[str] = None


@dataclass
class StatusResult:
    docs_loc: Path
    total_nodes: int
    with_runs: int
    stale: int
    history_limit: int
    updated_at: Optional[str]
    overlay_installed: bool
    failing: List[str] = field(default_factory=list)


def _require(path: Path, what: str) -> Path:
    if not path.exists():
        raise PatchError(f"{what} not found: {path}")
    return path


def patch(docs_loc: Path, run_loc: Path, history_limit: Optional[int] = None,
          force: bool = False, install_overlay: bool = True,
          job_id_var: str = DEFAULT_JOB_ID_VAR) -> PatchResult:
    """Merges the run artifacts in `run_loc` into the docs sidecar in `docs_loc`.

    `job_id_var` names the `--vars` key recorded as each result's job id.
    """
    docs_loc = Path(docs_loc)
    run_loc = Path(run_loc)

    docs_manifest = _require(docs_loc / "manifest.json", "docs manifest")
    run_results = _require(run_loc / "run_results.json", "run results")
    # Optional: without it, results are recorded with no checksum and staleness
    # cannot be detected.
    run_manifest = run_loc / "manifest.json"

    limit = sidecar.resolve_history_limit(docs_loc, history_limit)

    catalog = DocsCatalog(history_limit=limit)
    catalog.parse_manifest(str(docs_manifest))
    hydrated = sidecar.hydrate(catalog, docs_loc)
    tally = catalog.parse_run(
        str(run_results),
        str(run_manifest) if run_manifest.exists() else None,
        force=force,
        job_id_var=job_id_var,
    )
    sidecar_file = sidecar.write(catalog, docs_loc)

    overlay_installed = False
    if install_overlay and not injector.is_installed(docs_loc / "index.html"):
        injector.install(docs_loc)
        overlay_installed = True

    return PatchResult(
        docs_loc=docs_loc,
        run_loc=run_loc,
        history_limit=limit,
        hydrated=hydrated,
        tally=tally,
        sidecar_file=sidecar_file,
        overlay_installed=overlay_installed,
        job_id=catalog.run_job_id,
    )


def status(docs_loc: Path) -> StatusResult:
    """Summarises how much of the docs' node set currently carries run data."""
    docs_loc = Path(docs_loc)
    docs_manifest = _require(docs_loc / "manifest.json", "docs manifest")

    limit = sidecar.resolve_history_limit(docs_loc)
    catalog = DocsCatalog(history_limit=limit)
    catalog.parse_manifest(str(docs_manifest))
    sidecar.hydrate(catalog, docs_loc)

    with_runs = 0
    stale = 0
    failing: List[str] = []
    for node in catalog.nodes.values():
        head = node.run_result
        if head is None:
            continue
        with_runs += 1
        if head.stale:
            stale += 1
        if str(head.node_status).lower() not in ("success", "pass"):
            failing.append(f"{node.name} ({head.node_status})")

    raw = sidecar.read_sidecar(docs_loc)
    return StatusResult(
        docs_loc=docs_loc,
        total_nodes=len(catalog.nodes),
        with_runs=with_runs,
        stale=stale,
        history_limit=limit,
        updated_at=raw.get('updated_at'),
        overlay_installed=injector.is_installed(docs_loc / "index.html"),
        failing=failing,
    )
