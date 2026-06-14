"""Draw.io diagram detection and batch PNG rendering for conex v2.

Contracts:
- DRAWIO_RENDER_VERSION = 1 keys derived blobs in the snapshot's
  ``derived_blobs`` map (``"drawio-png:v{V}:{xml_digest}"``). Bump this
  constant when render parameters change to invalidate previously cached PNGs.
- find_drawio_pairs() pairs .drawio/.xml source attachments with their
  previewfreshness is determined by ``version.created_at`` TIMESTAMP
  comparison, NOT version numbers (not comparable across attachments).
- render_batch() materialises xml blobs under ``.conex/tmp``, invokes the
  real draw.io CLI:
      drawio --export --format png --no-sandbox --output <out> <in>
  (long flags; --no-sandbox is load-bearing for headless Electron).
  Folder-input mode is attempted first; if it fails or produces no output,
  falls back to a per-file loop with identical flags. Successful PNGs are
  stored in the blob store and the name->digest map is returned.
- ``shutil.which`` is cached at module level; if the CLI is absent the
  function returns {} and emits exactly ONE warning (no re-probe on
  subsequent calls).
- Tests must mock subprocess.run and shutil.which — never invoke a real
  binary.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import urllib.parse
import warnings
import zlib
from pathlib import Path

from conex.models import Attachment
from conex.store.blobs import BlobStore


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DRAWIO_RENDER_VERSION: int = 2
"""Bump when CLI flags or render parameters change to invalidate cached PNGs.

v2: renders are auto-scaled by font size (see _compute_render_scale), so 1x
caches from v1 must be re-rendered.
"""

_RENDER_TIMEOUT_S: float = 120.0
"""Per-invocation wall-clock budget for a draw.io CLI call.

