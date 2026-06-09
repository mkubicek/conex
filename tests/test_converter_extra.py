"""Converter tests: Confluence HTML in, verify transformed output."""

from pathlib import Path

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
    ("drawio not rendered falls back", '<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">arch</ac:parameter></ac:structured-macro>', ["Draw.io diagram not rendered: arch.drawio"], ["[drawio:"]),
    ("drawio-sketch inline -> graceful note not dynamic placeholder (#6)", '<ac:structured-macro ac:name="drawio-sketch"><ac:parameter ac:name="mVer">2</ac:parameter></ac:structured-macro>', ["[Draw.io sketch]"], ["Confluence dynamic content"]),

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
    (
        "decision list renders as bullet list, decided marked (#40)",
        '<ac:adf-node type="decisionList">'
        '<ac:adf-node type="decisionItem" state="DECIDED"><p>Ship it</p></ac:adf-node>'
        '<ac:adf-node type="decisionItem" state="UNDECIDED"><p>Maybe later</p></ac:adf-node>'
        "</ac:adf-node>",
        ["<li>", "✓ Ship it", "Maybe later"], ["ac:adf-node", "✓ Maybe later"],
    ),
    (
        "decision list with only empty items is dropped (#40)",
        '<ac:adf-node type="decisionList"><ac:adf-node type="decisionItem"></ac:adf-node></ac:adf-node>',
        [], ["<li>", "ac:adf-node"],
    ),

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


def test_profile_picture_with_plain_text_param_does_not_leak_raw_id():
    # A profile-picture whose only content is a raw ac:parameter value (no nested
    # ri:user to resolve) must be dropped, not unwrapped — otherwise the raw
    # account-id leaks as visible body text.
    html = (
        '<p>Author: <ac:structured-macro ac:name="profile-picture">'
        '<ac:parameter ac:name="user">5f1a-accountid</ac:parameter>'
        "</ac:structured-macro></p>"
    )
    result = _preprocess_html(html, [])
    assert "5f1a-accountid" not in result
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
    # mention. Resolving the macro to a bare <span> leaves the link holding only
    # that span; the ac:link pass unwraps such a link (keeping the mention inline)
    # rather than decomposing it, which would silently drop the mention (#5).
    html = (
        "<p><ac:link><ac:structured-macro ac:name=\"profile-picture\">"
        '<ac:parameter ac:name="U"><ri:user ri:account-id="abc"/></ac:parameter>'
        "</ac:structured-macro></ac:link></p>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result
    assert "Confluence dynamic content" not in result


def test_two_profile_pictures_in_one_ac_link_keep_both_mentions():
    # Two avatars sharing a single ac:link: each profile-picture must resolve.
    # Replacing the whole link on the first macro detached the second; resolving
    # each macro in place and unwrapping the link keeps both mentions.
    html = (
        "<p><ac:link>"
        '<ac:structured-macro ac:name="profile-picture"><ac:parameter ac:name="U">'
        '<ri:user ri:account-id="u1"/></ac:parameter></ac:structured-macro>'
        '<ac:structured-macro ac:name="profile-picture"><ac:parameter ac:name="U">'
        '<ri:user ri:account-id="u2"/></ac:parameter></ac:structured-macro>'
        "</ac:link></p>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": f"N-{aid}"})
    assert "@N-u1" in result
    assert "@N-u2" in result


def test_macro_inside_ac_link_is_preserved():
    # F1: an ac:link wrapping a structured-macro must not be decomposed before the
    # macro-dispatch pass runs — the macro is preserved (link unwrapped) and
    # converted in place.
    html = (
        '<p><ac:link><ac:structured-macro ac:name="status">'
        '<ac:parameter ac:name="title">DONE</ac:parameter>'
        "</ac:structured-macro></ac:link></p>"
    )
    result = _preprocess_html(html, [])
    assert "DONE" in result


