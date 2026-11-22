"""Version 1 JSON result for a single export invocation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from conex.errors import ConexError
from conex.store.state import ExportIssue


def report_destination(path: Path, root: Path) -> Path:
    # Inside the export, prevent replacing page output, media or internal state.
    path = path.expanduser().absolute()
    resolved = path.resolve()
    if resolved.is_relative_to(root):
        parts = {p.casefold() for p in resolved.relative_to(root).parts}
        if parts & {".conex", ".git", ".media", ".gitignore"} or resolved.suffix.lower() in {".md", ".html"}:
            raise ConexError("choose a report path outside managed content or state")
    return path


class ReportScope(BaseModel):
    path: str | None
    no_children: bool
    media: bool
    include_archived: bool
    include_html: bool
    cached: bool


class ExportReport(BaseModel):
    schema_version: int = 1
    space_key: str
    scope: ReportScope
    status: str = "failed"
    counts: dict[str, int] | None = None
    issues: list[ExportIssue] = []


def write_report(path: Path, report: ExportReport) -> None:
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(report.model_dump_json(indent=2))
            stream.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise ConexError(f"could not write export report at {path}: {exc}") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
