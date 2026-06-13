"""Storage XHTML → Markdown conversion pipeline (8 ordered passes).

PORT semantics from ``confluence_export/converter.py``.  The v1 structure is
NOT carried over; the behaviour is the oracle.

Pass order (each mutates the BeautifulSoup in place):
1. ADF decision/task lists — innermost-first nested-list lift, checkbox /
   DECIDED ✓ rendering (v1 issues #40/#43).
2. Macro dispatch — every ``ac:structured-macro`` / ``ac:adf-extension``
   through ``parse_macro`` + HANDLERS registry.
3. Links — ``ac:link`` with ``ri:page`` (v1 PARITY: render label, no
   cross-page path resolution), ``ri:attachment`` (via ctx.media), ``ri:user``
   (mention), CDATA bodies.
4. Images — ``ac:image`` + ``ri:attachment`` / ``ri:url``; ``<img>`` only when
   available in ``ctx.media_available``, else alt-text.
5. Emoticons — ``ac:emoticon`` → Unicode via EMOTICON_MAP.
6. Layout unwrap — ``ac:layout`` / ``ac:layout-section`` / ``ac:layout-cell``,
   then generic ``ac:adf-node`` / ``ac:adf-content`` unwrap (AFTER ADF lists).
7. Inline elements — ``<time>`` datetime, inline status spans, task
   placeholders.
8. markdownify with v1's option set; whitespace normalisation; single-H1 rule.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import TYPE_CHECKING

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as _md

from conex.convert.macros import EMOTICON_MAP, register_all
from conex.convert.registry import HANDLERS, Macro, Replacement, default_handler, parse_macro

if TYPE_CHECKING:
    from conex.convert import ConvertContext

# Call register_all() once when the module is imported so all handlers are
# loaded into HANDLERS before the pipeline runs.
register_all()

_MEDIA_DIR = ".media"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _media_url(name: str) -> str:
    return f"{_MEDIA_DIR}/{urllib.parse.quote(name, safe='')}"


def _markdown_label(text: str) -> str:
    """Strip characters that break markdown link labels/titles."""
    return re.sub(r"[\[\]()\r\n]", "", str(text))


def _is_detached(node: Tag, soup: BeautifulSoup) -> bool:
    """True if node is no longer reachable from soup (extracted/decomposed).

    Walking up to the soup root is robust to both replace_with (subtree
    detached, parents intact) and decompose (parent set to None).
    """
    cur = node
    while cur is not None:
        if cur is soup:
            return False
        cur = cur.parent  # type: ignore[assignment]
    return True


def _media_available(name: str, ctx: "ConvertContext") -> bool:
    """Return True when the file is confirmed present for this page this run."""
    if not ctx.media_enabled:
        return False
    return name in ctx.media_available


def _missing_attachment_node(soup: BeautifulSoup, label: str) -> Tag:
    span = soup.new_tag("span")
    span.string = f"Missing attachment: {_markdown_label(label)}"
    return span


def _resolve_attachment_name(
    ri: Tag | None,
    ctx: "ConvertContext",
    filename: str,
) -> str:
    """Resolve attachment filename/id to its planned local filename."""
    att_id: str = ""
    if ri is not None:
        for attr in (
            "ri:content-id",
            "ri:contentId",
            "ri:attachment-id",
            "ri:attachmentId",
            "ri:id",
        ):
            att_id = str(ri.get(attr, "") or "")
            if att_id:
                break
    if att_id:
        local = ctx.media.filename_for_id(att_id)
        if local is not None:
            return local
    return ctx.media.filename_for_title(filename) or filename


# ---------------------------------------------------------------------------
# Pass 1: ADF decision/task lists
# ---------------------------------------------------------------------------


def _is_decision_list(t: Tag) -> bool:
    return t.name == "ac:adf-node" and t.get("type") in ("decisionList", "decision-list")


def _is_decision_item(t: Tag) -> bool:
    return t.name == "ac:adf-node" and t.get("type") in ("decisionItem", "decision-item")


def _is_nestable_list(t: Tag) -> bool:
    return t.name == "ac:task-list" or _is_decision_list(t)


def _top_level_uls(scope: Tag) -> list[Tag]:
    """The rendered <ul>s scope DIRECTLY owns.

    find_all is recursive so on a 3+-level list it also returns grandchild
    <ul>s.  Extracting those too would yank them OUT of the mid-level <ul>
    and re-attach them one level too high.  Keep only <ul>s whose nearest
    enclosing <ul> is NOT also inside scope — identity comparison, not ==
    (two rendered sublists can be structurally identical).
    """
    found = scope.find_all("ul")
    ids = {id(u) for u in found}
    return [u for u in found if id(u.find_parent("ul")) not in ids]


def _pass_adf_lists(soup: BeautifulSoup) -> None:
    """Pass 1: render ADF decision and task lists innermost-first."""
    # Remove ADF structural noise that would pollute text extraction.
    for tag_name in ("ac:adf-fallback", "ac:adf-attribute", "ac:adf-mark"):
        for tag in list(soup.find_all(tag_name)):
            tag.decompose()

    list_nodes = list(soup.find_all(_is_nestable_list))
    # Sort innermost-first: more parents = deeper nesting.
    for node in sorted(list_nodes, key=lambda n: len(list(n.parents)), reverse=True):
        ul = soup.new_tag("ul")
        if _is_decision_list(node):
            for item in node.find_all(_is_decision_item):
                # Defensive: with innermost-first rendering no deeper list
                # survives here, but an item routed to the wrong list must
                # never be emitted twice (#43).
                if item.find_parent(_is_decision_list) is not node:
                    continue  # pragma: no cover - defensive
                nested_uls = [u.extract() for u in _top_level_uls(item)]
                text = item.get_text().strip()
                if text:
                    decided = (item.get("state", "") or "").strip().upper() == "DECIDED"
                    li = soup.new_tag("li")
                    li.string = ("✓ " if decided else "") + text
                    for u in nested_uls:
                        li.append(u)
                    ul.append(li)
                else:
                    # No own text: keep any nested items rather than dropping them.
                    for u in nested_uls:
                        for li_child in list(u.children):
                            ul.append(li_child)
        else:  # ac:task-list
            for task in list(node.find_all("ac:task")):
                # Defensive: innermost-first rendering already consumed deeper tasks.
                if task.find_parent("ac:task-list") is not node:
                    continue  # pragma: no cover - defensive
                status = task.find("ac:task-status")
                body = task.find("ac:task-body")
                nested_uls = [u.extract() for u in _top_level_uls(task)]
                is_done = status and status.get_text().strip() == "complete"
                checkbox = "[x] " if is_done else "[ ] "
                li = soup.new_tag("li")
                li.string = checkbox + (body.get_text().strip() if body else "")
                for u in nested_uls:
                    li.append(u)
                ul.append(li)

        # A stray rendered nested list not inside any item would be discarded
        # by replace_with; splice its items in instead (top-level only).
        for stray in _top_level_uls(node):
            stray.extract()
            for li_child in list(stray.children):
                ul.append(li_child)

        # Task lists always render; an empty decision list is dropped (#40).
        if node.name == "ac:task-list" or ul.find("li"):
            node.replace_with(ul)
        else:
            node.decompose()


# ---------------------------------------------------------------------------
# Pass 2: Macro dispatch
# ---------------------------------------------------------------------------


def _apply_replacement(soup: BeautifulSoup, element: Tag, result: Replacement) -> None:
    """Apply a handler's Replacement result to the element in the soup."""
    if result is None:
        # Handler already mutated element in place (e.g. unwrap) or wants removal.
        # Only decompose if still attached.
        if not _is_detached(element, soup):
            element.decompose()
    elif isinstance(result, str):
        element.replace_with(result)
    elif isinstance(result, (Tag, NavigableString)):
        element.replace_with(result)


