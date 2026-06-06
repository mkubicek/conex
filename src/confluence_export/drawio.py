"""Draw.io diagram detection, extraction, and rendering."""

from __future__ import annotations

import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from confluence_export.filemode import default_file_mode, replacement_mode
from confluence_export.types import Attachment


def find_drawio_attachments(attachments: list[Attachment]) -> list[Attachment]:
    """Find draw.io diagram attachments."""
    return [
        a
        for a in attachments
        if a.title.casefold().endswith(".drawio")
        or a.media_type.casefold() == "application/x-drawio"
        or "drawio" in a.media_type.casefold()
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


def _replace_rendered_png(render_path: Path, output_path: Path, mode: int) -> bool:
    try:
        render_path.chmod(mode)
        os.replace(render_path, output_path)
    except OSError as exc:
        print(f"  Warning: draw.io render failed for {output_path.name}: {exc}", file=sys.stderr)
        try:
            render_path.unlink()
        except OSError:  # pragma: no cover
            pass
        return False
    return True


def _usable_png(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def render_drawio_to_png(
    drawio_path: Path,
    output_path: Path | None = None,
    *,
    force: bool = False,
) -> Path | None:
    """Render a .drawio file to PNG using the draw.io CLI.

    Returns the output path on success, None if draw.io CLI is not available.
    """
    if output_path is None:
        output_path = drawio_path.with_suffix(".drawio.png")

    if not force and _usable_png(output_path):
        return output_path

    cli = find_drawio_cli()
    if not cli:
        if _usable_png(output_path):
            print(
                f"  Warning: draw.io CLI not found for {drawio_path.name}; "
                "keeping previous PNG",
                file=sys.stderr,
            )
            return output_path
        print(
            "  draw.io CLI not found, skipping PNG render. "
            "Install draw.io desktop app for automatic rendering.",
            file=sys.stderr,
        )
        return None

    if output_path.exists() and output_path.is_dir() and not output_path.is_symlink():
        print(
            f"  Warning: draw.io output path is a directory: {output_path.name}",
            file=sys.stderr,
        )
        return None

    mode = (
        replacement_mode(output_path)
        if output_path.exists() and output_path.is_file() and not output_path.is_symlink()
        else default_file_mode()
    )
    fd, tmp_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=".drawio-",
        suffix=".png",
    )
    os.close(fd)
    render_path = Path(tmp_name)

    try:
        proc = subprocess.Popen(
            [
                cli,
                "--export",
                "--format", "png",
                "--no-sandbox",
                "--output", str(render_path),
                str(drawio_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        try:
            render_path.unlink()
        except OSError:  # pragma: no cover
            pass
        print(f"  Warning: draw.io render failed for {drawio_path.name}: {exc}", file=sys.stderr)
        if force and _usable_png(output_path):
            print(
                f"  Warning: keeping previous PNG for {drawio_path.name}",
                file=sys.stderr,
            )
            return output_path
        return None

    # Poll for process exit or stable output. draw.io writes the PNG before its
    # Electron cleanup, which can hang; requiring a repeated size/mtime avoids
    # replacing a previous PNG with a file that is still being written.
    # try/finally guarantees the subprocess is reaped even if the caller
    # raises (KeyboardInterrupt mid-export would otherwise leak Electron procs).
    observed_output: tuple[int, int] | None = None
    last_return_code: int | None = None
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ret = proc.poll()
            if ret is not None:
                last_return_code = ret
            if _usable_png(render_path):
                stat = render_path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                output_is_stable = ret is None and signature == observed_output
                if ret == 0 or output_is_stable:
                    return (
                        output_path
                        if _replace_rendered_png(render_path, output_path, mode)
                        else None
                    )
                if ret is not None:
                    break
                observed_output = signature
            if ret is not None:
                break
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if (
        _usable_png(render_path)
        and last_return_code == 0
    ):
        return (
            output_path
            if _replace_rendered_png(render_path, output_path, mode)
            else None
        )

    if force:
        try:
            render_path.unlink()
        except OSError:  # pragma: no cover
            pass
        if _usable_png(output_path):
            print(
                f"  Warning: draw.io produced no replacement for {drawio_path.name}; "
                "keeping previous PNG",
                file=sys.stderr,
            )
            return output_path

    try:
        render_path.unlink()
    except OSError:  # pragma: no cover
        pass

    print(f"  Warning: draw.io produced no output for {drawio_path.name}", file=sys.stderr)
    return None
