"""Macro registry: parse_macro and the HANDLERS dispatch table.

The registry is the v1 #45-class killer: parse_macro is THE ONLY place that
extracts params and bodies from an ac:structured-macro element.  It uses
recursive=False everywhere so a nested macro's params/body can never be
stolen by an outer macro.

Handler protocol:
  Handler(macro: Macro, ctx: ConvertContext) -> Replacement
  Replacement = Tag | NavigableString | str | None (None = remove element)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from bs4 import BeautifulSoup, NavigableString, Tag

if TYPE_CHECKING:
    from conex.convert import ConvertContext


# ---------------------------------------------------------------------------
# Macro dataclass
# ---------------------------------------------------------------------------


@dataclass
class Macro:
    """A structured macro extracted from storage XHTML.

    All attributes come from DIRECT children of the element only (recursive=False):
    - params: dict keyed by ``ac:name`` attribute of ``ac:parameter`` children
    - rich_body: the first direct ``ac:rich-text-body`` child, or None
    - plain_body: text of the first direct ``ac:plain-text-body`` child, or None

    This structure makes it impossible for a nested macro's params/body to be
    stolen by the outer macro (#45-class kill).
    """

    name: str
    element: Tag
    params: dict[str, str]
    rich_body: Tag | None
    plain_body: str | None


# ---------------------------------------------------------------------------
# Replacement type alias
# ---------------------------------------------------------------------------

Replacement = Tag | NavigableString | str | None
Handler = Callable[["Macro", "ConvertContext"], Replacement]


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    """Decorator: register a Handler for the given macro name.

    Usage::

        @register("code")
        def handle_code(macro: Macro, ctx: ConvertContext) -> Replacement:
            ...
    """
    def _decorator(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return _decorator


# ---------------------------------------------------------------------------
# parse_macro — the single extraction point
# ---------------------------------------------------------------------------


def parse_macro(element: Tag) -> Macro:
    """Extract name, params, and body from a structured-macro element.

    Uses ``recursive=False`` for ALL child lookups: a nested macro's params
    and bodies are invisible here, preventing the #45 class of param/body
    theft.

    For ``ac:plain-text-body``, CDATA content is returned via ``get_text()``
    (BeautifulSoup unwraps CDATA sections automatically with html.parser).
    """
    name = str(element.get("ac:name", "") or "")

    params: dict[str, str] = {}
    for p in element.find_all("ac:parameter", recursive=False):
        key = str(p.get("ac:name", "") or "")
        if key:
            params[key] = p.get_text()

    rich_body_tag = element.find("ac:rich-text-body", recursive=False)
    rich_body: Tag | None = rich_body_tag if isinstance(rich_body_tag, Tag) else None

    plain_body_tag = element.find("ac:plain-text-body", recursive=False)
    plain_body: str | None = None
    if isinstance(plain_body_tag, Tag):
        plain_body = plain_body_tag.get_text()

    return Macro(
        name=name,
        element=element,
        params=params,
        rich_body=rich_body,
        plain_body=plain_body,
    )


# ---------------------------------------------------------------------------
# default_handler
# ---------------------------------------------------------------------------


def dynamic_macro_placeholder(macro: Macro) -> Tag:
    """Build a VISIBLE italic placeholder Tag for a content-less dynamic macro.

    Mirrors v1 ``_convert_dynamic_macro_placeholder``: the reader sees
    ``[Confluence dynamic content: NAME (k=v, …)]`` — the macro name plus any
    non-default direct parameters (``ri:page`` references resolved to their
    content-title) — rather than an invisible HTML comment that markdownify
    escapes into literal ``<!-- … -->`` text.  Returns a ``<p><em>…</em></p>``
    Tag so it survives conversion as real markdown emphasis.
    """
    params: list[str] = []
    for p in macro.element.find_all("ac:parameter", recursive=False):
        name = p.get("ac:name", "")
        page_ref = p.find("ri:page")
        if page_ref is not None:
            value = (page_ref.get("ri:content-title", "") or "").strip()
        else:
            value = p.get_text().strip()
        if name and value:
            params.append(f"{name}={value}")
    suffix = f" ({', '.join(params)})" if params else ""
    soup = BeautifulSoup("", "html.parser")
    em = soup.new_tag("em")
    em.string = f"[Confluence dynamic content: {macro.name or 'unnamed'}{suffix}]"
    para = soup.new_tag("p")
    para.append(em)
    return para


def default_handler(macro: Macro, ctx: "ConvertContext") -> Replacement:
    """Fallback for unregistered macros.

    Three branches (in order):
    1. Has own body with non-empty text → render body content inline
       (returns the rich-text-body or falls back to plain_body text).
    2. Bodyless but wraps other macros → unwrap (drop the shell, keep children
       live for conversion). Direct ac:parameter children are decomposed so
       their raw parameter values cannot leak as body text.
    3. Otherwise → emit a visible ``[Confluence dynamic content: name]``
       placeholder (v1 parity).
    """
    # Branch 1: has own body with content
    if macro.rich_body is not None and macro.rich_body.get_text(strip=True):
        return macro.rich_body
    if macro.plain_body is not None and macro.plain_body.strip():
        return macro.plain_body

    # Branch 2: bodyless wrapper around other macros, OR ac:adf-extension.
    # ac:adf-extension is a native Cloud ADF wrapper that v1 unwrapped
    # generically (it was never treated as a structured-macro by v1's
    # converter).  Its children — plain content, ac:adf-node panels, lists —
    # must survive into the markdown.  Without this guard they fall through to
    # Branch 3 and are replaced by the empty placeholder comment, silently
    # destroying user content on Cloud pages.
    if (
        macro.element.find("ac:structured-macro") is not None
        or macro.element.name == "ac:adf-extension"
    ):
        # Drop own parameters to prevent raw param values leaking as text
        for p in macro.element.find_all("ac:parameter", recursive=False):
            p.decompose()
        macro.element.unwrap()
        return None  # signal: already handled in place

    # Branch 3: visible dynamic-content placeholder (v1 parity).
    return dynamic_macro_placeholder(macro)