draw.io is Electron-based and can hang on headless cleanup; without a deadline
a single bad diagram would freeze the whole (lock-held) export.  On timeout
``subprocess.run`` kills and reaps the child, so no process leaks: folder-mode
falls back to per-file, and a per-file timeout warns and skips that diagram.
"""

# Sentinel for the which-cache: None means "not yet looked up"; False means
# "looked up and not found"; a non-empty str is the resolved path.
_DRAWIO_CLI: str | None | bool = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Auto-scale: render readability-limited (tiny-font) diagrams at higher scale
# ---------------------------------------------------------------------------

# Target on-screen font size (px) we want the smallest meaningful label to reach.
_TARGET_FONT_PX: float = 14.0
_MAX_SCALE: int = 3
# draw.io / Chromium silently emit blank or truncated PNGs past ~12k px on the
# long edge (~100 MP); never let scale push output beyond this.
_MAX_LONG_EDGE_PX: int = 12000
_DEFAULT_FONT_PX: float = 12.0  # draw.io's default when a cell omits fontSize


def _inflate_diagram(payload: str) -> str | None:
    """Decode a compressed ``<diagram>`` payload (base64 + raw-deflate + url-quote)."""
    try:
        raw = base64.b64decode(payload)
        inflated = zlib.decompress(raw, -zlib.MAX_WBITS)
        return urllib.parse.unquote(inflated.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _decode_drawio(data: bytes) -> str:
    """Return analyzable mxGraph XML text for a .drawio blob.

    Handles all three on-disk shapes: a bare ``<mxGraphModel>``, an ``<mxfile>``
    with inline (uncompressed) diagrams, and an ``<mxfile>`` whose ``<diagram>``
    payloads are deflate+base64 compressed.  The raw text is always included
    (covering the first two), with any decompressed payloads appended — so a
    regex scan for fontSize/geometry sees the content regardless of shape.
    """
    text = data.decode("utf-8", errors="replace")
    parts = [text]
    for m in re.finditer(r"<diagram\b[^>]*>([^<]+)</diagram>", text):
        payload = m.group(1).strip()
        if payload and "<" not in payload:  # base64-ish, not inline XML
            decoded = _inflate_diagram(payload)
            if decoded:
                parts.append(decoded)
    return "\n".join(parts)


def _smallest_font_size(model_xml: str) -> float:
    """Smallest meaningful ``fontSize=`` in the model, or the draw.io default."""
    sizes = [
        float(s)
        for s in re.findall(r"fontSize=([0-9]+(?:\.[0-9]+)?)", model_xml)
    ]
    sizes = [s for s in sizes if s >= 2.0]  # ignore degenerate values
    return min(sizes) if sizes else _DEFAULT_FONT_PX


def _long_edge_px(model_xml: str) -> float:
    """Long edge (px) of the content bounding box at scale 1, or 0 if unknown."""
    xs: list[float] = []
    ys: list[float] = []
    for tag in re.findall(r"<mxGeometry\b[^>]*?/?>", model_xml):
        def _attr(name: str) -> float | None:
            mm = re.search(rf'\b{name}="(-?[0-9.]+)"', tag)
            return float(mm.group(1)) if mm else None

        w = _attr("width")
        h = _attr("height")
        if w is None or h is None:
            continue
        x = _attr("x") or 0.0
        y = _attr("y") or 0.0
        xs.extend((x, x + w))
        ys.extend((y, y + h))
    if not xs or not ys:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _compute_render_scale(data: bytes) -> int:
    """Pick an export scale for a .drawio blob, driven by font readability.

    ``scale = clamp(round(14 / smallest_font_px), 1, 3)`` — tiny-font diagrams
    render at 2-3x so their labels stay legible; normal-font diagrams stay 1x.
    A hard cap ``scale <= floor(12000 / long_edge_px)`` keeps the output under
    draw.io/Chromium's ~100 MP blank-PNG threshold.  Falls back to 1 on any
    parse failure (never raises).
    """
    try:
        model = _decode_drawio(data)
    except Exception:
        return 1
    font = _smallest_font_size(model)
    scale = max(1, min(_MAX_SCALE, round(_TARGET_FONT_PX / font))) if font > 0 else 1
    long_edge = _long_edge_px(model)
    if long_edge > 0:
        # Hard cap so scale * long_edge stays under the blank-PNG threshold.
        # When the diagram is already past it at 1x, the floor is 0 and the
        # final max(1, ...) keeps it at 1x (we can't shrink below 1x via scale).
        scale = min(scale, int(_MAX_LONG_EDGE_PX // long_edge))
    return max(1, scale)


# ---------------------------------------------------------------------------
# CLI discovery (cached)
# ---------------------------------------------------------------------------


def _get_drawio_cli() -> str | None:
    """Return the draw.io CLI path, caching the result after the first probe.

    ``shutil.which`` is called at most once per process.  Subsequent calls
    read the module-level cache without re-probing.
    """
    global _DRAWIO_CLI
    if _DRAWIO_CLI is None:
        found = shutil.which("drawio") or shutil.which("draw.io")
        _DRAWIO_CLI = found if found else False
    return _DRAWIO_CLI if _DRAWIO_CLI else None  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Pair detection
# ---------------------------------------------------------------------------


class DrawioPair:
    """A matched (.drawio/.xml source, optional .png preview) attachment pair.

    ``png`` is ``None`` when no PNG sibling was found.
    ``preview_fresh`` is ``True`` when ``png.version.created_at >=
    xml.version.created_at`` (i.e. the PNG preview was uploaded after the
    most recent XML edit).  When ``png`` is ``None`` the preview is never
    considered fresh.

    Freshness is compared as plain string comparison on ISO 8601 timestamps
    returned by the API.  The API always returns UTC timestamps in the same
    format, so lexicographic ordering is correct.
    """

    __slots__ = ("xml", "png", "preview_fresh")

    def __init__(
        self,
        xml: Attachment,
        png: Attachment | None,
    ) -> None:
        self.xml = xml
        self.png = png
        if png is not None:
            self.preview_fresh = (
                png.version.created_at >= xml.version.created_at
            )
        else:
            self.preview_fresh = False


def find_drawio_pairs(attachments: list[Attachment]) -> list[DrawioPair]:
    """Pair each .drawio/.xml source attachment with its .png sibling (if any).

    The PNG sibling for a source named ``diagram.drawio`` is the attachment
    titled ``diagram.drawio.png`` (exact case-insensitive suffix match).
    Preview freshness: ``png.version.created_at >= xml.version.created_at``
    (ISO 8601 lexicographic comparison — NOT version numbers, which are not
    comparable across attachments).

    Returns one :class:`DrawioPair` per source attachment.  Attachments that
    are themselves PNG previews (not sources) are not included as separate
    pairs.
    """
    sources: list[Attachment] = []
    png_by_title: dict[str, Attachment] = {}

    for att in attachments:
        title_lower = att.title.casefold()
        media_lower = att.media_type.casefold()
        # v1 semantics: source when title ends in .drawio, OR media_type is
        # application/x-drawio, OR 'drawio' appears in media_type.
        # The media-type checks are independent — no title extension required.
        is_source = (
            title_lower.endswith(".drawio")
            or (media_lower == "application/x-drawio")
            or ("drawio" in media_lower)
        )
        if is_source:
            sources.append(att)
        elif att.media_type.casefold() == "image/png":
            # Register all PNG attachments by their full casefolded title so
            # both '<name>.drawio.png' and '<name>.xml.png' previews can be
            # found regardless of the source naming convention.
            png_by_title[title_lower] = att

    pairs: list[DrawioPair] = []
    for xml_att in sources:
        expected_png_key = xml_att.title.casefold() + ".png"
        png_att = png_by_title.get(expected_png_key)
        pairs.append(DrawioPair(xml=xml_att, png=png_att))

    return pairs


# ---------------------------------------------------------------------------
# Batch render
# ---------------------------------------------------------------------------


def render_batch(
    xml_blobs: dict[str, str],
    blobs: BlobStore,
) -> dict[str, str]:
    """Render a batch of draw.io XML blobs to PNG and store them in *blobs*.

    Args:
        xml_blobs: Mapping of diagram name (attachment title) to the blob
            digest of the corresponding XML content.
        blobs: The :class:`~conex.store.blobs.BlobStore` to read XML from
            and write PNGs into.

    Returns:
        A mapping of diagram name to the blob digest of the rendered PNG.
        Names for which rendering failed are omitted from the result.

    Invariants:
    - All temporary files live under ``.conex/tmp/`` (I4).
    - If the draw.io CLI is absent, returns ``{}`` and emits exactly one
      ``warnings.warn`` call (no re-probe on subsequent calls in the same
      process, because the CLI path is cached at module level).
    - The REAL CLI invocation is:
          drawio --export --format png --no-sandbox --output <out> <in>
      Never hand-rolls a renderer.
    - Folder-mode is attempted first.  If it fails (non-zero return code)
      or produces no PNG files, a per-file loop is used instead (same flags).
    - Partial failures in per-file mode produce a partial result + warnings.
    """
    if not xml_blobs:
        return {}

    cli = _get_drawio_cli()
    if cli is None:
        warnings.warn(
            "draw.io CLI not found; skipping PNG render. "
            "Install the draw.io desktop app for automatic diagram rendering.",
            stacklevel=2,
        )
        return {}

    # Determine the tmp directory from the BlobStore's internal layout.
    # BlobStore exposes _tmp_dir (private, but co-owned by this package).
    tmp_dir: Path = blobs._tmp_dir  # noqa: SLF001
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Stage all XML sources into tmp using index-prefixed filenames to avoid
    # collisions between titles that differ only in path separators (e.g.
    # 'a/b.drawio' and 'a_b.drawio' would otherwise map to the same stem).
    staged: dict[str, Path] = {}  # name -> xml tmp path
    scales: dict[str, int] = {}
    for idx, (name, digest) in enumerate(xml_blobs.items()):
        xml_data = blobs.read_bytes(digest)
        scales[name] = _compute_render_scale(xml_data)
        safe_name = name.replace(os.sep, "_").replace("/", "_")
        xml_path = tmp_dir / f"drawio-src-{idx:04d}-{safe_name}"
        xml_path.write_bytes(xml_data)
        staged[name] = xml_path

    result: dict[str, str] = {}

    # -- Attempt folder-mode invocation first --------------------------------
    # Folder-mode renders the whole batch in one CLI call at a single scale, so
    # it only applies when every diagram renders at 1x (the common case).  Any
    # diagram needing a higher scale (tiny fonts) renders per-file below, where
    # each gets its own --scale.
    if all(s == 1 for s in scales.values()):
        folder_out = tmp_dir / "drawio-folder-out"
        folder_out.mkdir(exist_ok=True)

        folder_argv = [
            cli,
            "--export",
            "--format", "png",
            "--no-sandbox",
            "--output", str(folder_out),
            str(tmp_dir),
        ]

        folder_mode_ok = False
        try:
            proc = subprocess.run(
                folder_argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_RENDER_TIMEOUT_S,
            )
            if proc.returncode == 0:
                # Attempt to map folder outputs back to staged input names.
                # folder_mode_ok is set only when we successfully mapped at least
                # one output — spec says "fails OR produces nothing -> fall back".
                # If drawio names its outputs differently (e.g. stem-replacement
                # instead of appending .png), produced is non-empty but result
                # stays empty, so we fall through to the per-file loop which uses
                # explicit --output paths and is name-robust.
                folder_result: dict[str, str] = {}
                for xml_path in staged.values():
                    expected_png = folder_out / (xml_path.name + ".png")
                    if expected_png.exists() and expected_png.stat().st_size > 0:
                        mapped_name = _name_for_staged_path(staged, xml_path)
                        if mapped_name is not None:
                            digest = blobs.add_bytes(expected_png.read_bytes())
                            folder_result[mapped_name] = digest
                if folder_result:
                    folder_mode_ok = True
                    result = folder_result
        except subprocess.TimeoutExpired:
            warnings.warn(
                f"draw.io folder-mode render timed out after "
                f"{_RENDER_TIMEOUT_S:.0f}s; falling back to per-file rendering",
                stacklevel=2,
            )
        except OSError:
            pass  # fall through to per-file

        if folder_mode_ok:
            return result

    # -- Per-file path (mixed/hi-res scales, and the folder-mode fallback) ----
    for name, xml_path in staged.items():
        scale = scales.get(name, 1)
        out_path = tmp_dir / f"drawio-out-{xml_path.name}.png"
        argv = [
            cli,
            "--export",
            "--format", "png",
            "--no-sandbox",
        ]
        if scale > 1:
            argv += ["--scale", str(scale)]
        argv += ["--output", str(out_path), str(xml_path)]
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_RENDER_TIMEOUT_S,
            )
            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                digest = blobs.add_bytes(out_path.read_bytes())
                result[name] = digest
            else:
                warnings.warn(
                    f"draw.io render failed for {name!r} "
                    f"(exit code {proc.returncode})",
                    stacklevel=2,
                )
        except subprocess.TimeoutExpired:
            warnings.warn(
                f"draw.io render timed out for {name!r} after "
                f"{_RENDER_TIMEOUT_S:.0f}s; skipping",
                stacklevel=2,
            )
        except OSError as exc:
            warnings.warn(
                f"draw.io render error for {name!r}: {exc}",
                stacklevel=2,
            )

    return result


def _name_for_staged_path(
    staged: dict[str, Path], target: Path
) -> str | None:
    """Reverse-lookup: return the diagram name for a staged xml path."""
    for name, path in staged.items():
        if path == target:
            return name
    return None
