"""Report definitions (declared on dbt exposures) and rendering."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dbtx.templates import TemplateEngine, TemplateEntry

RELATION_RESOURCE_TYPES = {"model", "seed", "snapshot"}


class ReportConfig(BaseModel):
    """The `meta.dbtx.report` block of an exposure."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None,
        description="Name of the upstream node to query. Optional when the exposure has one.",
    )
    columns: list[str] | None = None
    where: str | None = None
    order_by: str | None = None
    limit: int | None = Field(default=None, gt=0)
    formats: list[Literal["html", "csv"]] = Field(default_factory=lambda: ["html"])
    template: str | None = Field(
        default=None, description="Name of a project or integrated template, e.g. `table`."
    )
    template_path: str | None = Field(
        default=None,
        description="An exact template file, relative to the dbt project. Wins over `template`.",
    )
    template_params: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form options for the template, available to it as `params`.",
    )

    @model_validator(mode="after")
    def _html_needs_a_template(self) -> ReportConfig:
        if "html" in self.formats and not (self.template or self.template_path):
            raise ValueError(
                "html output needs `template` (a template name) or `template_path` "
                "(a file); there is no default template"
            )
        return self


@dataclass(frozen=True)
class Report:
    exposure_id: str
    name: str
    title: str
    description: str
    owner: str
    relation: str
    parents: frozenset[str]
    config: ReportConfig
    template: TemplateEntry | None  # None when the report has no html output

    def sql(self) -> str:
        columns = ", ".join(self.config.columns) if self.config.columns else "*"
        sql = f"select {columns} from {self.relation}"
        if self.config.where:
            sql += f" where {self.config.where}"
        if self.config.order_by:
            sql += f" order by {self.config.order_by}"
        if self.config.limit:
            sql += f" limit {self.config.limit}"
        return sql


class ReportDefinitionError(Exception):
    pass


def _report_meta(exposure: Any) -> dict | None:
    # dbt >= 1.10 prefers `config.meta`; top-level `meta` is still merged in older projects.
    for meta in (getattr(exposure.config, "meta", None), exposure.meta):
        if meta and isinstance(meta.get("dbtx"), dict) and "report" in meta["dbtx"]:
            return meta["dbtx"]["report"] or {}
    return None


def _describe(exc: ValidationError) -> str:
    """One `field: problem` phrase per pydantic error, without pydantic's boilerplate."""
    parts = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        parts.append(f"{field}: {message}" if field else message)
    return "; ".join(parts)


def discover_reports(
    manifest: Any, templates: TemplateEngine, project_dir: Path
) -> dict[str, Report]:
    """Return every exposure that declares `meta.dbtx.report`, keyed by exposure unique_id.

    Each report's template is resolved and compiled here, so a missing or broken template
    fails before dbt builds anything.
    """
    reports: dict[str, Report] = {}
    errors: list[str] = []

    for uid, exposure in manifest.exposures.items():
        raw = _report_meta(exposure)
        if raw is None:
            continue
        try:
            try:
                config = ReportConfig.model_validate(raw)
            except ValidationError as exc:
                raise ReportDefinitionError(_describe(exc)) from None
            parents = [
                manifest.nodes[p]
                for p in exposure.depends_on.nodes
                if p in manifest.nodes and manifest.nodes[p].resource_type in RELATION_RESOURCE_TYPES
            ]
            if config.model:
                matches = [p for p in parents if p.name == config.model]
                if not matches:
                    raise ReportDefinitionError(
                        f"model '{config.model}' is not in the exposure's depends_on"
                    )
                source = matches[0]
            elif len(parents) == 1:
                source = parents[0]
            else:
                raise ReportDefinitionError(
                    f"exposure depends on {len(parents)} models; set `model:` to pick one"
                )
            template = None
            if "html" in config.formats:
                path = project_dir / config.template_path if config.template_path else None
                template = templates.resolve(config.template, path)
                templates.load(template)
        except Exception as exc:  # pydantic.ValidationError, ReportDefinitionError, TemplateError
            errors.append(f"{exposure.name}: {exc}")
            continue

        reports[uid] = Report(
            exposure_id=uid,
            name=exposure.name,
            title=exposure.label or exposure.name,
            description=exposure.description or "",
            owner=exposure.owner.name or exposure.owner.email or "",
            relation=source.relation_name,
            parents=frozenset(exposure.depends_on.nodes),
            config=config,
            template=template,
        )

    if errors:
        raise ReportDefinitionError("Invalid report definitions:\n  " + "\n  ".join(errors))
    return reports


def write_sql(report: Report, sql_dir: Path, sql: str | None = None) -> Path:
    """Write `sql` (default: the report's query) to `<sql_dir>/<report name>.sql`."""
    sql_dir.mkdir(parents=True, exist_ok=True)
    path = sql_dir / f"{report.name}.sql"
    path.write_text((report.sql() if sql is None else sql) + "\n", encoding="utf-8")
    return path


def render(
    report: Report,
    columns: list[str],
    rows: list[tuple],
    output_dir: Path,
    templates: TemplateEngine,
) -> list[Path]:
    """Write the report in each configured format and return the written paths.

    HTML templates receive: `report`, `columns`, `rows` (tuples), `records` (dicts keyed
    by column), `params` (the report's `template_params`) and `generated_at`.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fmt in report.config.formats:
        path = output_dir / f"{report.name}.{fmt}"
        if fmt == "csv":
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(rows)
        elif fmt == "html":
            assert report.template is not None  # guaranteed by ReportConfig validation
            html = templates.load(report.template).render(
                report=report,
                columns=columns,
                rows=rows,
                records=[dict(zip(columns, row)) for row in rows],
                params=report.config.template_params,
                generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            )
            path.write_text(html, encoding="utf-8")
        written.append(path)
    return written