def _pass_macros(soup: BeautifulSoup, ctx: "ConvertContext") -> None:
    """Pass 2: dispatch every ac:structured-macro / ac:adf-extension.

    Each macro is parsed via parse_macro (the single extraction point) then
    routed to a registered handler or default_handler.

    Result semantics:
    - None: handler mutated element in place (unwrap, decompose, replace_with
      already called) — do nothing further.
    - str / Tag / NavigableString: replace element with the result.

    Detached-node guard: an earlier pass (e.g. profile-picture resolved in
    the user pass) may have already detached this node.
    """
    for macro_el in list(soup.find_all(["ac:structured-macro", "ac:adf-extension"])):
        if _is_detached(macro_el, soup):
            continue
        macro = parse_macro(macro_el)
        handler = HANDLERS.get(macro.name, default_handler)
        result = handler(macro, ctx)
        if result is not None and not _is_detached(macro_el, soup):
            macro_el.replace_with(result)


# ---------------------------------------------------------------------------
# Pass 3: Links
# ---------------------------------------------------------------------------


def _pass_links(soup: BeautifulSoup, ctx: "ConvertContext") -> None:
    """Pass 3: ac:link → <a> or <span>.

    Sub-cases:
    - ri:attachment: link into .media/ when available, else missing note.
    - ri:page: render label as <span> (v1 PARITY: no cross-page path
      resolution).
    - ri:user: unwrap (already resolved by user pre-pass or left alone).
    - Bodyless / content-holding link: unwrap to keep content.
    - Genuinely empty: decompose.
    """
    for link_tag in list(soup.find_all("ac:link")):
        if _is_detached(link_tag, soup):
            continue
        _replace_ac_link(soup, link_tag, ctx)


