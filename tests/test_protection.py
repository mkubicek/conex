"""Unit tests for the typed protection model (protection.py).

Each hard-won decision the git prune/restore used to re-derive inline is now one
named, pure thing — a scope-typed wrapper, a single dual-form matcher, or a small
predicate — so a regression fails one of these tiny tests at the unit boundary
instead of a buried end-to-end prune path. This file is that boundary.
"""

from pathlib import Path

from confluence_export.media import MEDIA_DIR_NAME
from confluence_export.paths import nfc_casefold
from confluence_export.protection import (
    PageExactProtection,
    ProtectedDir,
    ProtectionSet,
    SubtreeProtection,
    _media_owner_dir,
    build_protected,
    media_file_is_preserved,
    media_owner_dirs_from_written_files,
    move_window_dirs,
    page_dirs_from_written_files,
    prune_media_owner_set,
)


class TestProtectionSetRouting:
    def test_from_exporter_routes_scope(self, tmp_path):
        # The single producer that replaces cli's hand-written '+': archived-exact
        # pages -> page_exact (PageExactProtection); blind _archived + skipped pages
        # -> subtrees (SubtreeProtection), order preserved; prune-media folded.
        ps = ProtectionSet.from_exporter(
            preserved_page_paths=[tmp_path / "pe"],
            preserved_paths=[tmp_path / "blind"],
            skipped_paths=[tmp_path / "skip"],
            prune_media_dirs=[tmp_path / "Page" / MEDIA_DIR_NAME],
            output_dir=tmp_path,
        )
        assert [p.path for p in ps.page_exact] == [tmp_path / "pe"]
        assert all(isinstance(p, PageExactProtection) for p in ps.page_exact)
        assert [s.path for s in ps.subtrees] == [tmp_path / "blind", tmp_path / "skip"]
        assert all(isinstance(s, SubtreeProtection) for s in ps.subtrees)
        assert ps.prune_media_owners == frozenset({(tmp_path / "Page").resolve()})

    def test_structural_equality(self, tmp_path):
        # ProtectionSet is frozen with hashable fields, so tests can assert exact
        # structural equality on the single typed argument (the cli routing test).
        a = ProtectionSet(page_exact=(PageExactProtection(tmp_path / "x"),))
        b = ProtectionSet(page_exact=(PageExactProtection(tmp_path / "x"),))
        assert a == b
        assert a != ProtectionSet(subtrees=(SubtreeProtection(tmp_path / "x"),))


class TestPruneMediaOwnerSet:
    def test_strips_media_and_resolves(self, tmp_path):
        owners = prune_media_owner_set(
            [tmp_path / "Page" / MEDIA_DIR_NAME, tmp_path / "Other"], tmp_path
        )
        assert (tmp_path / "Page").resolve() in owners  # .media stripped to owner
        assert (tmp_path / "Other").resolve() in owners  # non-media resolved as-is

    def test_output_dir_self_dropped(self, tmp_path):
        assert prune_media_owner_set([tmp_path], tmp_path) == frozenset()


class TestBuildProtected:
    def test_excludes_output_dir_self(self, tmp_path):
        # A protection whose path IS output_dir is dropped from both forms.
        ps = ProtectionSet(
            page_exact=(PageExactProtection(tmp_path),),
            subtrees=(SubtreeProtection(tmp_path / "keep"),),
        )
        page, sub = build_protected(tmp_path, ps)
        assert page == []
        assert len(sub) == 1 and sub[0].resolved == (tmp_path / "keep").resolve()

    def test_symlink_keeps_both_forms_never_unioned(self, tmp_path):
        # A protected dir that IS a symlink: resolved FOLLOWS it, lexical does NOT,
        # and the two are stored separately (never merged into one canonical key —
        # that union is what would make a symlink compare equal to its target).
        out = tmp_path / "out"
        out.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = out / "Page"
        link.symlink_to(outside)
        _, sub = build_protected(out, ProtectionSet(subtrees=(SubtreeProtection(link),)))
        d = sub[0]
        assert d.resolved == outside.resolve()  # resolved follows the symlink
        assert d.lexical == link.absolute()  # lexical does not
        assert d.resolved != d.lexical