def test_two_bare_ri_users_in_one_link_both_resolved():
    # F2: two bare ri:user sharing one ac:link must each resolve. Replacing the
    # whole link on the first dropped the second.
    html = (
        '<p><ac:link><ri:user ri:account-id="a1"/>'
        '<ri:user ri:account-id="a2"/></ac:link></p>'
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": f"N-{aid}"})
    assert "@N-a1" in result
    assert "@N-a2" in result


def test_profile_macro_resolves_user_name():
    # F3: the profile macro must resolve the user's display name, not be starved
    # of its ri:user by the mention pre-pass (which left it showing "Unknown user").
    html = (
        '<ac:structured-macro ac:name="profile">'
        '<ac:parameter ac:name="User"><ri:user ri:account-id="a1"/></ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "Alice" in result
    assert "Unknown user" not in result


def test_nested_profile_picture_keeps_inner_mention():
    # F4: a profile-picture nested inside a profile-picture must keep the inner
    # resolved mention — the outer must not decompose the already-resolved span.
    html = (
        '<ac:structured-macro ac:name="profile-picture">'
        '<ac:structured-macro ac:name="profile-picture">'
        '<ri:user ri:account-id="a1"/>'
        "</ac:structured-macro></ac:structured-macro>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Alice"})
    assert "@Alice" in result


def test_drawio_nested_in_panel_still_renders():
    # A drawio diagram inside an info/panel body must still emit its real <img>.
    # _convert_panel re-parsed the body into a fresh soup, detaching the inner
    # macro from the dispatch snapshot so it was silently dropped; moving the live
    # body children keeps it attached and converted.
    html = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch</ac:parameter>'
        "</ac:structured-macro></ac:rich-text-body></ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="arch.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(html, atts, rendered={"arch.drawio": Path(".media/arch.drawio.png")})
    assert 'src=".media/arch.drawio.png"' in result
    assert "[drawio:" not in result


def test_drawio_sketch_attachment_backed_renders_png():
    # #6: an attachment-backed drawio-sketch renders its PNG like a full drawio macro.
    html = (
        '<ac:structured-macro ac:name="drawio-sketch">'
        '<ac:parameter ac:name="mVer">2</ac:parameter>'
        '<ri:attachment ri:filename="sketch.drawio"/>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="sketch.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(html, atts, rendered={"sketch.drawio": Path(".media/sketch.drawio.png")})
    assert 'src=".media/sketch.drawio.png"' in result
    assert "Confluence dynamic content" not in result


def test_drawio_sketch_attachment_backed_not_rendered_falls_back():
    # #6: attachment-backed sketch with no rendered PNG degrades to the same graceful
    # "not rendered" note as a drawio diagram, not a dynamic-content placeholder.
    html = (
        '<ac:structured-macro ac:name="drawio-sketch">'
        '<ri:attachment ri:filename="sketch.drawio"/>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="sketch.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(html, atts)
    assert "Draw.io diagram not rendered: sketch.drawio" in result
    assert "Confluence dynamic content" not in result


def test_drawio_rendered_lookup_is_case_and_space_insensitive():
    # F7: the exporter keys rendered[att.title]; a diagramName differing only in
    # case/whitespace from the attachment title must still find the PNG, not fall
    # through to a "not rendered" note.
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">my  diagram</ac:parameter>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="My Diagram.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(
        html, atts, rendered={"My Diagram.drawio": Path(".media/My Diagram.drawio.png")}
    )
    assert "not rendered" not in result
    assert "My%20Diagram.drawio.png" in result


def test_drawio_no_media_omits_dead_source_link():
    # F5: on a --no-media run the .drawio source is not on disk, so a source link
    # to .media/<name>.drawio would be dead. It must be omitted.
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch</ac:parameter>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="arch.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(html, atts, rendered={}, media_downloaded=False)
    assert "Draw.io source" not in result
    assert "not rendered" in result


def test_drawio_source_link_uses_actual_available_media():
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch</ac:parameter>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="arch.drawio", media_type="application/x-drawio")]

    no_media_with_source = _preprocess_html(
        html, atts, rendered={}, media_downloaded=False,
        available_media={"arch.drawio"},
    )
    failed_download = _preprocess_html(
        html, atts, rendered={}, media_downloaded=True,
        available_media=set(),
    )

    assert "Draw.io source" in no_media_with_source
    assert "Draw.io source" not in failed_download


def test_drawio_matches_uppercase_drawio_extension():
    # RF-D: a diagramName with an upper/mixed-case .drawio extension must still
    # match an attachment titled with a lowercase .drawio (the suffix strip must
    # be case-insensitive, not removesuffix-before-casefold).
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">Arch.DRAWIO</ac:parameter>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="Arch.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(
        html, atts, rendered={"Arch.drawio": Path(".media/Arch.drawio.png")}
    )
    assert "not rendered" not in result
    assert "Arch.drawio.png" in result


def test_drawio_source_link_uses_real_attachment_title():
    # F6: the source link must use the actual on-disk attachment title, not a
    # reconstructed bare+'.drawio'. Here the drawio attachment (by media-type)
    # has no .drawio extension, so reconstruction would point at a missing file.
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch</ac:parameter>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="arch", media_type="application/x-drawio")]
    result = _preprocess_html(html, atts, rendered={})
    assert '.media/arch"' in result          # links to the real title "arch"
    assert ".media/arch.drawio" not in result  # not the reconstructed name