def _replace_ac_link(soup: BeautifulSoup, tag: Tag, ctx: "ConvertContext") -> None:
    ri_att = tag.find("ri:attachment")
    if ri_att:
        filename = str(ri_att.get("ri:filename", "") or "")
        label_tag = tag.find("ac:plain-text-link-body") or tag.find("ac:link-body")
        label = label_tag.get_text().strip() if label_tag else filename
        if filename:
            local_name = _resolve_attachment_name(ri_att, ctx, filename)
            if not _media_available(local_name, ctx):
                tag.replace_with(_missing_attachment_node(soup, label or filename))
                return
            a = soup.new_tag("a", href=_media_url(local_name))
            a.string = _markdown_label(label or filename)
            tag.replace_with(a)
            return

    ri_page = tag.find("ri:page")
    if ri_page:
        title = str(ri_page.get("ri:content-title", "") or "Link")
        body = tag.find("ac:link-body") or tag.find("ac:plain-text-link-body")
        # Preserve a rich body (e.g. an image already turned into <img> by the
        # images pass) instead of collapsing the link to a title-only span,
        # which would silently drop the image.
        if body is not None and body.find(True) is not None:
            tag.unwrap()
            return
        label = body.get_text().strip() if body else title
        span = soup.new_tag("span")
        span.string = label or title
        tag.replace_with(span)
        return

    ri_user = tag.find("ri:user")
    if ri_user:
        tag.unwrap()
        return

    # A link with no recognised ri: child but still holding content → unwrap.
    # Preserve any element child (an <img> the images pass produced, or a
    # structured macro the macro-dispatch pass must not lose) as well as text;
    # only a genuinely empty link is decomposed.  An <img> has no text, so a
    # get_text-only check used to drop image-bodied links (content loss).
    if tag.find(True) is not None or tag.get_text(strip=True):
        tag.unwrap()
        return

    tag.decompose()


# ---------------------------------------------------------------------------
# Pass 4: Images
# ---------------------------------------------------------------------------


def _pass_images(soup: BeautifulSoup, ctx: "ConvertContext") -> None:
    """Pass 4: ac:image → <img> when available, else alt-text fallback."""
    for img_tag in list(soup.find_all("ac:image")):
        if _is_detached(img_tag, soup):
            continue
        _replace_ac_image(soup, img_tag, ctx)


