"""Converter tests: Confluence HTML in, verify transformed output."""

import pytest

from confluence_export.converter import _preprocess_html, convert_page
from confluence_export.types import Attachment, Page, Version


# -- Parametrized preprocessing tests ----------------------------------------
# Each case: (description, input HTML, expected substrings, forbidden substrings)

PREPROCESS_CASES = [
    # Emoticons
    ("known emoticon tick", '<ac:emoticon ac:name="tick"/>', ["\u2705"], []),
    ("unknown emoticon removed", '<ac:emoticon ac:name="zzz-unknown"/>', [], ["ac:emoticon"]),
    ("emoji shortname fallback", '<ac:emoticon ac:name="" ac:emoji-shortname=":tick:"/>', ["\u2705"], []),

    # Time tags
    ("datetime preserved", '<time datetime="2025-03-15"/>', ["2025-03-15"], []),
    ("empty time removed", "<time/>", [], ["<time"]),

    # Task lists
    (
        "complete task",
        "<ac:task-list><ac:task><ac:task-status>complete</ac:task-status>"
        "<ac:task-body>Done item</ac:task-body></ac:task></ac:task-list>",
        ["[x]", "Done item"], [],
    ),
    (
        "incomplete task",
        "<ac:task-list><ac:task><ac:task-status>incomplete</ac:task-status>"
        "<ac:task-body>Todo item</ac:task-body></ac:task></ac:task-list>",
        ["[ ]", "Todo item"], [],
    ),

    # Images
    ("attachment image", '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>', [".media/shot.png"], ["ac:image"]),
    ("external image", '<ac:image><ri:url ri:value="https://example.com/img.png"/></ac:image>', ["https://example.com/img.png"], []),
    ("image no source removed", "<ac:image></ac:image>", [], ["ac:image"]),

    # Links
    (
        "attachment link",
        '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
        "<ac:plain-text-link-body>My Doc</ac:plain-text-link-body></ac:link>",
        [".media/doc.pdf", "My Doc"], [],
    ),
    (
        "page link preserves label",
        '<ac:link><ri:page ri:content-title="Other Page"/>'
        "<ac:plain-text-link-body>See here</ac:plain-text-link-body></ac:link>",
        ["See here"], [],
    ),

    # Macros: panels, code, status, expand, jira, view-file, drawio
    (
        "info panel",
        '<ac:structured-macro ac:name="info">'
        '<ac:parameter ac:name="title">Important</ac:parameter>'
        "<ac:rich-text-body><p>Details here</p></ac:rich-text-body></ac:structured-macro>",
        ["Important", "Details here", "<blockquote>"], [],
    ),
    (
        "code block",
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body>print('hi')</ac:plain-text-body></ac:structured-macro>",
        ["<pre>", "<code", "print('hi')"], [],
    ),
    ("status macro", '<ac:structured-macro ac:name="status"><ac:parameter ac:name="title">DONE</ac:parameter></ac:structured-macro>', ["DONE"], []),
    ("status macro empty", '<ac:structured-macro ac:name="status"></ac:structured-macro>', [], ["ac:structured-macro"]),
    (
        "expand macro",
        '<ac:structured-macro ac:name="expand">'
        '<ac:parameter ac:name="title">More</ac:parameter>'
        "<ac:rich-text-body><p>Details</p></ac:rich-text-body></ac:structured-macro>",
        ["More", "Details"], [],
    ),
    ("jira macro", '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PROJ-42</ac:parameter></ac:structured-macro>', ["PROJ-42"], []),
    ("jira macro empty", '<ac:structured-macro ac:name="jira"></ac:structured-macro>', [], ["ac:structured-macro"]),
    ("view-file via ri:attachment", '<ac:structured-macro ac:name="view-file"><ri:attachment ri:filename="report.pdf"/></ac:structured-macro>', [".media/report.pdf"], []),
    ("view-file via name param", '<ac:structured-macro ac:name="view-file"><ac:parameter ac:name="name">doc.pdf</ac:parameter></ac:structured-macro>', [".media/doc.pdf"], []),
    ("view-file empty", '<ac:structured-macro ac:name="view-file"></ac:structured-macro>', [], ["ac:structured-macro"]),
    ("drawio placeholder", '<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">arch</ac:parameter></ac:structured-macro>', ["[drawio:arch]"], []),

    # Structural macros
    ("toc placeholder", '<ac:structured-macro ac:name="toc"></ac:structured-macro>', ["[Confluence dynamic content: toc]"], ["ac:structured-macro"]),
    ("children placeholder", '<ac:structured-macro ac:name="children"></ac:structured-macro>', ["[Confluence dynamic content: children]"], []),
    ("recently-updated placeholder", '<ac:structured-macro ac:name="recently-updated"></ac:structured-macro>', ["[Confluence dynamic content: recently-updated]"], []),
    (
        "include macro placeholder with page param",
        '<ac:structured-macro ac:name="include">'
        '<ac:parameter ac:name="page"><ri:page ri:content-title="Other Page"/></ac:parameter>'
        "</ac:structured-macro>",
        ["[Confluence dynamic content: include (page=Other Page)]"], [],
    ),
    (
        "unknown future macro placeholder",
        '<ac:structured-macro ac:name="future-2099-widget">'
        '<ac:parameter ac:name="depth">3</ac:parameter>'
        "</ac:structured-macro>",
        ["[Confluence dynamic content: future-2099-widget (depth=3)]"], [],
    ),
    ("excerpt preserves body", '<ac:structured-macro ac:name="excerpt"><ac:rich-text-body><p>Summary</p></ac:rich-text-body></ac:structured-macro>', ["Summary"], []),
    (
        "section/column unwrapped",
        '<ac:structured-macro ac:name="section"><ac:rich-text-body>'
        '<ac:structured-macro ac:name="column"><ac:rich-text-body><p>Col</p></ac:rich-text-body></ac:structured-macro>'
        "</ac:rich-text-body></ac:structured-macro>",
        ["Col"], [],
    ),
    ("unknown macro keeps body", '<ac:structured-macro ac:name="xyz"><ac:rich-text-body><p>Keep</p></ac:rich-text-body></ac:structured-macro>', ["Keep"], []),
    ("unknown macro no body emits placeholder", '<ac:structured-macro ac:name="xyz"></ac:structured-macro>', ["[Confluence dynamic content: xyz]"], []),

    # Decision lists
    ("decision item with text", '<ac:adf-node type="decisionItem" state="DECIDED"><p>Decided X</p></ac:adf-node>', ["Decided X"], []),
    ("decision item empty removed", '<ac:adf-node type="decisionItem"></ac:adf-node>', [], []),

    # Layout & cleanup
    ("layout tags unwrapped", "<ac:layout><ac:layout-section><ac:layout-cell><p>Inner</p></ac:layout-cell></ac:layout-section></ac:layout>", ["Inner"], ["ac:layout"]),
    ("inline comment unwrapped", '<p>Before <ac:inline-comment-marker ac:ref="x">noted</ac:inline-comment-marker> after</p>', ["noted"], ["ac:inline-comment-marker"]),
    ("placeholder removed", "<ac:placeholder>Type here</ac:placeholder>", [], ["Type here"]),
    ("adf-fallback removed", "<ac:adf-content><p>Real</p></ac:adf-content><ac:adf-fallback><p>Dupe</p></ac:adf-fallback>", ["Real"], ["Dupe"]),

    # Profile macro (ri:user consumed by mention handler first)
    ("profile macro no user", '<ac:structured-macro ac:name="profile"></ac:structured-macro>', ["Unknown user"], []),
    # profile-picture with no user: dropped silently, never a dynamic-content placeholder (#5)
    ("profile-picture no user dropped", '<ac:structured-macro ac:name="profile-picture"></ac:structured-macro>', [], ["Confluence dynamic content"]),
]