def test_drawio_match_prefers_diagram_attachment_over_same_name_file():
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">arch</ac:parameter>'
        "</ac:structured-macro>"
    )
    atts = [
        Attachment(id="plain", title="arch", media_type="text/plain"),
        Attachment(id="drawio", title="arch.drawio", media_type="application/x-drawio"),
    ]

    result = _preprocess_html(
        html,
        atts,
        rendered={"arch.drawio": Path(".media/arch.drawio.png")},
    )

    assert 'src=".media/arch.drawio.png"' in result
    assert 'href=".media/arch.drawio"' in result
    assert 'href=".media/arch"' not in result


def test_drawio_match_accepts_mixed_case_drawio_extension():
    html = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">Arch</ac:parameter>'
        "</ac:structured-macro>"
    )
    atts = [Attachment(id="a", title="Arch.DRAWIO", media_type="application/octet-stream")]

    result = _preprocess_html(
        html,
        atts,
        rendered={"Arch.DRAWIO": Path(".media/Arch.DRAWIO.png")},
    )

    assert "not rendered" not in result
    assert "Arch.DRAWIO.png" in result


def test_view_file_nested_in_expand_still_renders():
    # Same nested-macro class for view-file inside an expand body.
    html = (
        '<ac:structured-macro ac:name="expand"><ac:rich-text-body>'
        '<ac:structured-macro ac:name="view-file">'
        '<ri:attachment ri:filename="report.pdf"/>'
        "</ac:structured-macro></ac:rich-text-body></ac:structured-macro>"
    )
    atts = [Attachment(id="b", title="report.pdf", media_type="application/pdf")]
    result = _preprocess_html(html, atts)
    assert ".media/report.pdf" in result


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


# -- draw.io: render-before-convert, real <img> not a [drawio:NAME] sentinel (#9, #8) --

_DRAWIO_MACRO = (
    '<ac:structured-macro ac:name="drawio">'
    '<ac:parameter ac:name="diagramName">{name}</ac:parameter>'
    "</ac:structured-macro>"
)


