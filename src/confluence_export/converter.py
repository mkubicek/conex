"""HTML storage format to markdown conversion with YAML frontmatter."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as md

from confluence_export.media import MEDIA_DIR_NAME
from confluence_export.paths import AttachmentNamePlan, plan_attachment_names, safe_attachment_name

from typing import Callable

from confluence_export.types import Attachment, Page

# Optional callback: account_id -> {"displayName": ..., "email": ...} or None
UserResolver = Callable[[str], dict | None] | None


def _markdown_media_url(name: str) -> str:
    return f"{MEDIA_DIR_NAME}/{urllib.parse.quote(name, safe='')}"


def _markdown_label(text: str) -> str:
    return re.sub(r"[\[\]()\r\n]", "", str(text))


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
}


# Maximum length of a single path segment (directory or file stem). The layout
# planner reserves space within this cap when appending disambiguating suffixes,
# so a collision-suffixed name never exceeds it either.
MAX_FILENAME_LEN = 100


def sanitize_filename(title: str) -> str:
    """Convert a page title to a safe directory/file name.

    Performs raw normalization only (strip non-word chars, collapse separators,
    cap length). Per-parent collision disambiguation lives in layout.py.
    """
    name = re.sub(r"[^\w\s-]", "", title)
    name = re.sub(r"[-\s]+", "-", name)
    name = name.strip("-")
    if len(name) > MAX_FILENAME_LEN:
        name = name[:MAX_FILENAME_LEN].rstrip("-")
    return name or "untitled"


def convert_page(
    page: Page,
    base_url: str,
    space_key: str,
    path: str,
    attachments: list[Attachment] | None = None,
    user_resolver: UserResolver = None,
    rendered: dict[str, Path] | None = None,
    media_downloaded: bool = True,
    available_media: set[str] | None = None,
) -> str:
    """Convert a Confluence page to markdown with YAML frontmatter.

    ``rendered`` maps a draw.io diagram/attachment name to its rendered PNG path
    (built by the exporter BEFORE conversion). The drawio macro handler uses it to
    emit a real ``<img>`` inline, so no escapable ``[drawio:NAME]`` sentinel ever
    round-trips through markdownify (issues #9, #8)."""
    html = page.body_storage

    # Pre-process Confluence-specific HTML
    html = _preprocess_html(
        html, attachments or [], user_resolver=user_resolver, rendered=rendered or {},
        media_downloaded=media_downloaded, available_media=available_media,
    )

    # Convert to markdown using markdownify
    markdown = md(
        html,
        heading_style="ATX",
        bullets="*",
        strip=["script", "style"],
    )

    # Clean up excessive whitespace
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
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
    if page.status == "archived":
        meta["status"] = "archived"

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


def _is_detached(node: Tag, soup: BeautifulSoup) -> bool:
    """True if ``node`` is no longer reachable from ``soup`` — i.e. an earlier
    mutation extracted or decomposed it (or an ancestor). Walking up to the soup
    root is robust to both replace_with (subtree detached, parents intact) and
    decompose (parent set to None)."""
    cur = node
    while cur is not None:
        if cur is soup:
            return False
        cur = cur.parent
    return True


def _resolve_user_name(account_id: str, user_resolver: UserResolver) -> str:
    """Resolve a Confluence account id to a display name, falling back to the id
    when there is no resolver or no resolved name. Shared by the standalone
    user-mention pass and the profile-picture macro so they render identically."""
    if account_id and user_resolver:
        info = user_resolver(account_id)
        if info and info.get("displayName"):
            return info["displayName"]
    return account_id


def _preprocess_html(
    html: str,
    attachments: list[Attachment],
    user_resolver: UserResolver = None,
    rendered: dict[str, Path] | None = None,
    media_downloaded: bool = True,
    available_media: set[str] | None = None,
) -> str:
    """Pre-process Confluence storage HTML before markdownify."""
    soup = BeautifulSoup(html, "html.parser")
    rendered = rendered or {}

    # Build attachment lookup
    attach_map = {a.title: a for a in attachments}
    name_plan = plan_attachment_names(attachments)

    # --- ADF (Atlassian Document Format) elements ---
    # Remove adf-fallback (duplicate of adf-content) and adf-attribute (metadata
    # like panel-type="note" that would leak as plain text)
    for tag_name in ("ac:adf-fallback", "ac:adf-attribute", "ac:adf-mark"):
        for tag in list(soup.find_all(tag_name)):
            tag.decompose()
    # Decision lists (ADF) must be rendered BEFORE the generic adf-node unwrap
    # below, which would otherwise flatten them to plain text and drop both the
    # list structure and the decided/undecided state (issue #40). A decisionItem's
    # state lives in a ``state`` node attribute ("DECIDED"); its text is in child
    # nodes. Render each list as a bullet list; mark decided items with a ✓.
    for dlist in list(soup.find_all(
        lambda t: t.name == "ac:adf-node"
        and t.get("type") in ("decisionList", "decision-list")
    )):
        ul = soup.new_tag("ul")
        for item in dlist.find_all(
            lambda t: t.name == "ac:adf-node"
            and t.get("type") in ("decisionItem", "decision-item")
        ):
            text = item.get_text().strip()
            if not text:
                continue
            decided = (item.get("state", "") or "").strip().upper() == "DECIDED"
            li = soup.new_tag("li")
            li.string = ("✓ " if decided else "") + text
            ul.append(li)
        if ul.find("li"):
            dlist.replace_with(ul)
        else:
            dlist.decompose()
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

    # (Decision lists are handled above, before the adf-node unwrap.)

    # --- User mentions (ri:user inside ac:link or standalone) ---
    for user_tag in list(soup.find_all("ri:user")):
        # The snapshot can hold ri:user nodes an earlier iteration already
        # detached — e.g. a second ri:user inside a profile-picture we already
        # resolved (replace_with leaves the sibling in a detached subtree) or one
        # whose ancestor macro we decomposed (.get would raise on the dead node).
        # Operating on those would abort the whole export, so skip anything no
        # longer reachable from the live tree.
        if _is_detached(user_tag, soup):
            continue
        account_id = user_tag.get("ri:account-id", "")
        # A profile-picture macro is resolved to its inline @mention HERE, by
        # replacing the WHOLE macro — not deferred to the structured-macro pass.
        # Deferring breaks when the macro sits inside a panel/expand whose body is
        # re-parsed into a fresh soup before that pass runs: the macro would be
        # detached from the dispatch snapshot and the mention silently dropped
        # (issue #5, nested-macro regression).
        parent_macro = user_tag.find_parent("ac:structured-macro")
        if parent_macro is not None and parent_macro.get("ac:name") == "profile-picture":
            # Replace only the macro itself with its inline mention (NOT any
            # enclosing ac:link) so that multiple avatars sharing one link each
            # resolve. A link left holding only resolved mention spans is unwrapped
            # (not dropped) by the ac:link pass below, so the span still survives —
            # which is the silent-drop class #5 set out to fix.
            if account_id:
                span = soup.new_tag("span")
                span.string = f"@{_resolve_user_name(account_id, user_resolver)}"
                parent_macro.replace_with(span)
            else:
                parent_macro.decompose()
            continue
        if parent_macro is not None and parent_macro.get("ac:name") == "profile":
            # F3: leave the ri:user for _convert_profile to resolve. Consuming it
            # here starved the profile macro, leaving "Unknown user".
            continue
        name = _resolve_user_name(account_id, user_resolver)
        # F2: replace only this ri:user, never an enclosing ac:link — replacing the
        # link would drop a sibling ri:user not yet processed. The ac:link pass
        # below unwraps a link left holding only resolved mentions.
        user_tag.replace_with(f"@{name}")

    # --- ac:image ---
    for img_tag in list(soup.find_all("ac:image")):
        _replace_ac_image(soup, img_tag, name_plan, available_media)

    # --- ac:link (attachment and page links) ---
    for link_tag in list(soup.find_all("ac:link")):
        _replace_ac_link(soup, link_tag, name_plan, available_media)

    # --- Structured macros ---
    for macro in list(soup.find_all("ac:structured-macro")):
        # An earlier pass (e.g. a nested macro resolved in the mention pre-pass)
        # may have detached this node from the live tree; operating on it would
        # error or resurrect dropped content. Skip anything unreachable (F4).
        if _is_detached(macro, soup):
            continue
        macro_name = macro.get("ac:name", "")
        if macro_name in ("info", "tip", "note", "warning", "panel"):
            _convert_panel(soup, macro, macro_name)
        elif macro_name == "code":
            _convert_code_block(soup, macro)
        elif macro_name in ("drawio", "inc-drawio"):
            _convert_drawio_placeholder(
                soup, macro, rendered, attach_map, name_plan,
                media_downloaded=media_downloaded, available_media=available_media,
            )
        elif macro_name == "drawio-sketch":
            _convert_drawio_sketch(
                soup, macro, rendered, attach_map, name_plan,
                media_downloaded=media_downloaded, available_media=available_media,
            )
        elif macro_name == "profile":
            _convert_profile(soup, macro, user_resolver)
        elif macro_name == "profile-picture":
            # A profile-picture with a user was already resolved to an inline
            # mention <span> in the user-mention pre-pass (converter.py:312-314) —
            # including the nested case (F4), where the inner macro is replaced by a
            # span that ends up inside this outer one. Unwrap ONLY when that resolved
            # span is present; a macro whose only text is a raw ac:parameter value
            # (a bare account-id with no nested ri:user) carries no span and is
            # dropped, so the raw id is never leaked as body text. An empty one (no
            # user) is likewise dropped rather than emitting a placeholder.
            if macro.find("span"):
                macro.unwrap()
            else:
                macro.decompose()
        elif macro_name == "status":
            _convert_status(soup, macro)
        elif macro_name == "expand":
            _convert_expand(soup, macro)
        elif macro_name == "jira":
            _convert_jira(soup, macro)
        elif macro_name == "view-file":
            _convert_view_file(soup, macro, name_plan, available_media)
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

    return str(soup)


def _media_is_available(local_name: str, available_media: set[str] | None) -> bool:
    return available_media is None or local_name in available_media


def _missing_attachment_node(soup: BeautifulSoup, label: str) -> Tag:
    span = soup.new_tag("span")
    span.string = f"Missing attachment: {_markdown_label(label)}"
    return span


def _replace_ac_image(
    soup: BeautifulSoup,
    tag: Tag,
    name_plan: AttachmentNamePlan,
    available_media: set[str] | None,
) -> None:
    """Replace <ac:image><ri:attachment ri:filename="..."/></ac:image> with <img>."""
    ri = tag.find("ri:attachment")
    if ri:
        filename = ri.get("ri:filename", "")
        if filename:
            local_name = _attachment_local_name(ri, name_plan, filename)
            if not _media_is_available(local_name, available_media):
                tag.replace_with(_missing_attachment_node(soup, filename))
                return
            img = soup.new_tag(
                "img",
                src=_markdown_media_url(local_name),
                alt=_markdown_label(filename),
            )
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


def _replace_ac_link(
    soup: BeautifulSoup,
    tag: Tag,
    name_plan: AttachmentNamePlan,
    available_media: set[str] | None,
) -> None:
    """Replace <ac:link> attachment references with <a> tags."""
    ri = tag.find("ri:attachment")
    if ri:
        filename = ri.get("ri:filename", "")
        label_tag = tag.find("ac:plain-text-link-body") or tag.find("ac:link-body")
        label = label_tag.get_text().strip() if label_tag else filename
        if filename:
            local_name = _attachment_local_name(ri, name_plan, filename)
            if not _media_is_available(local_name, available_media):
                tag.replace_with(_missing_attachment_node(soup, label or filename))
                return
            a = soup.new_tag("a", href=_markdown_media_url(local_name))
            a.string = _markdown_label(label or filename)
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

    # A link with no recognized ri: child but still holding content is unwrapped
    # to preserve that content rather than dropping it: an inline mention span
    # resolved by the user pre-pass (supports >1 avatar per link), or a
    # structured-macro the ac:link pass must not destroy before the macro-dispatch
    # pass runs (F1). Only a genuinely empty link is removed. Using a content
    # check rather than a bare <span> sentinel avoids coupling to whatever tag an
    # earlier pass happened to emit (Q2).
    if tag.find("ac:structured-macro") or tag.get_text(strip=True):
        tag.unwrap()
        return

    tag.decompose()


def _convert_panel(soup: BeautifulSoup, macro: Tag, macro_name: str) -> None:
    """Convert info/tip/note/warning/panel to blockquotes."""
    title_param = macro.find("ac:parameter", attrs={"ac:name": "title"})
    title = title_param.get_text().strip() if title_param else macro_name.capitalize()

    body = macro.find("ac:rich-text-body")

    blockquote = soup.new_tag("blockquote")
    strong = soup.new_tag("strong")
    strong.string = title
    title_p = soup.new_tag("p")
    title_p.append(strong)
    blockquote.append(title_p)

    # Move the LIVE body children into the blockquote instead of re-parsing their
    # serialized HTML into a fresh soup. Re-parsing detached any macro nested in
    # the body from the structured-macro dispatch snapshot, so e.g. a drawio
    # diagram inside a panel was silently dropped (never converted). Moving the
    # live nodes keeps them attached and still in the snapshot, so the dispatch
    # converts them in place.
    if body:
        for element in list(body.children):
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

    # Heading + content. Move the LIVE body children (not a re-parsed copy) so a
    # macro nested in the expand body — e.g. a drawio diagram — stays attached and
    # in the structured-macro dispatch snapshot and is still converted (re-parsing
    # detached it, silently dropping it). The container is a throwaway holder; only
    # its children are spliced in where the macro was.
    container = BeautifulSoup("", "html.parser")
    h4 = soup.new_tag("h4")
    h4.string = title
    container.append(h4)

    if body:
        for element in list(body.children):
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

    if key:
        code = soup.new_tag("code")
        code.string = key
        macro.replace_with(code)
    else:
        macro.decompose()


def _convert_view_file(
    soup: BeautifulSoup,
    macro: Tag,
    name_plan: AttachmentNamePlan,
    available_media: set[str] | None,
) -> None:
    """Convert view-file macro to a link to the attachment."""
    name_param = macro.find("ac:parameter", attrs={"ac:name": "name"})
    ri = macro.find("ri:attachment")
    filename = ""
    if ri:
        filename = ri.get("ri:filename", "")
    if not filename and name_param:
        filename = name_param.get_text().strip()

    if filename:
        local_name = _attachment_local_name(ri, name_plan, filename)
        if not _media_is_available(local_name, available_media):
            macro.replace_with(_missing_attachment_node(soup, filename))
            return
        a = soup.new_tag("a", href=_markdown_media_url(local_name))
        a.string = _markdown_label(filename)
        macro.replace_with(a)
    else:
        macro.decompose()


def _match_drawio_attachment(
    diagram_name: str, attach_map: dict[str, Attachment]
) -> Attachment | None:
    """Find the drawio Attachment a macro's diagramName refers to.

    Tolerant of the ``.drawio`` extension and case/whitespace differences between
    the macro's diagramName and the on-disk attachment title (F6/F7)."""
    def _is_drawio(att: Attachment) -> bool:
        title = att.title.casefold()
        media_type = att.media_type.casefold()
        return (
            title.endswith(".drawio")
            or media_type == "application/x-drawio"
            or "drawio" in media_type
        )

    drawio_map = {
        title: att for title, att in attach_map.items()
        if _is_drawio(att)
    }
    bare = diagram_name.removesuffix(".drawio")
    for title in (diagram_name, f"{bare}.drawio", bare):
        if title in drawio_map:
            return drawio_map[title]

    def _norm(s: str) -> str:
        # Casefold BEFORE stripping the extension so an upper/mixed-case
        # ".DRAWIO" suffix is removed too (RF-D).
        return re.sub(r"\s+", " ", s.strip()).casefold().removesuffix(".drawio")

    target = _norm(diagram_name)
    for title, att in drawio_map.items():
        if _norm(title) == target:
            return att
    return None


def _attachment_local_name(
    ri: Tag | None,
    name_plan: AttachmentNamePlan,
    filename: str,
) -> str:
    attachment_id = ""
    if ri is not None:
        for attr in (
            "ri:content-id",
            "ri:contentId",
            "ri:attachment-id",
            "ri:attachmentId",
            "ri:id",
        ):
            attachment_id = str(ri.get(attr, "") or "")
            if attachment_id:
                break
    return name_plan.for_reference(filename, attachment_id or None)


def _convert_drawio_placeholder(
    soup: BeautifulSoup,
    macro: Tag,
    rendered: dict[str, Path],
    attach_map: dict[str, Attachment],
    name_plan: AttachmentNamePlan,
    *,
    media_downloaded: bool = True,
    available_media: set[str] | None = None,
) -> None:
    """Replace a drawio/inc-drawio macro with its rendered PNG (image + source
    link), or a graceful "not rendered" note when no PNG is available.

    Emitting real ``<img>``/``<a>`` here — rather than a ``[drawio:NAME]`` text
    sentinel resolved by a post-markdownify string replace — means markdownify
    can no longer escape the diagram name (``_`` -> ``\\_``) and break the lookup
    (#9), and a failed render can no longer leak a raw sentinel into the output
    (#8). The render is done by the exporter before conversion and handed in via
    ``rendered``."""
    name_param = macro.find("ac:parameter", attrs={"ac:name": "diagramName"})
    diagram_name = name_param.get_text().strip() if name_param else "diagram"
    # F6/F7: resolve the diagramName to the real on-disk attachment, tolerant of
    # the .drawio extension and case/whitespace, rather than reconstructing a name.
    matched = _match_drawio_attachment(diagram_name, attach_map)
    _emit_drawio(
        soup, macro, diagram_name, matched, rendered, name_plan,
        media_downloaded=media_downloaded, available_media=available_media,
    )


def _emit_drawio(
    soup: BeautifulSoup,
    macro: Tag,
    source_name: str,
    matched: Attachment | None,
    rendered: dict[str, Path],
    name_plan: AttachmentNamePlan,
    *,
    media_downloaded: bool,
    available_media: set[str] | None,
) -> None:
    """Emit the rendered-PNG ``<img>`` (+ a source link when the .drawio is on disk)
    for a drawio / drawio-sketch macro resolved to ``matched``, or a graceful
    "not rendered" note when no PNG is available."""
    bare = source_name.removesuffix(".drawio")
    source_title = matched.title if matched is not None else f"{bare}.drawio"
    source_local_name = (
        name_plan.for_attachment(matched)
        if matched is not None
        else safe_attachment_name(source_title)
    )

    # Rendered-PNG lookup: the exporter keys rendered[att.title]; try the matched
    # title first, then the reconstructed names (F7).
    png_path = None
    for key in ([matched.title] if matched is not None else []) + [
        source_name, f"{bare}.drawio", bare,
    ]:
        if key in rendered:
            png_path = rendered[key]
            break

    # F5: a source link only when the source is actually on disk.
    source_tracked = matched is not None and (
        source_local_name in available_media
        if available_media is not None
        else media_downloaded
    )

    p = soup.new_tag("p")
    if png_path is not None:
        p.append(soup.new_tag(
            "img",
            src=_markdown_media_url(png_path.name),
            alt=_markdown_label(bare),
        ))
    else:
        em = soup.new_tag("em")
        em.string = f"[Draw.io diagram not rendered: {_markdown_label(source_title)}]"
        p.append(em)
    if source_tracked:
        p.append(soup.new_tag("br"))
        src_em = soup.new_tag("em")
        src_em.append("Draw.io source: ")
        link = soup.new_tag("a", href=_markdown_media_url(source_local_name))
        link.string = _markdown_label(source_title)
        src_em.append(link)
        p.append(src_em)
    macro.replace_with(p)


def _convert_drawio_sketch(
    soup: BeautifulSoup,
    macro: Tag,
    rendered: dict[str, Path],
    attach_map: dict[str, Attachment],
    name_plan: AttachmentNamePlan,
    *,
    media_downloaded: bool = True,
    available_media: set[str] | None = None,
) -> None:
    """drawio-sketch macro (#6). Render it like a drawio diagram when it is
    attachment-backed (carries an ``ri:attachment``); otherwise emit a graceful
    ``[Draw.io sketch]`` note instead of the generic dynamic-content placeholder.

    The inline-payload render path (writing an embedded sketch to a temp ``.drawio``
    and rendering it) is intentionally deferred until a real drawio-sketch storage
    sample confirms the shape — until then an inline sketch degrades to a clean note
    rather than a confusing ``[Confluence dynamic content: drawio-sketch]``."""
    ri = macro.find("ri:attachment")
    filename = ri.get("ri:filename", "").strip() if ri is not None else ""
    if filename:
        matched = _match_drawio_attachment(filename, attach_map)
        _emit_drawio(
            soup, macro, filename, matched, rendered, name_plan,
            media_downloaded=media_downloaded, available_media=available_media,
        )
        return
    p = soup.new_tag("p")
    em = soup.new_tag("em")
    em.string = "[Draw.io sketch]"
    p.append(em)
    macro.replace_with(p)


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
