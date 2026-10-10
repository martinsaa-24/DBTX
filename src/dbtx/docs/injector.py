"""Installs the runtime overlay into a dbt-generated `index.html`.

dbt's docs site fetches `manifest.json` and `catalog.json` as siblings of
`index.html` at page load, so the overlay follows the same shape: a sibling
`dbtx_docs.js` plus one `<script>` tag. The HTML edit is marker-delimited and
idempotent, which keeps it reversible and makes re-installing after a
`dbt docs generate` a single cheap operation.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ASSET_FILENAME: str = "dbtx_docs.js"
MARKER_START: str = "<!-- dbtx:runtime-overlay:start -->"
MARKER_END: str = "<!-- dbtx:runtime-overlay:end -->"

_BLOCK_RE = re.compile(
    re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
    re.DOTALL,
)


class InjectionError(Exception):
    """Raised when the docs HTML cannot be instrumented."""


def asset_source() -> Path:
    return Path(__file__).parent / "assets" / ASSET_FILENAME


def asset_version(source: str) -> str:
    """Short content hash of the overlay asset, used to bust the browser cache.

    A hash rather than a timestamp: the published URL changes exactly when the
    asset's contents change, so a reinstall that ships identical JS stays
    cacheable, while one that ships new JS takes effect without the reader
    having to force-reload.
    """
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _snippet(version: str) -> str:
    return (
        f'{MARKER_START}'
        f'<script src="{ASSET_FILENAME}?v={version}"></script>'
        f'{MARKER_END}'
    )


def is_static_bundle(html: str) -> bool:
    """Whether this looks like `dbt docs generate --static` output.

    A static bundle inlines the artifacts instead of fetching them, so there is
    no sibling JSON for the overlay to read and patching is pointless.
    """
    return 'src="manifest.json' not in html and "manifest.json" not in html


def is_installed(index_html: Path) -> bool:
    if not Path(index_html).exists():
        return False
    return MARKER_START in Path(index_html).read_text(encoding="utf-8", errors="replace")


def install(docs_loc: Path) -> tuple[Path, Path]:
    """Writes the overlay asset into `docs_loc` and references it from index.html.

    Re-running replaces the existing marker block rather than appending a
    second one, so this is safe to call after every `dbt docs generate`.
    Returns the (index.html, asset) paths written.
    """
    docs_loc = Path(docs_loc)
    index_html = docs_loc / "index.html"
    if not index_html.exists():
        raise InjectionError(f"no index.html in {docs_loc} -- run `dbt docs generate` first")

    html = index_html.read_text(encoding="utf-8", errors="replace")
    if is_static_bundle(html):
        raise InjectionError(
            f"{index_html} looks like `--static` output, which inlines its data; "
            "the overlay needs the default docs build that fetches manifest.json")

    source = asset_source().read_text(encoding="utf-8")
    asset_dest = docs_loc / ASSET_FILENAME
    asset_dest.write_text(source, encoding="utf-8")

    snippet = _snippet(asset_version(source))
    if _BLOCK_RE.search(html):
        # `sub` would read a backslash in the replacement as an escape, and a
        # hash never contains one, but the literal form keeps that guaranteed.
        patched = _BLOCK_RE.sub(lambda _m: snippet, html)
    elif "</body>" in html:
        # Last thing before </body>, so the docs app has already defined itself.
        patched = html.replace("</body>", snippet + "</body>", 1)
    else:
        patched = html + snippet

    if patched != html:
        index_html.write_text(patched, encoding="utf-8")
    return index_html, asset_dest


def uninstall(docs_loc: Path) -> bool:
    """Removes the marker block and the overlay asset. Returns whether anything changed."""
    docs_loc = Path(docs_loc)
    index_html = docs_loc / "index.html"
    changed = False

    if index_html.exists():
        html = index_html.read_text(encoding="utf-8", errors="replace")
        patched = _BLOCK_RE.sub("", html)
        if patched != html:
            index_html.write_text(patched, encoding="utf-8")
            changed = True

    asset = docs_loc / ASSET_FILENAME
    if asset.exists():
        asset.unlink()
        changed = True

    return changed
