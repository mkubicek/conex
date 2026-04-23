"""Draw.io diagram detection, extraction, and rendering."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from confluence_export.media import MEDIA_DIR_NAME

from confluence_export.types import Attachment


def find_drawio_attachments(attachments: list[Attachment]) -> list[Attachment]:
    """Find draw.io diagram attachments."""
    return [
        a
        for a in attachments
        if a.title.endswith(".drawio")
        or a.media_type == "application/x-drawio"
        or "drawio" in a.media_type
    ]


def detect_drawio_macros(html: str) -> list[str]:
    """Extract diagramName values from drawio structured macros in HTML."""
    pattern = re.compile(
        r'<ac:structured-macro[^>]*ac:name="drawio"[^>]*>.*?'
        r'<ac:parameter\s+ac:name="diagramName"[^>]*>([^<]+)</ac:parameter>',
        re.DOTALL,
    )
    return pattern.findall(html)


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
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if output_path.exists() and output_path.stat().st_size > 0:
            # File created — kill the (possibly hung) process and return
            proc.kill()
            proc.wait()
            return output_path
        ret = proc.poll()
        if ret is not None:
            # Process exited without creating the file
            break
        time.sleep(0.5)
    else:
        # Timeout — kill the process
        proc.kill()
        proc.wait()

    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    print(f"  Warning: draw.io produced no output for {drawio_path.name}", file=sys.stderr)
    return None


def replace_drawio_placeholders(
    markdown: str,
    rendered_diagrams: dict[str, Path],
    media_dir_name: str = MEDIA_DIR_NAME,
) -> str:
    """Replace [drawio:name] placeholders in markdown with image + source link."""
    for diagram_name, png_path in rendered_diagrams.items():
        bare_name = diagram_name.removesuffix(".drawio")
        placeholder = f"[drawio:{diagram_name}]"
        if placeholder not in markdown:
            placeholder = f"[drawio:{bare_name}]"
            if placeholder not in markdown:
                continue

        drawio_filename = diagram_name if diagram_name.endswith(".drawio") else f"{diagram_name}.drawio"
        png_filename = png_path.name

        replacement = (
            f"![{bare_name}]"
            f"({media_dir_name}/{png_filename})\n"
            f"*Draw.io source: [{drawio_filename}]({media_dir_name}/{drawio_filename})*"
        )
        markdown = markdown.replace(placeholder, replacement)

    return markdown
