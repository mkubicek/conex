"""HTML storage format to markdown conversion with YAML frontmatter."""

from __future__ import annotations

import json
import re

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as md

from confluence_export.media import MEDIA_DIR_NAME

from typing import Callable

from confluence_export.types import Attachment, Page

# Optional callback: account_id -> {"displayName": ..., "email": ...} or None
UserResolver = Callable[[str], dict | None] | None

# Confluence emoticon name -> Unicode emoji
_EMOTICON_MAP = {
    "tick": "\u2705",
    "cross": "\u274c",
    "warning": "\u26a0\ufe0f",
    "information": "\u2139\ufe0f",
    "plus": "\u2795",
    "minus": "\u2796",
    "question": "\u2753",
    "light-on": "\U0001f4a1",
    "light-off": "\U0001f4a1",
    "yellow-star": "\u2b50",
    "red-star": "\u2b50",
    "green-star": "\u2b50",
    "blue-star": "\u2b50",
    "heart": "\u2764\ufe0f",
    "thumbs-up": "\U0001f44d",
    "thumbs-down": "\U0001f44e",
    "smile": "\U0001f642",
    "sad": "\U0001f641",
    "cheeky": "\U0001f61c",
    "laugh": "\U0001f604",
    "wink": "\U0001f609",
    # Atlassian shortnames (ac:emoji-shortname without colons)
    "check_mark": "\u2705",
    "cross_mark": "\u274c",
    "info": "\u2139\ufe0f",
    "warning": "\u26a0\ufe0f",
}

# Atlassian-shipped emoji codepoints used by `data-emoji-id="atlassian-..."`
# in rendered Confluence HTML. Mirrors CME's _ATLASSIAN_EMOTICONS map.
_ATLASSIAN_EMOJI_BY_ID = {
    "atlassian-check_mark": "\u2705",
    "atlassian-cross_mark": "\u274c",
    "atlassian-warning": "\u26a0\ufe0f",
    "atlassian-info": "\u2139\ufe0f",
    "atlassian-thumbsup": "\U0001f44d",
    "atlassian-thumbsdown": "\U0001f44e",
    "atlassian-smile": "\U0001f642",
    "atlassian-sad": "\U0001f641",
    "atlassian-tongue": "\U0001f61b",
    "atlassian-biggrin": "\U0001f601",
    "atlassian-wink": "\U0001f609",
}

# Known HTML element names — ports CME's _HTML_ELEMENTS list. Used by
# _escape_template_placeholders() to decide whether <foo> in markdown text is
# a real HTML tag (preserve) or a template placeholder like <TOPIC> (escape).
_HTML_ELEMENT_NAMES = frozenset(
    {
        "a", "abbr", "acronym", "address", "area", "article", "aside", "audio",
        "b", "base", "bdi", "bdo", "blockquote", "body", "br", "button",
        "canvas", "caption", "cite", "code", "col", "colgroup", "data",
        "datalist", "dd", "del", "details", "dfn", "dialog", "div", "dl", "dt",
        "em", "embed", "fieldset", "figcaption", "figure", "footer", "form",
        "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr",
        "html", "i", "iframe", "img", "input", "ins", "kbd", "keygen", "label",
        "legend", "li", "link", "main", "map", "mark", "menu", "menuitem",
        "meta", "meter", "nav", "noscript", "object", "ol", "optgroup",
        "option", "output", "p", "picture", "pre", "progress", "q", "rp", "rt",
        "ruby", "s", "samp", "script", "section", "select", "small", "source",
        "span", "strong", "style", "sub", "summary", "sup", "table", "tbody",
        "td", "template", "textarea", "tfoot", "th", "thead", "time", "title",
        "tr", "track", "u", "ul", "var", "video", "wbr",
    }
)

_ANGLE_BRACKET_RE = re.compile(r"<([^<>\n]*)>")
_CODE_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_FRAGMENT_LINK_RE = re.compile(r"(\]\(#)([^)\s]+)(\))")
_MAX_UNICODE_CODEPOINT = 0x10FFFF