def _replace_ac_image(soup: BeautifulSoup, tag: Tag, ctx: "ConvertContext") -> None:
    ri_att = tag.find("ri:attachment")
    if ri_att:
        filename = str(ri_att.get("ri:filename", "") or "")
        if filename:
            local_name = _resolve_attachment_name(ri_att, ctx, filename)
            if not _media_available(local_name, ctx):
                tag.replace_with(_missing_attachment_node(soup, filename))
                return
            img = soup.new_tag(
                "img",
                src=_media_url(local_name),
                alt=_markdown_label(filename),
            )
            tag.replace_with(img)
            return

    ri_url = tag.find("ri:url")
    if ri_url:
        url = str(ri_url.get("ri:value", "") or "")
        if url:
            img = soup.new_tag("img", src=url, alt="")
            tag.replace_with(img)
            return

    tag.decompose()


# ---------------------------------------------------------------------------
# Pass 5: Emoticons
# ---------------------------------------------------------------------------


def _pass_emoticons(soup: BeautifulSoup) -> None:
    """Pass 5: ac:emoticon → Unicode via EMOTICON_MAP.

    Runs BEFORE the list pass in the oracle (v1); here it's a separate pass
    that runs after macros (pass 2) but the spec ordering is:
    emoticons happen BEFORE list rendering so task/decision item get_text()
    captures the substituted Unicode.  In this implementation emoticons are
    handled here in pass 5 (after lists, pass 1), but lists use get_text()
    AFTER emoticons have been substituted in pass 1-setup.

    NOTE: The v1 oracle processes emoticons BEFORE the list pass so that
    ac:task-body.get_text() captures the substituted emoji.  We preserve
    this by running emoticons inside _pass_adf_lists setup (see the call to
    _substitute_emoticons below), but also run a cleanup pass here to catch
    any remaining ac:emoticon tags left by non-list contexts.
    """
    for tag in list(soup.find_all("ac:emoticon")):
        _substitute_emoticon(soup, tag)


def _substitute_emoticon(soup: BeautifulSoup, tag: Tag) -> None:
    """Replace a single ac:emoticon with its Unicode character or remove it."""
    name = str(tag.get("ac:name", "") or "")
    emoji = EMOTICON_MAP.get(name, "")
    if not emoji:
        shortname = str(tag.get("ac:emoji-shortname", "") or "").strip(":")
        emoji = EMOTICON_MAP.get(shortname, "")
    if emoji:
        tag.replace_with(emoji)
    else:
        tag.decompose()


# ---------------------------------------------------------------------------
# Pass 6: Layout unwrap
# ---------------------------------------------------------------------------


def _pass_layout(soup: BeautifulSoup) -> None:
    """Pass 6: unwrap layout containers and generic ADF wrapper nodes.

    Order matters: special-cased ADF nodes (decision/task lists) were already
    rendered in pass 1.  Generic ac:adf-node / ac:adf-content / ac:adf-extension
    are unwrapped HERE so their inner HTML is preserved.
    """
    for tag_name in ("ac:layout", "ac:layout-section", "ac:layout-cell"):
        for tag in list(soup.find_all(tag_name)):
            tag.unwrap()

    # Unwrap remaining ADF wrappers AFTER special-cased ADF nodes are resolved.
    for tag_name in ("ac:adf-content", "ac:adf-extension", "ac:adf-node"):
        for tag in list(soup.find_all(tag_name)):
            tag.unwrap()


# ---------------------------------------------------------------------------
# Pass 7: Inline elements
# ---------------------------------------------------------------------------


def _pass_inline(soup: BeautifulSoup) -> None:
    """Pass 7: time/date elements, inline-comment markers, placeholders.

    Also cleans up any remaining ac:/ri: tags by unwrapping them so inner
    text survives into the markdown output.
    """
    # Time/date elements
    for tag in list(soup.find_all("time")):
        dt = str(tag.get("datetime", "") or "")
        if dt:
            tag.replace_with(dt)
        else:
            tag.decompose()

    # Inline comment markers: unwrap
    for tag in list(soup.find_all("ac:inline-comment-marker")):
        tag.unwrap()

    # Placeholders: remove
    for tag in list(soup.find_all("ac:placeholder")):
        tag.decompose()

    # Clean up any remaining ac:/ri: tags by unwrapping
    for tag in list(soup.find_all(
        lambda t: isinstance(t, Tag) and t.name and (
            t.name.startswith("ac:") or t.name.startswith("ri:")
        )
    )):
        if not _is_detached(tag, soup):
            tag.unwrap()