class TestBuildProtectedCaseFold:
    """#44: with a case/Unicode fold, a protected dir matches a committed path that
    differs only in case — the prune folds its staleness key, so protection must
    fold too or a re-cased page is silently pruned. Folding is applied PER FORM so
    the resolved/lexical symlink dual is preserved. FS-independent (the fold is
    passed explicitly, not probed)."""

    @staticmethod
    def _f(p: Path) -> Path:
        return Path(nfc_casefold(str(p)))

    def test_folded_subtree_matches_other_case_file(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        _, sub = build_protected(
            out, ProtectionSet(subtrees=(SubtreeProtection(out / "FOO"),)), fold=nfc_casefold
        )
        committed = out / "Foo" / "Foo.md"  # same dir, different case
        assert sub[0].contains_subtree(
            self._f(committed.resolve()), self._f(committed.absolute())
        )

    def test_folded_page_exact_matches_other_case_owned_file(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        page, _ = build_protected(
            out, ProtectionSet(page_exact=(PageExactProtection(out / "FOO"),)), fold=nfc_casefold
        )
        owned = out / "Foo" / "Foo.md"
        assert page[0].owns_exactly(
            self._f(owned.resolve()), self._f(owned.absolute())
        )

    def test_identity_fold_stays_case_sensitive(self, tmp_path):
        # The identity default does no folding, so a different-case file does NOT
        # match. (It is a test-only no-op — NOT the prune's case-sensitive-FS fold,
        # which is `nfc` and still folds NFD→NFC; see build_protected's contract.)
        out = tmp_path / "out"
        out.mkdir()
        _, sub = build_protected(out, ProtectionSet(subtrees=(SubtreeProtection(out / "FOO"),)))
        other = out / "Foo" / "Foo.md"
        assert not sub[0].contains_subtree(other.resolve(), other.absolute())

    def test_fold_does_not_overmatch_sibling(self, tmp_path):
        # Folding must not make 'Foo' protect 'Foobar' — distinct dirs stay distinct.
        out = tmp_path / "out"
        out.mkdir()
        _, sub = build_protected(
            out, ProtectionSet(subtrees=(SubtreeProtection(out / "Foo"),)), fold=nfc_casefold
        )
        sibling = out / "Foobar" / "x.md"
        assert not sub[0].contains_subtree(
            self._f(sibling.resolve()), self._f(sibling.absolute())
        )


class TestProtectedDirDualForm:
    def test_pair_argument_matches_via_lexical_form(self, tmp_path):
        # The load-bearing security property: the match methods take the PRECOMPUTED
        # (resolved, lexical) pair. When the matcher's two forms diverge (as a
        # symlinked protected dir's do), a file under the LEXICAL form is matched —
        # even though its resolved form points elsewhere.
        d = ProtectedDir(resolved=tmp_path / "RES", lexical=tmp_path / "LEX")
        file_resolved = tmp_path / "ELSEWHERE" / "x.md"
        file_lexical = tmp_path / "LEX" / "x.md"
        assert d.contains_subtree(file_resolved, file_lexical) is True

    def test_single_path_rederive_trap_goes_dark(self, tmp_path):
        # If a caller "simplifies" to one path and re-derives both forms, the lexical
        # defense silently goes dark: passing (resolved, resolved) misses the match
        # the (resolved, lexical) pair caught above. This pins WHY the signature must
        # stay a pair (see ProtectedDir's security note).
        d = ProtectedDir(resolved=tmp_path / "RES", lexical=tmp_path / "LEX")
        file_resolved = tmp_path / "ELSEWHERE" / "x.md"
        assert d.contains_subtree(file_resolved, file_resolved) is False

    def test_owns_exactly_pair_argument_trap(self, tmp_path):
        # The same load-bearing pair-argument property for owns_exactly (page-exact)
        # that the previous test pins for contains_subtree. A review found the
        # owns_exactly integration path was only guarded indirectly; this pins the
        # matcher contract directly: a file owned ONLY via the lexical form is caught
        # by the (resolved, lexical) pair, and the single-path re-derive misses it.
        d = ProtectedDir(resolved=tmp_path / "RES", lexical=tmp_path / "LEX")
        file_resolved = tmp_path / "ELSEWHERE" / "x.md"
        file_lexical = tmp_path / "LEX" / "x.md"  # parent == d.lexical -> owned
        assert d.owns_exactly(file_resolved, file_lexical) is True
        assert d.owns_exactly(file_resolved, file_resolved) is False

    def test_never_unions_forms(self, tmp_path):
        d = ProtectedDir(resolved=tmp_path / "RES", lexical=tmp_path / "LEX")
        # under resolved-only, under lexical-only, under neither
        assert d.contains_subtree(tmp_path / "RES" / "x", tmp_path / "NO" / "x") is True
        assert d.contains_subtree(tmp_path / "NO" / "x", tmp_path / "LEX" / "x") is True
        assert d.contains_subtree(tmp_path / "NO" / "x", tmp_path / "NO" / "x") is False

    def test_owns_exactly_page_not_child(self, tmp_path):
        d = ProtectedDir(resolved=tmp_path / "P", lexical=tmp_path / "P")
        own = tmp_path / "P" / "P.md"
        assert d.owns_exactly(own, own) is True
        # a nested CHILD page's file is NOT owned page-exactly (M1-exact)
        child = tmp_path / "P" / "Child" / "Child.md"
        assert d.owns_exactly(child, child) is False
        # the page's own .media IS owned
        media = tmp_path / "P" / MEDIA_DIR_NAME / "a.png"
        assert d.owns_exactly(media, media) is True

    def test_none_form_never_matches(self, tmp_path):
        # A self-excluded form (None) is never consulted.
        d = ProtectedDir(resolved=None, lexical=tmp_path / "LEX")
        assert d.contains_subtree(tmp_path / "LEX" / "x", tmp_path / "LEX" / "x") is True
        d2 = ProtectedDir(resolved=tmp_path / "RES", lexical=None)
        assert d2.contains_subtree(tmp_path / "x", tmp_path / "x") is False


class TestMediaFileIsPreserved:
    def _owner(self, tmp_path: Path) -> Path:
        return (tmp_path / "Page").resolve()

    def test_M1a_keep_when_written_not_repruned(self, tmp_path):
        owner = self._owner(tmp_path)
        assert media_file_is_preserved(
            owner,
            written_page_dirs=frozenset({owner}),
            written_media_dirs=frozenset(),
            prune_media_owners=frozenset(),
            page_exact=[],
            subtrees=[],
        ) is True

    def test_RF_C_prune_when_owner_gone(self, tmp_path):
        owner = self._owner(tmp_path)
        assert media_file_is_preserved(
            owner,
            written_page_dirs=frozenset(),
            written_media_dirs=frozenset(),
            prune_media_owners=frozenset(),
            page_exact=[],
            subtrees=[],
        ) is False

    def test_RF_C_force_prune_override_beats_written(self, tmp_path):
        owner = self._owner(tmp_path)
        assert media_file_is_preserved(
            owner,
            written_page_dirs=frozenset({owner}),
            written_media_dirs=frozenset(),
            prune_media_owners=frozenset({owner}),
            page_exact=[],
            subtrees=[],
        ) is False

    def test_page_exact_and_subtree_keep(self, tmp_path):
        owner = self._owner(tmp_path)
        pe = ProtectedDir(resolved=owner, lexical=tmp_path / "Page")
        assert media_file_is_preserved(
            owner, written_page_dirs=frozenset(), written_media_dirs=frozenset(),
            prune_media_owners=frozenset(), page_exact=[pe], subtrees=[],
        ) is True
        root = (tmp_path / "root").resolve()
        child = (tmp_path / "root" / "child").resolve()
        sub = ProtectedDir(resolved=root, lexical=tmp_path / "root")
        assert media_file_is_preserved(
            child, written_page_dirs=frozenset(), written_media_dirs=frozenset(),
            prune_media_owners=frozenset(), page_exact=[], subtrees=[sub],
        ) is True

    def test_resolved_only_never_consults_lexical(self, tmp_path):
        # media-owner matching is RESOLVED-ONLY (the old inline check had no lexical
        # variant and owner is already resolved). A subtree matcher with ONLY a
        # lexical form does NOT preserve media, proving d.lexical is never consulted.
        real = (tmp_path / "real").resolve()
        owner = (real / "child").resolve()
        lex_only = ProtectedDir(resolved=None, lexical=real)
        assert media_file_is_preserved(
            owner, written_page_dirs=frozenset(), written_media_dirs=frozenset(),
            prune_media_owners=frozenset(), page_exact=[], subtrees=[lex_only],
        ) is False


class TestMoveWindowDirs:
    def test_current_plus_pre_reconcile(self, tmp_path):
        page_dir = tmp_path / "P"
        pre = {"id1": [tmp_path / "old1", tmp_path / "old2"]}
        assert move_window_dirs(page_dir, "id1", pre) == [
            page_dir, tmp_path / "old1", tmp_path / "old2"
        ]

    def test_missing_id_is_just_page_dir(self, tmp_path):
        page_dir = tmp_path / "P"
        assert move_window_dirs(page_dir, "absent", {"x": [tmp_path / "o"]}) == [page_dir]

    def test_empty_pre_reconcile_is_just_page_dir(self, tmp_path):
        page_dir = tmp_path / "P"
        assert move_window_dirs(page_dir, "id1", {}) == [page_dir]


class TestMediaOwnerDir:
    def test_returns_owner_dir_for_media_path(self, tmp_path):
        # A tracked .media file folds to the RESOLVED owner page dir (the path
        # segments before the .media component).
        rel = f"Space/Page/{MEDIA_DIR_NAME}/a.png"
        assert _media_owner_dir(tmp_path, rel) == (
            tmp_path / "Space" / "Page"
        ).resolve()

    def test_returns_none_when_not_under_media(self, tmp_path):
        # Line 247: a path with no .media component is not media-owned -> None.
        assert _media_owner_dir(tmp_path, "Space/Page/Page.md") is None


class TestPageDirsFromWrittenFiles:
    def test_media_file_folds_to_owner_dir(self, tmp_path):
        out = tmp_path / "out"
        media_file = out / "Space" / "Page" / MEDIA_DIR_NAME / "a.png"
        dirs = page_dirs_from_written_files(out, [media_file])
        assert dirs == frozenset({(out / "Space" / "Page").resolve()})

    def test_md_and_html_use_parent_dir(self, tmp_path):
        out = tmp_path / "out"
        md = out / "Space" / "Page" / "Page.md"
        html = out / "Space" / "Other" / "Other.html"
        dirs = page_dirs_from_written_files(out, [md, html])
        assert dirs == frozenset(
            {(out / "Space" / "Page").resolve(), (out / "Space" / "Other").resolve()}
        )

    def test_other_suffix_contributes_nothing(self, tmp_path):
        # A non-.md/.html, non-media file under output_dir is ignored entirely.
        out = tmp_path / "out"
        assert page_dirs_from_written_files(out, [out / "Page" / "note.txt"]) == (
            frozenset()
        )

    def test_file_outside_output_dir_is_skipped(self, tmp_path):
        # Lines 262-263: a written file that resolves OUTSIDE output_dir raises
        # ValueError on relative_to and is skipped, contributing nothing.
        out = tmp_path / "out"
        outside = tmp_path / "elsewhere" / "Page" / "Page.md"
        inside = out / "Page" / "Page.md"
        dirs = page_dirs_from_written_files(out, [outside, inside])
        assert dirs == frozenset({(out / "Page").resolve()})


class TestMediaOwnerDirsFromWrittenFiles:
    def test_media_file_folds_to_owner_dir(self, tmp_path):
        out = tmp_path / "out"
        media_file = out / "Space" / "Page" / MEDIA_DIR_NAME / "a.png"
        owners = media_owner_dirs_from_written_files(out, [media_file])
        assert owners == frozenset({(out / "Space" / "Page").resolve()})

    def test_non_media_file_contributes_nothing(self, tmp_path):
        # Only .media files count here; a plain page file under output_dir is ignored.
        out = tmp_path / "out"
        owners = media_owner_dirs_from_written_files(out, [out / "Page" / "Page.md"])
        assert owners == frozenset()

    def test_file_outside_output_dir_is_skipped(self, tmp_path):
        # Lines 283-284: a media file that resolves OUTSIDE output_dir raises
        # ValueError on relative_to and is skipped, contributing nothing.
        out = tmp_path / "out"
        outside = tmp_path / "elsewhere" / "Page" / MEDIA_DIR_NAME / "a.png"
        inside = out / "Page" / MEDIA_DIR_NAME / "b.png"
        owners = media_owner_dirs_from_written_files(out, [outside, inside])
        assert owners == frozenset({(out / "Page").resolve()})


def test_folded_subtree_matches_nfd_committed_path_with_nfc_fold(tmp_path):
    # The OTHER #44 axis: Unicode-form drift on a case-SENSITIVE filesystem,
    # where the shared fold is plain nfc (no casefold). A protected dir
    # registered in NFD must match a committed path in NFC form.
    import unicodedata

    from confluence_export.paths import nfc
    from confluence_export.protection import (
        ProtectionSet,
        SubtreeProtection,
        build_protected,
    )

    out = tmp_path / "out"
    out.mkdir()
    name_nfd = unicodedata.normalize("NFD", "Übersicht")
    name_nfc = unicodedata.normalize("NFC", "Übersicht")
    assert name_nfd != name_nfc

    _, sub = build_protected(
        out, ProtectionSet(subtrees=(SubtreeProtection(out / name_nfd),)), fold=nfc
    )
    committed = out / name_nfc / "x.md"
    folded_resolved = Path(nfc(str(committed.resolve())))
    folded_lexical = Path(nfc(str(committed.absolute())))
    assert sub[0].contains_subtree(folded_resolved, folded_lexical)