def _normalize_unicode_whitespace(text: str) -> str:
    """Replace Unicode whitespace (nbsp, EM SPACE, THIN SPACE, ...) with regular space.

    Mirrors CME's Page.Converter._normalize_unicode_whitespace. Confluence
    storage HTML often contains \\xa0 inside inline formatting like
    <em>&nbsp;text</em>; markdownify's chomp() drops these characters
    entirely, producing "word*text*" instead of "word *text*".

    Newlines, tabs, and carriage returns are preserved (semantic).
    """
    if not text:
        return text
    chars = []
    for ch in text:
        if ch.isspace() and ch not in " \n\r\t":
            chars.append(" ")
        else:
            chars.append(ch)
    return "".join(chars)


def _escape_template_placeholders(text: str) -> str:
    r"""Escape ``<placeholder>`` patterns so Obsidian renders them as text.

    Mirrors CME's Page.Converter._escape_template_placeholders. Confluence
    templates often contain markers like ``<medical device>`` or ``<TOPIC>``
    that Obsidian otherwise eats as unknown HTML tags. Real HTML elements
    (``<br/>``, ``</div>``, ``<!-- ... -->``) and content inside fenced or
    inline code blocks are left alone.
    """

    def _escape_if_placeholder(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith("!"):
            return m.group(0)
        stripped = inner.strip().lstrip("/")
        tag_name = re.split(r"[\s/]", stripped)[0].lower() if stripped else ""
        if tag_name in _HTML_ELEMENT_NAMES:
            return m.group(0)
        return f"\\<{inner}\\>"

    lines = text.split("\n")
    result: list[str] = []
    in_fence = False
    for line in lines:
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            result.append(line)
            continue
        if in_fence:
            result.append(line)
            continue
        parts = _INLINE_CODE_RE.split(line)
        codes = _INLINE_CODE_RE.findall(line)
        processed: list[str] = []
        for i, part in enumerate(parts):
            processed.append(_ANGLE_BRACKET_RE.sub(_escape_if_placeholder, part))
            if i < len(codes):
                processed.append(codes[i])
        result.append("".join(processed))
    return "\n".join(result)


def _normalize_anchor_slug(slug: str) -> str:
    """Normalize a heading anchor slug to GitHub-style: lowercase, dashes only.

    Confluence emits anchor hrefs like ``#1.-Request-Service``. GitHub /
    Obsidian markdown renderers expect ``#1-request-service``. CME does this
    in convert_a; we do it as a post-pass on the rendered markdown.
    """
    s = slug.lower()
    s = re.sub(r"[^\w-]+", "", s)
    s = re.sub(r"_+", "_", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _pre_code_language(pre_el: Tag) -> str:
    """Extract the language from a ``<pre><code class="language-X">`` block.

    markdownify's ``convert_pre`` invokes the callback on the ``<pre>`` tag;
    we look at its first ``<code>`` child for ``language-*`` to render fenced
    blocks as ```` ```python ```` instead of bare ```` ``` ````.
    """
    code = pre_el.find("code") if isinstance(pre_el, Tag) else None
    if not isinstance(code, Tag):
        return ""
    classes = code.get("class") or []
    for cls in classes:
        if cls.startswith("language-"):
            return cls[len("language-") :]
    return ""


def _normalize_anchor_links(markdown: str) -> str:
    """Apply _normalize_anchor_slug to fragment-only links in markdown text.

    Skips fenced and inline code regions to avoid mangling literal examples.
    """
    lines = markdown.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = _INLINE_CODE_RE.split(line)
        codes = _INLINE_CODE_RE.findall(line)
        rebuilt: list[str] = []
        for i, part in enumerate(parts):
            rebuilt.append(
                _FRAGMENT_LINK_RE.sub(
                    lambda m: f"{m.group(1)}{_normalize_anchor_slug(m.group(2))}{m.group(3)}",
                    part,
                )
            )
            if i < len(codes):
                rebuilt.append(codes[i])
        out.append("".join(rebuilt))
    return "\n".join(out)


def sanitize_filename(title: str) -> str:
    """Convert a page title to a safe directory/file name."""
    name = re.sub(r"[^\w\s-]", "", title)
    name = re.sub(r"[-\s]+", "-", name)
    name = name.strip("-")
    if len(name) > 100:
        name = name[:100].rstrip("-")
    return name or "untitled"


def convert_page(
    page: Page,
    base_url: str,
    space_key: str,
    path: str,
    attachments: list[Attachment] | None = None,
    user_resolver: UserResolver = None,
) -> str:
    """Convert a Confluence page to markdown with YAML frontmatter."""
    html = page.body_storage

    # Pre-process Confluence-specific HTML
    html = _preprocess_html(html, attachments or [], user_resolver=user_resolver)

    # Convert to markdown using markdownify
    markdown = md(
        html,
        heading_style="ATX",
        bullets="*",
        strip=["script", "style"],
        code_language_callback=_pre_code_language,
    )

    # Clean up excessive whitespace
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = _normalize_anchor_links(markdown)
    markdown = _escape_template_placeholders(markdown)
    markdown = markdown.strip()

    # Build frontmatter
    frontmatter = _build_frontmatter(page, base_url, space_key, path, attachments)

    # Add title as H1 if not already present
    if not markdown.startswith(f"# {page.title}"):
        markdown = f"# {page.title}\n\n{markdown}"

    return frontmatter + markdown + "\n"


def _build_frontmatter(
    page: Page,
    base_url: str,
    space_key: str,
    path: str,
    attachments: list[Attachment] | None,
) -> str:
    """Generate YAML frontmatter block."""
    url = ""
    if base_url and page.webui:
        url = f"{base_url}/wiki{page.webui}"

    meta: dict = {
        "title": page.title,
        "page_id": page.id,
        "space_key": space_key,
        "path": path,
        "url": url,
        "last_modified": page.version.created_at,
        "version": page.version.number,
    }

    if attachments:
        meta["attachments"] = [
            {
                "name": a.title,
                "type": a.media_type,
                "size": a.file_size,
            }
            for a in attachments
        ]

    # Use yaml.dump for proper escaping, then wrap in ---
    yaml_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{yaml_str}---\n\n"


def _preprocess_html(
    html: str,
    attachments: list[Attachment],
    user_resolver: UserResolver = None,
) -> str:
    """Pre-process Confluence storage HTML before markdownify."""
    soup = BeautifulSoup(html, "html.parser")

    # Build attachment lookup
    attach_map = {a.title: a for a in attachments}

    # --- ADF (Atlassian Document Format) elements ---
    # Remove adf-fallback (duplicate of adf-content) and adf-attribute (metadata
    # like panel-type="note" that would leak as plain text)
    for tag_name in ("ac:adf-fallback", "ac:adf-attribute", "ac:adf-mark"):
        for tag in list(soup.find_all(tag_name)):
            tag.decompose()
    # Unwrap adf-content, adf-extension, adf-node so their inner HTML is preserved
    for tag_name in ("ac:adf-content", "ac:adf-extension", "ac:adf-node"):
        for tag in list(soup.find_all(tag_name)):
            tag.unwrap()

    # --- Layout tags ---
    for tag_name in ("ac:layout", "ac:layout-section", "ac:layout-cell"):
        for tag in list(soup.find_all(tag_name)):
            tag.unwrap()

    # --- Emoticons ---
    for tag in list(soup.find_all("ac:emoticon")):
        name = tag.get("ac:name", "")
        # Prefer our Unicode map; the ac:emoji-fallback is usually a shortcode
        # like ":check_mark:" which isn't useful as plain text
        emoji = _EMOTICON_MAP.get(name, "")
        if not emoji:
            # Check if emoji-shortname maps to something we know
            shortname = tag.get("ac:emoji-shortname", "").strip(":")
            emoji = _EMOTICON_MAP.get(shortname, "")
        if emoji:
            tag.replace_with(emoji)
        else:
            tag.decompose()

    # --- Time/date tags ---
    for tag in list(soup.find_all("time")):
        dt = tag.get("datetime", "")
        if dt:
            tag.replace_with(dt)
        else:
            tag.decompose()

    # --- Task lists ---
    for task_list in list(soup.find_all("ac:task-list")):
        ul = soup.new_tag("ul")
        for task in list(task_list.find_all("ac:task")):
            status = task.find("ac:task-status")
            body = task.find("ac:task-body")
            is_done = status and status.get_text().strip() == "complete"
            checkbox = "[x] " if is_done else "[ ] "
            li = soup.new_tag("li")
            li.string = checkbox + (body.get_text().strip() if body else "")
            ul.append(li)
        task_list.replace_with(ul)

    # --- Decision lists ---
    for tag in list(soup.find_all(lambda t: t.name == "ac:adf-node" and t.get("type") in ("decisionList", "decision-list"))):
        tag.unwrap()
    for tag in list(soup.find_all(lambda t: t.name == "ac:adf-node" and t.get("type") in ("decisionItem", "decision-item"))):
        state = tag.get("state", tag.get("localId", ""))
        # Just preserve the text content
        text = tag.get_text().strip()
        if text:
            p = soup.new_tag("p")
            p.string = text
            tag.replace_with(p)
        else:
            tag.decompose()

    # --- User mentions (ri:user inside ac:link or standalone) ---
    for user_tag in list(soup.find_all("ri:user")):
        account_id = user_tag.get("ri:account-id", "")
        parent_link = user_tag.find_parent("ac:link")
        display_name = None
        if account_id and user_resolver:
            info = user_resolver(account_id)
            if info:
                display_name = info.get("displayName")
        name = display_name or account_id
        if parent_link:
            span = soup.new_tag("span")
            span.string = f"@{name}"
            parent_link.replace_with(span)
        else:
            user_tag.replace_with(f"@{name}")

    # --- Rendered emoticon img tags (e.g. <img class="emoticon" data-emoji-id="..."/>) ---
    # These appear when a page is fetched as rendered HTML rather than storage
    # XML. Mirrors CME's _convert_emoticon resolution order.
    for img_tag in list(soup.find_all("img")):
        emoji = _resolve_emoticon_img(img_tag)
        if emoji is not None:
            img_tag.replace_with(emoji)

    # --- ac:image ---
    for img_tag in list(soup.find_all("ac:image")):
        _replace_ac_image(soup, img_tag, attach_map)

    # --- ac:link (attachment and page links) ---
    for link_tag in list(soup.find_all("ac:link")):
        _replace_ac_link(soup, link_tag, attach_map)

    # --- Structured macros ---
    for macro in list(soup.find_all("ac:structured-macro")):
        macro_name = macro.get("ac:name", "")
        if macro_name in ("info", "tip", "note", "warning", "panel"):
            _convert_panel(soup, macro, macro_name)
        elif macro_name == "code":
            _convert_code_block(soup, macro)
        elif macro_name in ("drawio", "inc-drawio"):
            _convert_drawio_placeholder(soup, macro)
        elif macro_name == "plantuml":
            _convert_plantuml(soup, macro)
        elif macro_name == "profile":
            _convert_profile(soup, macro, user_resolver)
        elif macro_name == "status":
            _convert_status(soup, macro)
        elif macro_name == "expand":
            _convert_expand(soup, macro)
        elif macro_name == "jira":
            _convert_jira(soup, macro)
        elif macro_name == "view-file":
            _convert_view_file(soup, macro)
        elif macro_name in ("excerpt", "section", "column"):
            # Pure layout/wrapper — keep body, drop wrapper, no placeholder
            body = macro.find("ac:rich-text-body")
            if body:
                body.unwrap()
            macro.unwrap()
        else:
            # Default: if body content exists, preserve it. Otherwise the
            # macro is dynamic/widget-like (toc, children, recently-updated,
            # include, future Confluence additions) — emit a visible
            # placeholder so the reader knows something was there instead
            # of silently dropping it.
            body = macro.find("ac:rich-text-body")
            if body and body.get_text(strip=True):
                macro.replace_with(*list(body.children))
            else:
                _convert_dynamic_macro_placeholder(soup, macro, macro_name)

    # --- Inline comment markers: just unwrap ---
    for tag in list(soup.find_all("ac:inline-comment-marker")):
        tag.unwrap()

    # --- ac:placeholder: remove ---
    for tag in list(soup.find_all("ac:placeholder")):
        tag.decompose()

    # --- Clean up any remaining ac:/ri: tags by unwrapping ---
    for tag in list(soup.find_all(lambda t: t.name and (t.name.startswith("ac:") or t.name.startswith("ri:")))):
        tag.unwrap()

    # Replace Unicode whitespace (nbsp, EM SPACE, ...) with regular spaces in
    # all text nodes outside <pre>. markdownify's chomp() drops these, which
    # collapses spacing around inline emphasis (CME parity fix).
    for s in list(soup.find_all(string=True)):
        if any(p.name == "pre" for p in s.parents if p is not None and p.name):
            continue
        normalized = _normalize_unicode_whitespace(str(s))
        if normalized != str(s):
            s.replace_with(NavigableString(normalized))

    return str(soup)


def _replace_ac_image(soup: BeautifulSoup, tag: Tag, attach_map: dict[str, Attachment]) -> None:
    """Replace <ac:image><ri:attachment ri:filename="..."/></ac:image> with <img>."""
    ri = tag.find("ri:attachment")
    if ri:
        filename = ri.get("ri:filename", "")
        if filename:
            img = soup.new_tag("img", src=f"{MEDIA_DIR_NAME}/{filename}", alt=filename)
            tag.replace_with(img)
            return
    # Fallback: external image URL
    ri_url = tag.find("ri:url")
    if ri_url:
        url = ri_url.get("ri:value", "")
        if url:
            img = soup.new_tag("img", src=url, alt="")
            tag.replace_with(img)
            return
    tag.decompose()


def _replace_ac_link(soup: BeautifulSoup, tag: Tag, attach_map: dict[str, Attachment]) -> None:
    """Replace <ac:link> attachment references with <a> tags."""
    ri = tag.find("ri:attachment")
    if ri:
        filename = ri.get("ri:filename", "")
        label_tag = tag.find("ac:plain-text-link-body") or tag.find("ac:link-body")
        label = label_tag.get_text().strip() if label_tag else filename
        if filename:
            a = soup.new_tag("a", href=f"{MEDIA_DIR_NAME}/{filename}")
            a.string = label or filename
            tag.replace_with(a)
            return

    # Page link — use content-title for display
    ri_page = tag.find("ri:page")
    if ri_page:
        title = ri_page.get("ri:content-title", "Link")
        body = tag.find("ac:link-body") or tag.find("ac:plain-text-link-body")
        label = body.get_text().strip() if body else title
        # Use title as readable text since we can't resolve cross-page links
        span = soup.new_tag("span")
        span.string = label or title
        tag.replace_with(span)
        return

    # User link (already handled above, but catch stragglers)
    ri_user = tag.find("ri:user")
    if ri_user:
        tag.unwrap()
        return

    tag.decompose()


def _convert_panel(soup: BeautifulSoup, macro: Tag, macro_name: str) -> None:
    """Convert info/tip/note/warning/panel to blockquotes."""
    title_param = macro.find("ac:parameter", attrs={"ac:name": "title"})
    title = title_param.get_text().strip() if title_param else macro_name.capitalize()

    body = macro.find("ac:rich-text-body")
    body_html = "".join(str(child) for child in body.children) if body else ""

    blockquote = soup.new_tag("blockquote")
    strong = soup.new_tag("strong")
    strong.string = title
    title_p = soup.new_tag("p")
    title_p.append(strong)
    blockquote.append(title_p)

    if body_html:
        body_soup = BeautifulSoup(body_html, "html.parser")
        for element in list(body_soup.children):
            blockquote.append(element)

    macro.replace_with(blockquote)


def _convert_profile(soup: BeautifulSoup, macro: Tag, user_resolver: UserResolver) -> None:
    """Convert profile macro to a user mention as a list item."""
    user_tag = macro.find("ri:user")
    account_id = user_tag.get("ri:account-id", "") if user_tag else ""

    user_info = None
    if account_id and user_resolver:
        user_info = user_resolver(account_id)

    li = soup.new_tag("li")
    if user_info:
        name = user_info.get("displayName", account_id)
        email = user_info.get("email")
        if email:
            li.string = f"{name} ({email})"
        else:
            li.string = name
    else:
        li.string = f"user:{account_id}" if account_id else "Unknown user"
    macro.replace_with(li)


def _convert_status(soup: BeautifulSoup, macro: Tag) -> None:
    """Convert status macro to bold inline text."""
    title_param = macro.find("ac:parameter", attrs={"ac:name": "title"})
    title = title_param.get_text().strip() if title_param else ""
    colour_param = macro.find("ac:parameter", attrs={"ac:name": "colour"})
    colour = colour_param.get_text().strip().upper() if colour_param else ""

    if title:
        # Render as: **STATUS_TEXT**
        strong = soup.new_tag("strong")
        strong.string = title
        macro.replace_with(strong)
    else:
        macro.decompose()


def _convert_expand(soup: BeautifulSoup, macro: Tag) -> None:
    """Convert expand macro to a details/summary block (rendered as heading + content)."""
    title_param = macro.find("ac:parameter", attrs={"ac:name": "title"})
    title = title_param.get_text().strip() if title_param else "Details"

    body = macro.find("ac:rich-text-body")
    body_html = "".join(str(child) for child in body.children) if body else ""

    # Use <details><summary> which markdownify will preserve as HTML
    # or just use a heading + content approach
    container = BeautifulSoup("", "html.parser")
    h4 = soup.new_tag("h4")
    h4.string = title
    container.append(h4)

    if body_html:
        body_soup = BeautifulSoup(body_html, "html.parser")
        for element in list(body_soup.children):
            container.append(element)

    macro.replace_with(*list(container.children))


def _convert_code_block(soup: BeautifulSoup, macro: Tag) -> None:
    """Convert code macro to <pre><code> for markdownify."""
    lang_param = macro.find("ac:parameter", attrs={"ac:name": "language"})
    lang = lang_param.get_text().strip() if lang_param else ""

    body = macro.find("ac:plain-text-body")
    code_text = ""
    if body:
        code_text = body.get_text()

    pre = soup.new_tag("pre")
    code = soup.new_tag("code", attrs={"class": f"language-{lang}"} if lang else {})
    code.string = code_text
    pre.append(code)
    macro.replace_with(pre)


def _convert_jira(soup: BeautifulSoup, macro: Tag) -> None:
    """Convert Jira issue macro to a link."""
    key_param = macro.find("ac:parameter", attrs={"ac:name": "key"})
    key = key_param.get_text().strip() if key_param else ""

    server_param = macro.find("ac:parameter", attrs={"ac:name": "server"})
    server_id_param = macro.find("ac:parameter", attrs={"ac:name": "serverId"})

    if key:
        code = soup.new_tag("code")
        code.string = key
        macro.replace_with(code)
    else:
        macro.decompose()


def _convert_view_file(soup: BeautifulSoup, macro: Tag) -> None:
    """Convert view-file macro to a link to the attachment."""
    name_param = macro.find("ac:parameter", attrs={"ac:name": "name"})
    ri = macro.find("ri:attachment")
    filename = ""
    if ri:
        filename = ri.get("ri:filename", "")
    if not filename and name_param:
        filename = name_param.get_text().strip()

    if filename:
        a = soup.new_tag("a", href=f"{MEDIA_DIR_NAME}/{filename}")
        a.string = filename
        macro.replace_with(a)
    else:
        macro.decompose()


def _convert_drawio_placeholder(soup: BeautifulSoup, macro: Tag) -> None:
    """Replace drawio/inc-drawio macro with a placeholder."""
    name_param = macro.find("ac:parameter", attrs={"ac:name": "diagramName"})
    diagram_name = name_param.get_text().strip() if name_param else "diagram"

    placeholder = soup.new_tag("p")
    placeholder.string = f"[drawio:{diagram_name}]"
    macro.replace_with(placeholder)


def _convert_plantuml(soup: BeautifulSoup, macro: Tag) -> None:
    """Convert a PlantUML structured macro to a fenced ```plantuml code block.

    Mirrors CME's Page.Converter.convert_plantuml. Confluence stores the UML
    text as JSON (``{"umlDefinition": "@startuml\\n..."}``) inside
    ``<ac:plain-text-body>``. CME reads this from a separate ``editor2`` XML
    field; conex receives storage HTML directly so we read the body in place.
    On missing/empty/invalid content we emit an HTML comment so the reader
    knows something was lost rather than dropping silently.
    """
    body = macro.find("ac:plain-text-body")
    if not body:
        macro.replace_with(NavigableString("\n<!-- PlantUML diagram (no content found) -->\n\n"))
        return
    cdata_content = body.get_text(strip=True)
    if not cdata_content:
        macro.replace_with(NavigableString("\n<!-- PlantUML diagram (empty content) -->\n\n"))
        return
    try:
        data = json.loads(cdata_content)
    except json.JSONDecodeError:
        macro.replace_with(NavigableString("\n<!-- PlantUML diagram (invalid JSON) -->\n\n"))
        return
    uml = data.get("umlDefinition", "") if isinstance(data, dict) else ""
    if not uml:
        macro.replace_with(NavigableString("\n<!-- PlantUML diagram (no UML definition) -->\n\n"))
        return
    pre = soup.new_tag("pre")
    code = soup.new_tag("code", attrs={"class": "language-plantuml"})
    code.string = uml
    pre.append(code)
    macro.replace_with(pre)


def _resolve_emoticon_img(img: Tag) -> str | None:
    """Resolve a rendered ``<img class="emoticon" ...>`` tag to a Unicode emoji.

    Mirrors CME's Page.Converter._convert_emoticon. Resolution order:

    1. ``data-emoji-fallback`` if it is direct Unicode (not a ``:shortname:``).
    2. ``data-emoji-id`` parsed as ``-`` separated hex codepoints (e.g.
       ``1f6e0`` or ``1f1e6-1f1e8`` for flags).
    3. ``data-emoji-id`` looked up in the Atlassian-shipped emoji map (e.g.
       ``atlassian-check_mark``).
    4. ``data-emoji-shortname`` (returned as ``:short:``).
    5. ``data-emoji-fallback`` even if it's a shortname.
    6. None — the caller leaves the original ``<img>`` alone.
    """
    classes = img.get("class") or []
    if "emoticon" not in classes:
        return None
    fallback = str(img.get("data-emoji-fallback", ""))
    if fallback and not fallback.startswith(":"):
        return fallback
    emoji_id = str(img.get("data-emoji-id", ""))
    if emoji_id:
        try:
            codepoints = [int(cp, 16) for cp in emoji_id.split("-")]
            if codepoints and all(0 <= cp <= _MAX_UNICODE_CODEPOINT for cp in codepoints):
                return "".join(chr(cp) for cp in codepoints)
        except (OverflowError, ValueError):
            pass
        if emoji_id in _ATLASSIAN_EMOJI_BY_ID:
            return _ATLASSIAN_EMOJI_BY_ID[emoji_id]
    shortname = str(img.get("data-emoji-shortname", ""))
    if shortname:
        return shortname
    if fallback:
        return fallback
    # Not a resolvable emoticon — leave the img tag alone (e.g. it was just
    # tagged "emoticon" but has a real src to a custom emoji image).
    return None


def _convert_dynamic_macro_placeholder(
    soup: BeautifulSoup, macro: Tag, macro_name: str
) -> None:
    """Emit a visible italic placeholder for content-less macros.

    Captures the macro name plus any non-default parameters (resolving page
    references) so the reader sees what was there without the export trying
    to keep dynamic content fresh. Future-proof: any new Confluence macro
    without a body lands here automatically.
    """
    params: list[str] = []
    for p in macro.find_all("ac:parameter", recursive=False):
        name = p.get("ac:name", "")
        page_ref = p.find("ri:page")
        if page_ref is not None:
            value = page_ref.get("ri:content-title", "").strip()
        else:
            value = p.get_text().strip()
        if name and value:
            params.append(f"{name}={value}")

    suffix = f" ({', '.join(params)})" if params else ""
    em = soup.new_tag("em")
    em.string = f"[Confluence dynamic content: {macro_name or 'unnamed'}{suffix}]"
    p = soup.new_tag("p")
    p.append(em)
    macro.replace_with(p)
