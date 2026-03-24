"""HTML storage format to markdown conversion with YAML frontmatter."""

from __future__ import annotations

import re

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as md

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
        elif macro_name == "excerpt":
            # Keep excerpt body content
            body = macro.find("ac:rich-text-body")
            if body:
                body.unwrap()
            macro.unwrap()
        elif macro_name in ("section", "column"):
            body = macro.find("ac:rich-text-body")
            if body:
                body.unwrap()
            macro.unwrap()
        elif macro_name in ("toc", "children", "pagetree", "attachments",
                            "create-from-template", "decisionreport"):
            # Navigation/interactive macros — no content to preserve
            macro.decompose()
        else:
            # Unknown macro: keep rich-text-body content, drop the wrapper
            body = macro.find("ac:rich-text-body")
            if body:
                macro.replace_with(*list(body.children))
            else:
                macro.decompose()

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


def _replace_ac_image(soup: BeautifulSoup, tag: Tag, attach_map: dict[str, Attachment]) -> None:
    """Replace <ac:image><ri:attachment ri:filename="..."/></ac:image> with <img>."""
    ri = tag.find("ri:attachment")
    if ri:
        filename = ri.get("ri:filename", "")
        if filename:
            img = soup.new_tag("img", src=f"media/{filename}", alt=filename)
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
            a = soup.new_tag("a", href=f"media/{filename}")
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
        a = soup.new_tag("a", href=f"media/{filename}")
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
