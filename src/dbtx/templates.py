"""Named HTML report templates.

A template is a Jinja file named `<name>_template.html.j2` or `<name>_template.html`.
Templates come from two kinds of folders, resolved once at start-up into a
`TemplateRegistry`:

  * integrated: shipped with dbtx (`src/dbtx/templates/`)
  * project:    folders listed in dbt_project.yml under `vars.dbtx.template_paths`

A project template replaces an integrated one of the same name. The same name in two
project folders, or twice in one folder (`.html` and `.html.j2`), is an error.

A report picks its template with `template: <name>`, or points at one exact file with
`template_path:`, which takes precedence over `template`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml
from jinja2 import BaseLoader, Environment, StrictUndefined, Template, TemplateNotFound
from jinja2 import TemplateError as JinjaTemplateError

TEMPLATE_SUFFIXES = ("_template.html.j2", "_template.html")
INTEGRATED_DIR = Path(__file__).parent / "templates"


class TemplateSource(str, Enum):
    PROJECT = "project"
    INTEGRATED = "integrated"
    EXPOSURE = "exposure"  # an exact file set on one report; never in the registry


@dataclass(frozen=True)
class TemplateEntry:
    name: str
    source: TemplateSource
    path: Path

    @property
    def uri(self) -> str:
        return self.path.as_uri()


class TemplateError(Exception):
    pass


def template_name(filename: str) -> str | None:
    """`finance_template.html.j2` -> `finance`; None for files that are not templates."""
    for suffix in TEMPLATE_SUFFIXES:
        if filename.endswith(suffix) and len(filename) > len(suffix):
            return filename[: -len(suffix)]
    return None


def scan_folder(folder: Path, source: TemplateSource) -> dict[str, TemplateEntry]:
    """Templates directly inside `folder` (subfolders are not searched)."""
    if not folder.is_dir():
        raise TemplateError(f"template folder not found: {folder}")
    found: dict[str, TemplateEntry] = {}
    for path in sorted(folder.iterdir()):
        name = template_name(path.name) if path.is_file() else None
        if name is None:
            continue
        if name in found:
            raise TemplateError(
                f"template '{name}' is defined twice in {folder}: "
                f"{found[name].path.name} and {path.name}"
            )
        found[name] = TemplateEntry(name, source, path.resolve())
    return found


def project_template_folders(project_dir: Path) -> list[Path]:
    """Folders from `vars.dbtx.template_paths` in dbt_project.yml, relative to the project."""
    project_yml = project_dir / "dbt_project.yml"
    data = yaml.safe_load(project_yml.read_text(encoding="utf-8")) or {}
    settings = ((data.get("vars") or {}).get("dbtx")) or {}
    paths = settings.get("template_paths") or []
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise TemplateError(
            f"vars.dbtx.template_paths in {project_yml} must be a list of folder paths"
        )
    return [project_dir / p for p in paths]


class TemplateRegistry:
    """Every named template available to a project, after precedence is applied."""

    def __init__(self, entries: dict[str, TemplateEntry]) -> None:
        self._entries = dict(sorted(entries.items()))

    @classmethod
    def build(
        cls, project_folders: Sequence[Path], integrated_folder: Path = INTEGRATED_DIR
    ) -> TemplateRegistry:
        entries = scan_folder(integrated_folder, TemplateSource.INTEGRATED)

        project: dict[str, TemplateEntry] = {}
        seen_folders: set[Path] = set()
        for folder in project_folders:
            resolved = folder.resolve()
            if resolved in seen_folders:
                raise TemplateError(f"template folder listed twice: {folder}")
            seen_folders.add(resolved)
            for name, entry in scan_folder(resolved, TemplateSource.PROJECT).items():
                if name in project:
                    raise TemplateError(
                        f"template '{name}' is defined in two project template folders: "
                        f"{project[name].path} and {entry.path}"
                    )
                project[name] = entry

        entries.update(project)  # project templates replace integrated ones
        return cls(entries)

    @classmethod
    def for_project(cls, project_dir: Path) -> TemplateRegistry:
        return cls.build(project_template_folders(project_dir))

    def get(self, name: str) -> TemplateEntry | None:
        return self._entries.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def __iter__(self) -> Iterator[TemplateEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def rows(self) -> list[tuple[str, str, str]]:
        """(name, source, uri) for each template."""
        return [(e.name, e.source.value, e.uri) for e in self]


class _Loader(BaseLoader):
    """Loads registry templates by name (so `{% extends "table" %}` works) and
    exact-file templates by their URI."""

    def __init__(self, registry: TemplateRegistry) -> None:
        self.registry = registry
        self.exact: dict[str, TemplateEntry] = {}

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str, Callable[[], bool]]:
        entry = self.registry.get(template) or self.exact.get(template)
        if entry is None:
            raise TemplateNotFound(template)
        mtime = entry.path.stat().st_mtime
        source = entry.path.read_text(encoding="utf-8")
        return source, str(entry.path), lambda: entry.path.stat().st_mtime == mtime


class TemplateEngine:
    def __init__(self, registry: TemplateRegistry) -> None:
        self.registry = registry
        self._loader = _Loader(registry)
        self.env = Environment(loader=self._loader, autoescape=True, undefined=StrictUndefined)

    def resolve(self, name: str | None, path: Path | None) -> TemplateEntry:
        """Pick a report's template: an exact `path` wins over a registry `name`."""
        if path is not None:
            if not path.is_file():
                raise TemplateError(f"template_path not found: {path}")
            entry = TemplateEntry(path.name, TemplateSource.EXPOSURE, path.resolve())
            self._loader.exact[self._key(entry)] = entry
            return entry
        if name is None:
            raise TemplateError("no template given")
        entry = self.registry.get(name)
        if entry is None:
            available = ", ".join(f"{e.name} ({e.source.value})" for e in self.registry)
            raise TemplateError(f"unknown template '{name}'; available: {available or 'none'}")
        return entry

    def load(self, entry: TemplateEntry) -> Template:
        """Compile (and cache) a template; syntax errors surface as TemplateError."""
        try:
            return self.env.get_template(self._key(entry))
        except JinjaTemplateError as exc:
            line = f", line {exc.lineno}" if getattr(exc, "lineno", None) else ""
            raise TemplateError(f"{entry.path}{line}: {exc.message}") from exc

    @staticmethod
    def _key(entry: TemplateEntry) -> str:
        return entry.name if entry.source is not TemplateSource.EXPOSURE else entry.uri