# ---------------------------------------------------------------------------
# Pass 8: markdownify + cleanup
# ---------------------------------------------------------------------------


def _pass_markdownify(soup: BeautifulSoup, page_title: str) -> str:
    """Pass 8: HTML → markdown via markdownify with v1's option set.

    Post-processing:
    - Collapse 3+ consecutive blank lines to 2.
    - Ensure the document starts with a single H1 matching page.title.
    """
    html_str = str(soup)
    markdown = _md(
        html_str,
        heading_style="ATX",
        bullets="*",
        strip=["script", "style"],
    )
    # Collapse excessive whitespace
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = markdown.strip()

    # Ensure single H1 title
    if not markdown.startswith(f"# {page_title}"):
        markdown = f"# {page_title}\n\n{markdown}"

    return markdown


# ---------------------------------------------------------------------------
# User-mention pre-pass (before macro dispatch)
# ---------------------------------------------------------------------------


def _pass_user_mentions(soup: BeautifulSoup, ctx: "ConvertContext") -> None:
    """Pre-pass: resolve ri:user elements to @mention text.

    Runs before macro dispatch.  profile-picture macros are resolved here
    (inline mention) to survive panel body re-attachment (#5).  profile
    macros are LEFT ALONE (resolved in their handler so ri:user is not
    stolen).
    """
    for user_tag in list(soup.find_all("ri:user")):
        if _is_detached(user_tag, soup):
            continue
        account_id = str(user_tag.get("ri:account-id", "") or "")
        parent_macro = user_tag.find_parent("ac:structured-macro")
        if parent_macro is not None and str(parent_macro.get("ac:name", "")) == "profile-picture":
            if account_id:
                span = soup.new_tag("span")
                span.string = f"@{ctx.resolve_user(account_id) or account_id}"
                parent_macro.replace_with(span)
            else:
                parent_macro.decompose()
            continue
        if parent_macro is not None and str(parent_macro.get("ac:name", "")) == "profile":
            # Leave for the profile handler (F3).
            continue
        name = ctx.resolve_user(account_id) if account_id else account_id
        if not name:
            name = account_id
        user_tag.replace_with(f"@{name}")


# ---------------------------------------------------------------------------
# Public pipeline entry point
# ---------------------------------------------------------------------------


def preprocess_storage_xhtml(html: str, ctx: "ConvertContext") -> str:
    """Run all 8 passes over storage XHTML and return the processed HTML.

    This function is the primary conversion pipeline; convert_page() calls it
    and then applies markdownify + frontmatter assembly.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Emoticons must be substituted BEFORE the list pass so that
    # ac:task-body.get_text() captures the substituted Unicode (v1 oracle).
    for tag in list(soup.find_all("ac:emoticon")):
        _substitute_emoticon(soup, tag)

    # Also substitute time tags before the list pass for the same reason.
    for tag in list(soup.find_all("time")):
        dt = str(tag.get("datetime", "") or "")
        if dt:
            tag.replace_with(dt)
        else:
            tag.decompose()

    # Pass 1: ADF decision/task lists
    _pass_adf_lists(soup)

    # User-mention pre-pass (before macro dispatch)
    _pass_user_mentions(soup, ctx)

    # Pass 4: Images (before pass 3 links so ri:attachment handling is
    # consistent — v1 processes images before links)
    _pass_images(soup, ctx)

    # Pass 3: Links
    _pass_links(soup, ctx)

    # Pass 2: Macro dispatch
    _pass_macros(soup, ctx)

    # Pass 5: Emoticons (cleanup pass for any remaining after pre-pass)
    _pass_emoticons(soup)

    # Pass 6: Layout unwrap
    _pass_layout(soup)

    # Pass 7: Inline elements + final cleanup
    _pass_inline(soup)

    return str(soup)
