"""Additional converter tests for uncovered macro/element handling."""

from confluence_export.converter import _preprocess_html


class TestEmoticons:
    def test_known_emoticon(self):
        html = '<ac:emoticon ac:name="tick"/>'
        assert "\u2705" in _preprocess_html(html, [])

    def test_unknown_emoticon_removed(self):
        html = '<ac:emoticon ac:name="nonexistent-xyz"/>'
        result = _preprocess_html(html, [])
        assert "ac:emoticon" not in result

    def test_emoji_shortname_fallback(self):
        html = '<ac:emoticon ac:name="" ac:emoji-shortname=":tick:"/>'
        assert "\u2705" in _preprocess_html(html, [])


class TestTimeTags:
    def test_datetime_preserved(self):
        html = '<time datetime="2025-03-15"/>'
        assert "2025-03-15" in _preprocess_html(html, [])

    def test_empty_datetime_removed(self):
        html = "<time/>"
        result = _preprocess_html(html, [])
        assert "<time" not in result


class TestTaskLists:
    def test_complete_task(self):
        html = (
            "<ac:task-list>"
            "<ac:task><ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Done item</ac:task-body></ac:task>"
            "</ac:task-list>"
        )
        result = _preprocess_html(html, [])
        assert "[x]" in result
        assert "Done item" in result

    def test_incomplete_task(self):
        html = (
            "<ac:task-list>"
            "<ac:task><ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Todo item</ac:task-body></ac:task>"
            "</ac:task-list>"
        )
        result = _preprocess_html(html, [])
        assert "[ ]" in result
        assert "Todo item" in result


class TestUserMentions:
    def test_user_mention_with_resolver(self):
        html = '<ac:link><ri:user ri:account-id="abc123"/></ac:link>'
        resolver = lambda aid: {"displayName": "Alice"}
        result = _preprocess_html(html, [], user_resolver=resolver)
        assert "@Alice" in result

    def test_user_mention_without_resolver(self):
        html = '<ac:link><ri:user ri:account-id="abc123"/></ac:link>'
        result = _preprocess_html(html, [])
        assert "@abc123" in result


class TestPageLinks:
    def test_page_link(self):
        html = (
            '<ac:link><ri:page ri:content-title="Other Page"/>'
            "<ac:plain-text-link-body>See here</ac:plain-text-link-body></ac:link>"
        )
        result = _preprocess_html(html, [])
        assert "See here" in result


class TestExternalImage:
    def test_external_url(self):
        html = '<ac:image><ri:url ri:value="https://example.com/img.png"/></ac:image>'
        result = _preprocess_html(html, [])
        assert "https://example.com/img.png" in result

    def test_image_no_source_removed(self):
        html = "<ac:image></ac:image>"
        result = _preprocess_html(html, [])
        assert "ac:image" not in result


