"""Macro handlers for conex convert pipeline.

All Confluence structured-macro handlers live here; they are registered via
``@register`` from ``conex.convert.registry`` and called through the HANDLERS
dispatch table.

Semantics oracle: ``confluence_export/converter.py`` — each handler's behavior
matches the corresponding v1 function exactly unless a deliberate divergence is
documented.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup, Tag

from conex.convert.registry import (
    Macro,
    Replacement,
    dynamic_macro_placeholder,
    register,
)
from conex.paths import safe_attachment_name

if TYPE_CHECKING:
    from conex.convert import ConvertContext


# ---------------------------------------------------------------------------
# Emoticon map — PORT the full map from v1 confluence_export/converter.py
# ---------------------------------------------------------------------------

EMOTICON_MAP: dict[str, str] = {
    # Core Confluence emoticon names
    "tick": "✅",
    "cross": "❌",
    "warning": "⚠️",
    "information": "ℹ️",
    "plus": "➕",
    "minus": "➖",
    "question": "❓",
    "light-on": "\U0001f4a1",
    "light-off": "\U0001f4a1",
    "yellow-star": "⭐",
    "red-star": "⭐",
    "green-star": "⭐",
    "blue-star": "⭐",
    "heart": "❤️",
    "thumbs-up": "\U0001f44d",
    "thumbs-down": "\U0001f44e",
    "smile": "\U0001f642",
    "sad": "\U0001f641",
    "cheeky": "\U0001f61c",
    "laugh": "\U0001f604",
    "wink": "\U0001f609",
    # Atlassian shortnames (ac:emoji-shortname without colons)
    "check_mark": "✅",
    "cross_mark": "❌",
    "info": "ℹ️",
}


# ---------------------------------------------------------------------------
# Internal helpers shared by multiple handlers
# ---------------------------------------------------------------------------


def _markdown_label(text: str) -> str:
    """Strip characters that break markdown link labels/titles."""
    return re.sub(r"[\[\]()\r\n]", "", str(text))


def _media_url(name: str) -> str:
    return f".media/{urllib.parse.quote(name, safe='')}"


def _media_available(name: str, ctx: "ConvertContext") -> bool:
    """Return True when the file is confirmed present for this page this run."""
    if not ctx.media_enabled:
        return False
    return name in ctx.media_available


def _missing_attachment_node(soup_root: BeautifulSoup, label: str) -> Tag:
    span = soup_root.new_tag("span")
    span.string = f"Missing attachment: {_markdown_label(label)}"
    return span


def _get_soup_root(macro: Macro) -> BeautifulSoup:
    """Walk up from the macro element to find the BeautifulSoup root.

    Returns a fresh empty BeautifulSoup when the element is detached.
    """
    cur = macro.element
    while cur is not None and not isinstance(cur, BeautifulSoup):
        cur = cur.parent
    return cur if cur is not None else BeautifulSoup("", "html.parser")  # type: ignore[return-value]


def _own_title_and_body_children(
    macro: Macro, default_title: str
) -> tuple[str, list]:
    """Resolve a bodied macro's own title and DETACH its body content as live nodes.

    Title: from ``macro.params["title"]`` (extracted with recursive=False — never
    steals a nested macro's title, #45). Body: ``macro.rich_body`` children when
    present; otherwise the non-parameter direct children of the element.
    Children are detached BEFORE the title text is read.

    PORT: v1 ``_own_title_and_body_children`` semantics.
    """
    title_text = macro.params.get("title", "").strip()

    if macro.rich_body is not None:
        children = [el.extract() for el in list(macro.rich_body.children)]
    else:
        children = [
            el.extract()
            for el in list(macro.element.children)
            if not (isinstance(el, Tag) and el.name == "ac:parameter")
        ]

    return (title_text or default_title), children


# ---------------------------------------------------------------------------
# Handler: code
# ---------------------------------------------------------------------------


@register("code")
def handle_code(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert code macro to <pre><code> for markdownify.

    PORT: v1 ``_convert_code_block``.
    Language from ``language`` param; body from ``macro.plain_body``.
    """
    lang = macro.params.get("language", "").strip()
    code_text = macro.plain_body or ""

    soup_root = _get_soup_root(macro)
    pre = soup_root.new_tag("pre")
    if lang:
        code = soup_root.new_tag("code", attrs={"class": f"language-{lang}"})
    else:
        code = soup_root.new_tag("code")
    code.string = code_text
    pre.append(code)
    return pre


# ---------------------------------------------------------------------------
# Handlers: panel / info / note / warning / tip
# ---------------------------------------------------------------------------


def _handle_panel_like(macro: Macro, ctx: "ConvertContext", default_title: str) -> Replacement:
    """Shared body for panel/info/note/warning/tip.

    Renders as a blockquote: ``<p><strong>TITLE</strong></p>`` followed by the
    body children moved LIVE (not re-parsed) so nested macros survive.

    PORT: v1 ``_convert_panel``.
    """
    title, body_children = _own_title_and_body_children(macro, default_title)
    soup_root = _get_soup_root(macro)

    blockquote = soup_root.new_tag("blockquote")
    strong = soup_root.new_tag("strong")
    strong.string = title
    title_p = soup_root.new_tag("p")
    title_p.append(strong)
    blockquote.append(title_p)
    for element in body_children:
        blockquote.append(element)
    return blockquote


@register("panel")
def handle_panel(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert panel macro to blockquote. PORT: v1 ``_convert_panel``."""
    return _handle_panel_like(macro, ctx, "Panel")


@register("info")
def handle_info(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert info macro to blockquote. PORT: v1 ``_convert_panel``."""
    return _handle_panel_like(macro, ctx, "Info")


@register("note")
def handle_note(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert note macro to blockquote. PORT: v1 ``_convert_panel``."""
    return _handle_panel_like(macro, ctx, "Note")


@register("warning")
def handle_warning(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert warning macro to blockquote. PORT: v1 ``_convert_panel``."""
    return _handle_panel_like(macro, ctx, "Warning")


@register("tip")
def handle_tip(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert tip macro to blockquote. PORT: v1 ``_convert_panel``."""
    return _handle_panel_like(macro, ctx, "Tip")


# ---------------------------------------------------------------------------
# Handler: expand
# ---------------------------------------------------------------------------


@register("expand")
def handle_expand(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert expand macro to h4 heading + body children.

    PORT: v1 ``_convert_expand``.
    Body children are moved LIVE to preserve nested macros (re-parsing would
    detach them from the dispatch snapshot).  A temporary container collects
    the multiple output nodes; ``macro.element.replace_with(*children)`` splices
    them in place and we return None to signal the replacement is done.
    """
    title, body_children = _own_title_and_body_children(macro, "Details")
    soup_root = _get_soup_root(macro)

    h4 = soup_root.new_tag("h4")
    h4.string = title

    container = BeautifulSoup("", "html.parser")
    container.append(h4)
    for element in body_children:
        container.append(element)

    macro.element.replace_with(*list(container.children))
    return None  # replaced in place


# ---------------------------------------------------------------------------
# Handler: status
# ---------------------------------------------------------------------------


@register("status")
def handle_status(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert status macro to bold inline text.

    PORT: v1 ``_convert_status``.
    Returns None (remove) when the title param is empty.
    """
    title = macro.params.get("title", "").strip()
    if not title:
        macro.element.decompose()
        return None

    soup_root = _get_soup_root(macro)
    strong = soup_root.new_tag("strong")
    strong.string = title
    return strong


# ---------------------------------------------------------------------------
# Handler: jira
# ---------------------------------------------------------------------------


@register("jira")
def handle_jira(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert Jira issue macro to inline code with the issue key.

    PORT: v1 ``_convert_jira``.
    Returns None (remove) when the key param is absent.
    """
    key = macro.params.get("key", "").strip()
    if not key:
        macro.element.decompose()
        return None

    soup_root = _get_soup_root(macro)
    code = soup_root.new_tag("code")
    code.string = key
    return code


# ---------------------------------------------------------------------------
# Handler: toc
# ---------------------------------------------------------------------------


@register("toc")
def handle_toc(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Emit a visible ``[Confluence dynamic content: toc]`` placeholder (v1 parity)."""
    return dynamic_macro_placeholder(macro)


# ---------------------------------------------------------------------------
# Handlers: view-file / viewpdf / viewppt / viewxls
# ---------------------------------------------------------------------------


def _handle_view_file_like(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Shared resolution for view-file / viewpdf / viewppt / viewxls.

    Resolution order: ``ri:attachment ri:filename`` child (any depth) →
    ``name`` param.  Local filename via ``ctx.media``; if not available,
    emits a "Missing attachment" span.

    PORT: v1 ``_convert_view_file``.
    """
    ri = macro.element.find("ri:attachment")
    filename = ""
    if ri is not None:
        filename = str(ri.get("ri:filename", "") or "").strip()
    if not filename:
        filename = macro.params.get("name", "").strip()
    if not filename:
        macro.element.decompose()
        return None

    local_name = ctx.media.filename_for_title(filename) or safe_attachment_name(filename)
    soup_root = _get_soup_root(macro)

    if not _media_available(local_name, ctx):
        return _missing_attachment_node(soup_root, filename)

    a = soup_root.new_tag("a", href=_media_url(local_name))
    a.string = _markdown_label(filename)
    return a


@register("view-file")
def handle_view_file(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert view-file macro to an attachment link. PORT: v1 ``_convert_view_file``."""
    return _handle_view_file_like(macro, ctx)


@register("viewpdf")
def handle_viewpdf(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert viewpdf macro to an attachment link."""
    return _handle_view_file_like(macro, ctx)


@register("viewppt")
def handle_viewppt(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert viewppt macro to an attachment link."""
    return _handle_view_file_like(macro, ctx)


@register("viewxls")
def handle_viewxls(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert viewxls macro to an attachment link."""
    return _handle_view_file_like(macro, ctx)


# ---------------------------------------------------------------------------
# Drawio helpers
# ---------------------------------------------------------------------------


def _match_drawio_attachment(diagram_name: str, ctx: "ConvertContext"):
    """Return the drawio :class:`Attachment` that best matches ``diagram_name``.

    Tolerant of the ``.drawio`` extension and case/whitespace differences.
    PORT: v1 ``_match_drawio_attachment``.
    """
    from conex.models import Attachment as AttModel

    def _is_drawio(att: AttModel) -> bool:
        title = att.title.casefold()
        mtype = att.media_type.casefold()
        return (
            title.endswith(".drawio")
            or mtype == "application/x-drawio"
            or "drawio" in mtype
        )

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip()).casefold().removesuffix(".drawio")

    bare = diagram_name.removesuffix(".drawio")
    drawio_atts = [a for a in ctx.attachments if _is_drawio(a)]
    att_map = {a.title: a for a in drawio_atts}

    for key in (diagram_name, f"{bare}.drawio", bare):
        if key in att_map:
            return att_map[key]

    target = _norm(diagram_name)
    for att in drawio_atts:
        if _norm(att.title) == target:
            return att

    return None


def _match_drawio_attachment_local_name(
    diagram_name: str, ctx: "ConvertContext"
) -> str | None:
    """Return the local .media filename for the best-matching drawio attachment.

    PORT: v1 ``_attachment_local_name`` semantics.
    """
    matched = _match_drawio_attachment(diagram_name, ctx)
    if matched is None:
        return None
    return ctx.media.filename_for_title(matched.title)


def _emit_drawio(macro: Macro, ctx: "ConvertContext", diagram_name: str) -> Replacement:
    """Emit the rendered PNG <img> (+ source link) or a 'not rendered' note.

    PORT: v1 ``_emit_drawio``.

    Rules:
    - PNG available in ``ctx.rendered_drawio``: emit ``<img src=".media/FILE">``.
    - F5 (dead-source-link rule): append a source link only when the .drawio file
      is confirmed present in ``ctx.media_available``.
    - No PNG: emit italic ``[Draw.io diagram not rendered: NAME]``.
    """
    soup_root = _get_soup_root(macro)
    bare = diagram_name.removesuffix(".drawio")
    source_title = f"{bare}.drawio"

    matched = _match_drawio_attachment(diagram_name, ctx)
    if matched is not None:
        source_local_name = ctx.media.filename_for_title(matched.title)
    else:
        source_local_name = safe_attachment_name(source_title)

    png_name: str | None = None
    # build keys rendered_drawio by the .drawio attachment's ACTUAL title, so try
    # the matched attachment's title FIRST (handles a diagramName that differs in
    # case/whitespace from the title — v1 tried matched.title first), then the
    # diagram_name-derived keys.
    png_keys: list[str] = []
    if matched is not None:
        png_keys.append(matched.title)
    png_keys += [diagram_name, f"{bare}.drawio", bare]
    for key in png_keys:
        if key in ctx.rendered_drawio:
            png_name = ctx.rendered_drawio[key]
            break

    source_tracked = ctx.media_enabled and source_local_name in ctx.media_available

    p = soup_root.new_tag("p")
    if png_name is not None:
        p.append(soup_root.new_tag(
            "img",
            src=_media_url(png_name),
            alt=_markdown_label(bare),
        ))
    else:
        em = soup_root.new_tag("em")
        em.string = f"[Draw.io diagram not rendered: {_markdown_label(source_title)}]"
        p.append(em)

    if source_tracked:
        p.append(soup_root.new_tag("br"))
        src_em = soup_root.new_tag("em")
        src_em.append("Draw.io source: ")
        link = soup_root.new_tag("a", href=_media_url(source_local_name))
        link.string = _markdown_label(source_title)
        src_em.append(link)
        p.append(src_em)

    return p


# ---------------------------------------------------------------------------
# Handlers: drawio / inc-drawio / drawio-sketch
# ---------------------------------------------------------------------------


@register("drawio")
def handle_drawio(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert drawio macro to rendered PNG or placeholder.

    PORT: v1 ``_convert_drawio_placeholder`` + ``_emit_drawio``.
    ``diagramName`` param names the source diagram.
    """
    diagram_name = macro.params.get("diagramName", "").strip() or "diagram"
    return _emit_drawio(macro, ctx, diagram_name)


@register("inc-drawio")
def handle_inc_drawio(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert inc-drawio macro — same semantics as drawio. PORT: v1."""
    diagram_name = macro.params.get("diagramName", "").strip() or "diagram"
    return _emit_drawio(macro, ctx, diagram_name)


@register("drawio-sketch")
def handle_drawio_sketch(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert drawio-sketch macro.

    PORT: v1 ``_convert_drawio_sketch``.
    Attachment-backed (carries ``ri:attachment ri:filename``): render like drawio.
    Inline payload (no attachment): emit italic ``[Draw.io sketch]``.
    """
    ri = macro.element.find("ri:attachment")
    if ri is not None:
        filename = str(ri.get("ri:filename", "") or "").strip()
        if filename:
            return _emit_drawio(macro, ctx, filename)

    soup_root = _get_soup_root(macro)
    p = soup_root.new_tag("p")
    em = soup_root.new_tag("em")
    em.string = "[Draw.io sketch]"
    p.append(em)
    return p


# ---------------------------------------------------------------------------
# Handlers: profile / profile-picture
# ---------------------------------------------------------------------------


@register("profile")
def handle_profile(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert profile macro to a list item with the user's display name.

    PORT: v1 ``_convert_profile``.
    Resolves ``ri:user ri:account-id`` via ``ctx.resolve_user``.  When the
    resolver returns the account id unchanged (no display name), formats as
    ``user:<account_id>``.  An empty or missing user id produces "Unknown user".

    DELIBERATE DIVERGENCE from v1: v1 also appended the email address when
    present (``name (email)``), but v2 ``resolve_user`` only returns a display
    name string — there is no email channel.  Output is therefore name-only.
    """
    soup_root = _get_soup_root(macro)
    user_tag = macro.element.find("ri:user")
    account_id = str(user_tag.get("ri:account-id", "") or "") if user_tag else ""

    li = soup_root.new_tag("li")
    if account_id:
        display = ctx.resolve_user(account_id)
        li.string = display if (display and display != account_id) else f"user:{account_id}"
    else:
        li.string = "Unknown user"
    return li


@register("profile-picture")
def handle_profile_picture(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Handle residual profile-picture macros after the user-mention pre-pass.

    PORT: v1 profile-picture tail in ``_preprocess_html``.
    The pre-pass (``render._pass_user_mentions``) resolves macros with a live
    ``ri:user`` child to a ``<span>@NAME</span>`` in place.  This handler is
    called for any macro the pre-pass left intact (e.g. a macro whose only
    remaining child is the already-resolved span, or a macro with no user).

    If the macro now wraps a ``<span>`` (the resolved mention), unwrap it.
    Otherwise drop silently (no raw account-id must leak as body text).
    """
    if macro.element.find("span"):
        macro.element.unwrap()
        return None
    macro.element.decompose()
    return None


# ---------------------------------------------------------------------------
# Handler: anchor (drop)
# ---------------------------------------------------------------------------


@register("anchor")
def handle_anchor(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Drop anchor macros — Confluence-internal navigation, no MD equivalent."""
    macro.element.decompose()
    return None


# ---------------------------------------------------------------------------
# Handlers: excerpt / section / column (unwrap body)
# ---------------------------------------------------------------------------


@register("excerpt")
def handle_excerpt(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Unwrap excerpt — keep body content, drop wrapper. PORT: v1."""
    if macro.rich_body is not None:
        macro.rich_body.unwrap()
    macro.element.unwrap()
    return None


@register("section")
def handle_section(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Unwrap section — keep body content, drop wrapper. PORT: v1."""
    if macro.rich_body is not None:
        macro.rich_body.unwrap()
    macro.element.unwrap()
    return None


@register("column")
def handle_column(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Unwrap column — keep body content, drop wrapper. PORT: v1."""
    if macro.rich_body is not None:
        macro.rich_body.unwrap()
    macro.element.unwrap()
    return None


# ---------------------------------------------------------------------------
# Handlers: children / pagetree (comment placeholders)
# ---------------------------------------------------------------------------


@register("children")
def handle_children(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Emit a visible ``[Confluence dynamic content: children]`` placeholder."""
    return dynamic_macro_placeholder(macro)


@register("pagetree")
def handle_pagetree(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Emit a visible ``[Confluence dynamic content: pagetree]`` placeholder."""
    return dynamic_macro_placeholder(macro)


# ---------------------------------------------------------------------------
# Handler: attachments
# ---------------------------------------------------------------------------


@register("attachments")
def handle_attachments(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Render the attachments macro as an unordered list of media links.

    Each attachment in ``ctx.attachments`` becomes a ``<li>``:
    - Available (in ``ctx.media_available``): an ``<a>`` link into ``.media/``.
    - Not available: a "Missing attachment: …" span.
    An empty attachment list produces nothing (returns None).
    """
    soup_root = _get_soup_root(macro)
    ul = soup_root.new_tag("ul")
    for att in ctx.attachments:
        local_name = ctx.media.filename_for_title(att.title) or safe_attachment_name(att.title)
        li = soup_root.new_tag("li")
        if _media_available(local_name, ctx):
            a = soup_root.new_tag("a", href=_media_url(local_name))
            a.string = _markdown_label(att.title)
            li.append(a)
        else:
            li.string = f"Missing attachment: {_markdown_label(att.title)}"
        ul.append(li)
    if not ul.find("li"):
        macro.element.decompose()
        return None
    return ul


# ---------------------------------------------------------------------------
# Handlers: multimedia / widget (link / URL)
# ---------------------------------------------------------------------------


@register("multimedia")
def handle_multimedia(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert multimedia macro to an attachment link when available.

    Uses the same resolution as view-file (ri:attachment or name param).  A
    url-only embed (e.g. a YouTube/Vimeo link with no attachment) has no
    filename to resolve — emit the visible dynamic-content placeholder (v1
    parity) instead of silently dropping it.
    """
    has_attachment = macro.element.find("ri:attachment") is not None
    has_name = bool(macro.params.get("name", "").strip())
    if has_attachment or has_name:
        return _handle_view_file_like(macro, ctx)
    return dynamic_macro_placeholder(macro)


@register("widget")
def handle_widget(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Convert widget macro to a URL link when a ``url`` param is present.

    Falls back to a comment placeholder when no URL is available.
    """
    url = macro.params.get("url", "").strip()
    if not url:
        return dynamic_macro_placeholder(macro)

    soup_root = _get_soup_root(macro)
    a = soup_root.new_tag("a", href=url)
    a.string = _markdown_label(url)
    return a


# ---------------------------------------------------------------------------
# Registrar hook (called once by render.py on import)
# ---------------------------------------------------------------------------


def register_all() -> None:
    """No-op: all handlers self-register via @register at module-import time."""