def test_drawio_rendered_emits_real_image():
    html = _DRAWIO_MACRO.format(name="arch")
    atts = [Attachment(id="a", title="arch.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(html, atts, rendered={"arch.drawio": Path(".media/arch.drawio.png")})
    assert 'src=".media/arch.drawio.png"' in result  # a real <img>, not a sentinel
    assert 'href=".media/arch.drawio"' in result      # source link
    assert "[drawio:" not in result
    assert "not rendered" not in result


def test_drawio_underscore_name_survives_markdownify():
    # #9: a diagramName containing '_' used to be markdownify-escaped ('foo\\_bar')
    # inside the [drawio:...] sentinel, so the post-pass string replace failed and
    # the literal token leaked. Emitting a real <img> before markdownify keeps the
    # rendered PNG link intact through the full pipeline.
    page = Page(
        id="1", title="Diagram Page", space_id="1",
        version=Version(created_at="2025-01-01T00:00:00Z", number=1),
        body_storage=_DRAWIO_MACRO.format(name="foo_bar"),
        webui="/x",
    )
    atts = [Attachment(id="a", title="foo_bar.drawio", media_type="application/x-drawio")]
    md = convert_page(
        page, base_url="https://x", space_key="T", path="/Diagram Page",
        attachments=atts, rendered={"foo_bar.drawio": Path(".media/foo_bar.drawio.png")},
    )
    assert ".media/foo_bar.drawio.png" in md  # rendered PNG link survives (not escaped)
    assert "[drawio:" not in md               # no leaked sentinel


def test_drawio_render_failure_keeps_source_link_no_sentinel():
    # #8: render failed (empty `rendered`) but the .drawio attachment exists → a
    # graceful "not rendered" note + source link, never a leaked [drawio:...] token.
    html = _DRAWIO_MACRO.format(name="arch")
    atts = [Attachment(id="a", title="arch.drawio", media_type="application/x-drawio")]
    result = _preprocess_html(html, atts, rendered={})
    assert "Draw.io diagram not rendered: arch.drawio" in result
    assert 'href=".media/arch.drawio"' in result  # source link still offered
    assert "[drawio:" not in result


def test_drawio_name_with_spaces_produces_valid_encoded_url():
    # A diagram/attachment name with spaces must yield a percent-encoded URL so
    # the image actually renders after markdownify (a raw space truncates the URL).
    page = Page(
        id="1", title="P", space_id="1",
        version=Version(created_at="2025-01-01T00:00:00Z", number=1),
        body_storage=_DRAWIO_MACRO.format(name="Foo Bar"), webui="/x",
    )
    atts = [Attachment(id="a", title="Foo Bar.drawio", media_type="application/x-drawio")]
    md_out = convert_page(
        page, base_url="https://x", space_key="T", path="/P",
        attachments=atts, rendered={"Foo Bar.drawio": Path(".media/Foo Bar.drawio.png")},
    )
    assert ".media/Foo%20Bar.drawio.png" in md_out      # space encoded -> valid URL
    assert "(.media/Foo Bar.drawio.png)" not in md_out  # no raw space in the URL


def test_drawio_name_with_injection_chars_is_neutralized():
    # A diagramName with markdown-structural chars must not break the image/link
    # syntax or inject a clickable javascript link (#9 review).
    page = Page(
        id="1", title="P", space_id="1",
        version=Version(created_at="2025-01-01T00:00:00Z", number=1),
        body_storage=_DRAWIO_MACRO.format(name="x](javascript:alert(1))"), webui="/x",
    )
    title = "x](javascript:alert(1)).drawio"
    atts = [Attachment(id="a", title=title, media_type="application/x-drawio")]
    md_out = convert_page(
        page, base_url="https://x", space_key="T", path="/P",
        attachments=atts, rendered={title: Path(".media/safe.drawio.png")},
    )
    assert ".media/safe.drawio.png" in md_out          # image still renders
    assert "javascript%3A" in md_out                   # source href percent-encoded (neutralized)
    body = md_out.split("# P", 1)[1]                   # exclude the YAML frontmatter
    assert "](javascript:" not in body                 # no injected clickable link in the body


# -- Missing-media fallbacks: available_media gates every attachment reference --
# When available_media is a set that does NOT contain the attachment's local name
# (e.g. a --no-media run, or a download that failed for that one file), the
# reference must degrade to a visible "Missing attachment" note instead of a dead
# link/image. Passing an empty set forces the missing branch for every reference.

def test_ac_image_missing_media_emits_missing_attachment_note():
    html = '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>'
    result = _preprocess_html(html, [], available_media=set())
    assert "Missing attachment: shot.png" in result
    assert ".media/shot.png" not in result  # no dead image src
    assert "ac:image" not in result


def test_ac_link_attachment_missing_media_emits_missing_attachment_note():
    # The note must use the link's label text, not the raw filename.
    html = (
        '<ac:link><ri:attachment ri:filename="doc.pdf"/>'
        "<ac:plain-text-link-body>My Doc</ac:plain-text-link-body></ac:link>"
    )
    result = _preprocess_html(html, [], available_media=set())
    assert "Missing attachment: My Doc" in result
    assert ".media/doc.pdf" not in result  # no dead href


def test_view_file_missing_media_emits_missing_attachment_note():
    html = (
        '<ac:structured-macro ac:name="view-file">'
        '<ri:attachment ri:filename="report.pdf"/></ac:structured-macro>'
    )
    result = _preprocess_html(html, [], available_media=set())
    assert "Missing attachment: report.pdf" in result
    assert ".media/report.pdf" not in result


# -- Stray ac:/ri: tag cleanup keeps inner text -------------------------------

def test_stray_ri_tag_is_unwrapped_keeping_text():
    # Any ac:/ri: tag not consumed by an earlier pass is unwrapped at the end so
    # its text survives rather than leaking the namespaced tag into the markdown.
    result = _preprocess_html("<ri:something>kept text</ri:something>", [])
    assert "kept text" in result
    assert "ri:something" not in result


# -- ac:link straggler / empty-link handling ----------------------------------

def test_ac_link_wrapping_profile_macro_unwraps_link_keeps_user():
    # A ri:user the user pre-pass deliberately leaves alone (it belongs to a
    # profile macro) survives into the ac:link pass as a straggler. The link is
    # unwrapped (not dropped), so the profile macro still resolves to its mention.
    html = (
        "<ac:link><ac:structured-macro ac:name=\"profile\">"
        '<ac:parameter ac:name="U"><ri:user ri:account-id="z"/></ac:parameter>'
        "</ac:structured-macro></ac:link>"
    )
    result = _preprocess_html(html, [], user_resolver=lambda aid: {"displayName": "Zed"})
    assert "Zed" in result
    assert "Unknown user" not in result


def test_empty_ac_link_is_dropped():
    # A genuinely empty ac:link (no ri: child, no content) is removed entirely,
    # leaving the surrounding text intact.
    result = _preprocess_html("<p>a<ac:link></ac:link>b</p>", [])
    assert "ab" in result
    assert "ac:link" not in result


# -- profile macro with email renders "Name (email)" --------------------------

def test_profile_macro_with_email_renders_name_and_email():
    html = (
        '<ac:structured-macro ac:name="profile">'
        '<ac:parameter ac:name="U"><ri:user ri:account-id="x"/></ac:parameter>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(
        html, [], user_resolver=lambda aid: {"displayName": "Bob", "email": "b@x.io"}
    )
    assert "Bob (b@x.io)" in result


# -- nested empty profile-pictures degrade cleanly (detached-macro guard) ------

def test_nested_empty_profile_pictures_drop_without_crashing():
    # Both macros lack a ri:user, so the user pre-pass leaves them for the
    # structured-macro pass. The OUTER empty profile-picture is decomposed first,
    # detaching the INNER (still in the find_all snapshot). The detached-node guard
    # skips the inner instead of calling methods on a decomposed node (which would
    # raise and abort the export). Result: both dropped cleanly, no markup leaks.
    html = (
        '<ac:structured-macro ac:name="profile-picture">'
        '<ac:structured-macro ac:name="profile-picture"></ac:structured-macro>'
        "</ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "structured-macro" not in result
    assert "Confluence dynamic content" not in result
    assert result.strip() == ""


# -- attachment-id disambiguation through ri:content-id -----------------------

def test_ac_image_resolves_id_specific_local_name_via_content_id():
    # Two attachments share the filename "shot.png"; the name plan disambiguates
    # them by attachment id. An ac:image referencing one via ri:content-id must
    # resolve to that id's distinct local name, not the bare filename.
    atts = [
        Attachment(id="id1", title="shot.png", media_type="image/png"),
        Attachment(id="id2", title="shot.png", media_type="image/png"),
    ]
    html = '<ac:image><ri:attachment ri:filename="shot.png" ri:content-id="id2"/></ac:image>'
    result = _preprocess_html(html, atts)
    assert ".media/shot-id2.png" in result
    assert 'src=".media/shot.png"' not in result


# -- #45: untitled panel/expand must not steal a nested macro's title ---------

def test_untitled_panel_does_not_steal_nested_macro_title():
    # #45: the recursive title lookup reached INTO the nested status macro and
    # stole its title param — the Info header became "DONE" and the status text
    # was emitted twice (once as the header, once by the nested conversion).
    html = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        '<ac:structured-macro ac:name="status">'
        '<ac:parameter ac:name="title">DONE</ac:parameter>'
        "</ac:structured-macro>Some text"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "<strong>Info</strong>" in result
    assert result.count("DONE") == 1
    assert "Some text" in result


def test_untitled_expand_does_not_steal_nested_panel_title():
    # #45: same steal in _convert_expand — an untitled expand around a titled
    # inner panel must fall back to "Details", and the inner panel must keep its
    # own header (one "Inner", not an expand header + a panel header).
    html = (
        '<ac:structured-macro ac:name="expand"><ac:rich-text-body>'
        '<ac:structured-macro ac:name="info">'
        '<ac:parameter ac:name="title">Inner</ac:parameter>'
        "<ac:rich-text-body><p>Body</p></ac:rich-text-body>"
        "</ac:structured-macro></ac:rich-text-body></ac:structured-macro>"
    )
    result = _preprocess_html(html, [])
    assert "<h4>Details</h4>" in result
    assert result.count("Inner") == 1
    assert "Body" in result


def test_nested_decision_list_not_double_rendered():
    # #43: a decisionList nested inside a decisionItem is not producible by real
    # Confluence ADF (item content is inline-only) — hardening. The outer list's
    # recursive item scan must not emit the inner list's items a second time.
    html = (
        '<ac:adf-node type="decisionList">'
        '<ac:adf-node type="decisionItem" state="DECIDED"><p>Outer</p>'
        '<ac:adf-node type="decisionList">'
        '<ac:adf-node type="decisionItem"><p>Inner</p></ac:adf-node>'
        "</ac:adf-node>"
        "</ac:adf-node>"
        "</ac:adf-node>"
    )
    result = _preprocess_html(html, [])
    assert result.count("Inner") == 1
    assert result.count("Outer") == 1
