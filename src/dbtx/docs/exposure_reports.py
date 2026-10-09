"""Finds the report files `dbtx build` rendered, for the docs overlay to embed.

`dbtx docs patch` discovers these from disk rather than being told about them by
the build. The sidecar is dbtx-owned and rebuilt on every patch, so a report
that was re-rendered, removed, or never produced is reflected as it actually is,
and `dbtx build` stays free of any knowledge of where the docs live.

Reports are expected beside the docs, under `<docs_loc>/reports/`, which is
where `dbt docs generate` and `dbtx build` already both write when the docs
location is the project's `target/`. Being a sibling of `index.html` is what
makes them fetchable by the overlay: `dbt docs serve` serves that directory, so
no copying and no second origin is involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

REPORTS_SUBDIR: str = "reports"


@dataclass(frozen=True)
class ReportAsset:
    """One exposure's rendered report, as the docs site can reach it.

    Paths are relative to the docs location and always forward-slashed: they are
    used as URLs by the overlay, not reopened as files.
    """

    html: str
    csv: Optional[str] = None
    generated_at: Optional[str] = None
    size_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            'html': self.html,
            'csv': self.csv,
            'generated_at': self.generated_at,
            'size_bytes': self.size_bytes,
        }

    @classmethod
    def from_dict(cls, dat: dict) -> "ReportAsset":
        return cls(
            html=dat['html'],
            csv=dat.get('csv'),
            generated_at=dat.get('generated_at'),
            size_bytes=dat.get('size_bytes', 0),
        )


def _mtime(path: Path) -> Optional[str]:
    """The file's modification time, ISO-8601 UTC.

    The render time is not recorded inside the HTML in any machine-readable way,
    and the run that produced it may not be the run being patched, so the file's
    own timestamp is the honest answer to "how current is this?".
    """
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return stamp.isoformat().replace('+00:00', 'Z')


def _exposure_name(unique_id: str, json_dat: Optional[dict] = None) -> str:
    """The exposure's own name, which is what `dbtx build` names its files after.

    Taken from the manifest where available; the unique_id's last segment is the
    same value, since dbt builds the id as `exposure.<project>.<name>` and
    neither project nor exposure names may contain a dot.
    """
    if json_dat:
        name = json_dat.get('name')
        if name:
            return str(name)
    return unique_id.rsplit('.', 1)[-1]


def discover(nodes: Dict[str, object], docs_loc: Path,
             subdir: str = REPORTS_SUBDIR) -> Dict[str, ReportAsset]:
    """Reports under `<docs_loc>/<subdir>` belonging to an exposure in `nodes`.

    Keyed by exposure unique_id. Looked up per exposure by name rather than by
    listing the directory and inferring owners, so a stray file cannot be
    attributed to an exposure and a report whose exposure has left the manifest
    is simply not picked up.

    A CSV sitting beside the HTML is recorded alongside it; the HTML is what
    makes a report discoverable, so a CSV-only report is not reported here --
    there would be nothing for the overlay to embed.
    """
    reports_dir = Path(docs_loc) / subdir
    if not reports_dir.is_dir():
        return {}

    found: Dict[str, ReportAsset] = {}
    for unique_id, node in nodes.items():
        if not unique_id.startswith("exposure."):
            continue
        name = _exposure_name(unique_id, getattr(node, 'json_dat', None))
        html = reports_dir / f"{name}.html"
        if not html.is_file():
            continue
        csv = reports_dir / f"{name}.csv"
        found[unique_id] = ReportAsset(
            html=f"{subdir}/{name}.html",
            csv=f"{subdir}/{name}.csv" if csv.is_file() else None,
            generated_at=_mtime(html),
            size_bytes=html.stat().st_size,
        )
    return found
