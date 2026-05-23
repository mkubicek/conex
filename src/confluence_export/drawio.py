"""Draw.io diagram detection, extraction, and rendering."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from confluence_export.types import Attachment


@dataclass(frozen=True)
class DrawioMacroRef:
    """Draw.io-like macro reference discovered in Confluence storage HTML."""

    macro_name: str
    diagram_name: str


def find_drawio_attachments(attachments: list[Attachment]) -> list[Attachment]:
    """Find draw.io diagram attachments."""
    return [
        a
        for a in attachments
        if a.title.endswith(".drawio")
        or a.media_type == "application/x-drawio"
        or "drawio" in a.media_type
    ]


def find_drawio_macro_refs(html: str) -> list[DrawioMacroRef]:
    """Extract draw.io macro references from Confluence storage HTML."""
    soup = BeautifulSoup(html, "html.parser")
    refs: list[DrawioMacroRef] = []
    for macro in soup.find_all("ac:structured-macro"):
        macro_name = macro.get("ac:name", "")
        if macro_name not in {"drawio", "inc-drawio", "drawio-sketch"}:
            continue
        name_param = macro.find("ac:parameter", attrs={"ac:name": "diagramName"})
        diagram_name = name_param.get_text().strip() if name_param else macro_name
        refs.append(DrawioMacroRef(macro_name=macro_name, diagram_name=diagram_name))
    return refs


def detect_drawio_macros(html: str) -> list[str]:
    """Extract diagramName values from drawio structured macros in HTML."""
    return [
        ref.diagram_name
        for ref in find_drawio_macro_refs(html)
        if ref.macro_name in {"drawio", "inc-drawio"}
    ]


def drawio_name_candidates(diagram_name: str) -> list[str]:
    """Return likely attachment names for a draw.io macro diagram name."""
    name = diagram_name.strip()
    if not name:
        return []
    candidates = [name]
    if name.endswith(".drawio"):
        candidates.append(name.removesuffix(".drawio"))
    else:
        candidates.append(f"{name}.drawio")
    return list(dict.fromkeys(candidates))


def find_drawio_attachment(
    attachments: list[Attachment], diagram_name: str
) -> Attachment | None:
    """Find the source attachment referenced by a draw.io macro."""
    drawio_attachments = find_drawio_attachments(attachments)
    for candidate in drawio_name_candidates(diagram_name):
        for att in drawio_attachments:
            if att.title == candidate:
                return att
    return None


def find_drawio_cli() -> str | None:
    """Find the draw.io CLI executable."""
    # Try common names
    for name in ("drawio", "draw.io"):
        path = shutil.which(name)
        if path:
            return path

    # macOS app bundle
    mac_path = "/Applications/draw.io.app/Contents/MacOS/draw.io"
    if Path(mac_path).exists():
        return mac_path

    return None


def render_drawio_to_png(drawio_path: Path, output_path: Path | None = None) -> Path | None:
    """Render a .drawio file to PNG using the draw.io CLI.

    Returns the output path on success, None if draw.io CLI is not available.
    """
    cli = find_drawio_cli()
    if not cli:
        print(
            "  draw.io CLI not found, skipping PNG render. "
            "Install draw.io desktop app for automatic rendering.",
            file=sys.stderr,
        )
        return None

    if output_path is None:
        output_path = drawio_path.with_suffix(".drawio.png")

    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    try:
        proc = subprocess.Popen(
            [
                cli,
                "--export",
                "--format", "png",
                "--no-sandbox",
                "--output", str(output_path),
                str(drawio_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        print(f"  Warning: draw.io render failed for {drawio_path.name}: {exc}", file=sys.stderr)
        return None

    # Poll for output file or process exit, whichever comes first.
    # draw.io writes the PNG before its Electron cleanup, which can hang.
    # try/finally guarantees the subprocess is reaped even if the caller
    # raises (KeyboardInterrupt mid-export would otherwise leak Electron procs).
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
            ret = proc.poll()
            if ret is not None:
                break
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    print(f"  Warning: draw.io produced no output for {drawio_path.name}", file=sys.stderr)
    return None