class TestMacros:
    def test_status_macro(self):
        html = (
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">IN PROGRESS</ac:parameter>'
            '<ac:parameter ac:name="colour">Blue</ac:parameter>'
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "IN PROGRESS" in result

    def test_status_macro_empty(self):
        html = '<ac:structured-macro ac:name="status"></ac:structured-macro>'
        result = _preprocess_html(html, [])
        assert "ac:structured-macro" not in result

    def test_expand_macro(self):
        html = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">Click to expand</ac:parameter>'
            "<ac:rich-text-body><p>Hidden content</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "Click to expand" in result
        assert "Hidden content" in result

    def test_jira_macro(self):
        html = (
            '<ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">PROJ-123</ac:parameter>'
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "PROJ-123" in result

    def test_jira_macro_empty(self):
        html = '<ac:structured-macro ac:name="jira"></ac:structured-macro>'
        result = _preprocess_html(html, [])
        assert "ac:structured-macro" not in result

    def test_view_file_macro(self):
        html = (
            '<ac:structured-macro ac:name="view-file">'
            '<ri:attachment ri:filename="report.pdf"/>'
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "media/report.pdf" in result

    def test_view_file_macro_empty(self):
        html = '<ac:structured-macro ac:name="view-file"></ac:structured-macro>'
        result = _preprocess_html(html, [])
        assert "ac:structured-macro" not in result

    def test_toc_macro_removed(self):
        html = '<ac:structured-macro ac:name="toc"></ac:structured-macro>'
        result = _preprocess_html(html, [])
        assert result.strip() == ""

    def test_excerpt_macro_preserves_body(self):
        html = (
            '<ac:structured-macro ac:name="excerpt">'
            "<ac:rich-text-body><p>Excerpt text</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "Excerpt text" in result

    def test_section_column_macro(self):
        html = (
            '<ac:structured-macro ac:name="section">'
            "<ac:rich-text-body>"
            '<ac:structured-macro ac:name="column">'
            "<ac:rich-text-body><p>Column content</p></ac:rich-text-body>"
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "Column content" in result

    def test_unknown_macro_keeps_body(self):
        html = (
            '<ac:structured-macro ac:name="unknown-thing">'
            "<ac:rich-text-body><p>Keep this</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "Keep this" in result

    def test_unknown_macro_no_body_removed(self):
        html = '<ac:structured-macro ac:name="unknown-thing"></ac:structured-macro>'
        result = _preprocess_html(html, [])
        assert result.strip() == ""

    def test_profile_macro(self):
        """Profile macro's ri:user is consumed by user mention handler first."""
        html = (
            '<ac:structured-macro ac:name="profile">'
            '<ri:user ri:account-id="user1"/>'
            "</ac:structured-macro>"
        )
        resolver = lambda aid: {"displayName": "Bob", "email": "bob@test.com"}
        result = _preprocess_html(html, [], user_resolver=resolver)
        # ri:user is resolved to @Bob by the mention handler, then profile
        # macro sees no ri:user and falls back to "Unknown user"
        assert "Bob" in result or "Unknown user" in result

    def test_profile_macro_no_user(self):
        """Profile macro with no user tag."""
        html = '<ac:structured-macro ac:name="profile"></ac:structured-macro>'
        result = _preprocess_html(html, [])
        assert "Unknown user" in result


class TestDecisionLists:
    def test_decision_item_with_text(self):
        html = '<ac:adf-node type="decisionItem" state="DECIDED"><p>We decided X</p></ac:adf-node>'
        result = _preprocess_html(html, [])
        assert "We decided X" in result

    def test_decision_item_empty_removed(self):
        html = '<ac:adf-node type="decisionItem"></ac:adf-node>'
        result = _preprocess_html(html, [])
        assert result.strip() == ""


class TestViewFileFallback:
    def test_name_param_fallback(self):
        html = (
            '<ac:structured-macro ac:name="view-file">'
            '<ac:parameter ac:name="name">doc.pdf</ac:parameter>'
            "</ac:structured-macro>"
        )
        result = _preprocess_html(html, [])
        assert "media/doc.pdf" in result


class TestAcLinkStraggler:
    def test_user_link_straggler(self):
        html = '<ac:link><ri:user ri:account-id="u1"/><ac:link-body>Someone</ac:link-body></ac:link>'
        result = _preprocess_html(html, [])
        assert "u1" in result or "Someone" in result

    def test_unknown_link_decomposed(self):
        html = "<ac:link>mystery</ac:link>"
        result = _preprocess_html(html, [])
        assert "ac:link" not in result


class TestLayoutAndCleanup:
    def test_layout_tags_unwrapped(self):
        html = "<ac:layout><ac:layout-section><ac:layout-cell><p>Content</p></ac:layout-cell></ac:layout-section></ac:layout>"
        result = _preprocess_html(html, [])
        assert "Content" in result
        assert "ac:layout" not in result

    def test_inline_comment_unwrapped(self):
        html = '<p>Some <ac:inline-comment-marker ac:ref="abc">commented</ac:inline-comment-marker> text</p>'
        result = _preprocess_html(html, [])
        assert "commented" in result
        assert "ac:inline-comment-marker" not in result

    def test_placeholder_removed(self):
        html = "<ac:placeholder>Type here</ac:placeholder>"
        result = _preprocess_html(html, [])
        assert "Type here" not in result

    def test_adf_fallback_removed(self):
        html = "<ac:adf-content><p>Real</p></ac:adf-content><ac:adf-fallback><p>Duplicate</p></ac:adf-fallback>"
        result = _preprocess_html(html, [])
        assert "Real" in result
        assert "Duplicate" not in result