@pytest.mark.parametrize("desc,html,expected,forbidden", PREPROCESS_CASES, ids=[c[0] for c in PREPROCESS_CASES])
def test_preprocess(desc, html, expected, forbidden):
    result = _preprocess_html(html, [])
    for s in expected:
        assert s in result, f"expected '{s}' in output for: {desc}"
    for s in forbidden:
        assert s not in result, f"forbidden '{s}' found in output for: {desc}"


# -- Tests that need a user resolver -----------------------------------------

def test_user_mention_resolved():
    html = '<ac:link><ri:user ri:account-id="abc"/></ac:link>'
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result


def test_user_mention_unresolved_shows_id():
    html = '<ac:link><ri:user ri:account-id="abc"/></ac:link>'
    result = _preprocess_html(html, [])
    assert "@abc" in result


# -- profile-picture macro renders as an inline mention, not a placeholder (#5) --

_PROFILE_PIC = (
    '<ac:structured-macro ac:name="profile-picture">'
    '<ac:parameter ac:name="User"><ri:user ri:account-id="abc"/></ac:parameter>'
    "</ac:structured-macro>"
)


def test_profile_picture_renders_resolved_mention():
    result = _preprocess_html(_PROFILE_PIC, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result
    assert "Confluence dynamic content" not in result  # no placeholder noise


def test_profile_picture_unresolved_shows_id():
    result = _preprocess_html(_PROFILE_PIC, [])
    assert "@abc" in result
    assert "Confluence dynamic content" not in result


def test_profile_picture_resolver_without_name_falls_back_to_id():
    # Resolver present but can't resolve the account id → fall back to the id.
    result = _preprocess_html(_PROFILE_PIC, [], user_resolver=lambda aid: None)
    assert "@abc" in result
    assert "Confluence dynamic content" not in result


def test_profile_picture_empty_account_id_dropped():
    # A profile-picture whose ri:user carries no account-id → dropped cleanly,
    # no mention text and no dynamic-content placeholder.
    html = (
        '<ac:structured-macro ac:name="profile-picture">'
        '<ac:parameter ac:name="User"><ri:user/></ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "@" not in result
    assert "Confluence dynamic content" not in result


def test_profile_picture_inside_panel_keeps_mention():
    # Regression guard (#5 follow-up): a profile-picture nested in a panel must
    # keep its mention. _convert_panel re-parses the panel body into a fresh soup,
    # so a macro resolved only in the later structured-macro pass would be detached
    # from that pass's snapshot and the mention silently dropped. Resolving the
    # profile-picture in the user-mention pre-pass (before any panel re-parse)
    # survives it.
    html = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        '<p>Owner: <ac:structured-macro ac:name="profile-picture">'
        '<ac:parameter ac:name="User"><ri:user ri:account-id="abc"/></ac:parameter>'
        "</ac:structured-macro></p>"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result
    assert "Confluence dynamic content" not in result


def test_profile_picture_two_ri_users_does_not_crash():
    # Hardening: resolving a profile-picture in the pre-pass replaces the WHOLE
    # macro, detaching any sibling ri:user still in the find_all snapshot. Without
    # a detached-node guard the second iteration called replace_with on a node not
    # in the tree -> ValueError, aborting the entire export. Malformed/atypical
    # input (a well-formed macro has one ri:user) but the blast radius is the whole
    # run, so it must degrade gracefully to the first resolved mention.
    html = (
        '<ac:structured-macro ac:name="profile-picture">'
        '<ac:parameter ac:name="U1"><ri:user ri:account-id="abc"/></ac:parameter>'
        '<ac:parameter ac:name="U2"><ri:user ri:account-id="def"/></ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result


def test_profile_picture_empty_then_second_ri_user_does_not_crash():
    # Hardening: a first empty-account-id ri:user decomposes the macro, which
    # destroys (attrs=None) a second snapshotted ri:user; without the guard the
    # next iteration did user_tag.get(...) on the dead node -> AttributeError,
    # aborting the export. Must degrade to a clean drop instead.
    html = (
        '<ac:structured-macro ac:name="profile-picture">'
        '<ac:parameter ac:name="a"><ri:user ri:account-id=""/></ac:parameter>'
        '<ac:parameter ac:name="b"><ri:user ri:account-id="def"/></ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "@" not in result
    assert "Confluence dynamic content" not in result


def test_profile_picture_inside_ac_link_keeps_mention():
    # Regression guard: a profile-picture wrapped in an ac:link must keep its
    # mention. Resolving the macro to a bare <span> while it is still inside the
    # link left the later ac:link pass with a link whose only child is a plain
    # span -> it fell through to decompose and silently dropped the mention (the
    # exact failure class #5 fixes). Retargeting the enclosing ac:link survives it.
    html = (
        "<p><ac:link><ac:structured-macro ac:name=\"profile-picture\">"
        '<ac:parameter ac:name="U"><ri:user ri:account-id="abc"/></ac:parameter>'
        "</ac:structured-macro></ac:link></p>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result
    assert "Confluence dynamic content" not in result


# -- Full pipeline test: HTML → markdown with frontmatter --------------------

def test_full_conversion_pipeline():
    """End-to-end: Confluence storage HTML → final markdown string."""
    page = Page(
        id="42", title="Architecture Overview", space_id="1",
        version=Version(created_at="2025-06-15T10:00:00Z", number=7),
        body_storage=(
            "<h2>Summary</h2>"
            "<p>This page describes the <strong>system architecture</strong>.</p>"
            '<ac:structured-macro ac:name="info">'
            '<ac:parameter ac:name="title">Note</ac:parameter>'
            "<ac:rich-text-body><p>Updated for v2.</p></ac:rich-text-body>"
            "</ac:structured-macro>"
            '<ac:image><ri:attachment ri:filename="diagram.png"/></ac:image>'
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">yaml</ac:parameter>'
            "<ac:plain-text-body>services:\n  api: true</ac:plain-text-body>"
            "</ac:structured-macro>"
        ),
        webui="/spaces/TEST/pages/42",
    )
    att = Attachment(id="a1", title="diagram.png", media_type="image/png",
                     file_size=5000, page_id="42")

    md = convert_page(page, base_url="https://x.atlassian.net",
                       space_key="TEST", path="/Architecture Overview",
                       attachments=[att])

    # Frontmatter
    assert "title: Architecture Overview" in md
    assert "page_id:" in md
    assert "version: 7" in md

    # Content conversion
    assert "## Summary" in md
    assert "**system architecture**" in md

    # Info panel became blockquote
    assert "Note" in md
    assert "Updated for v2" in md

    # Image converted to markdown
    assert ".media/diagram.png" in md

    # Code block preserved
    assert "services:" in md
    assert "api: true" in md

    # No Confluence markup leaked
    assert "ac:structured-macro" not in md
    assert "ac:image" not in md
    assert "ri:attachment" not in md
