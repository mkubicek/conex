"""Tests for git versioning module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from confluence_export.git import (
    _chunked_paths,
    _is_secret_config_relpath,
    commit_export,
    commit_local_changes,
    ensure_repo,
    git_available,
)
from confluence_export.protection import (
    PageExactProtection,
    ProtectionSet,
    SubtreeProtection,
    prune_media_owner_set,
)


def _prot(*, page_exact=(), subtrees=(), prune_media=(), output_dir=None) -> ProtectionSet:
    """Build a typed ProtectionSet from plain dir lists for commit_export tests.
    Mirrors the production scope routing (page-exact vs recursive) explicitly so a
    test pins exactly the protections it intends — there is no untyped slot."""
    return ProtectionSet(
        page_exact=tuple(PageExactProtection(p) for p in page_exact),
        subtrees=tuple(SubtreeProtection(p) for p in subtrees),
        prune_media_owners=(
            prune_media_owner_set(list(prune_media), output_dir)
            if prune_media else frozenset()
        ),
    )


class TestChunkedPaths:
    def test_empty_input_yields_nothing(self):
        assert list(_chunked_paths([])) == []

    def test_single_small_path_one_batch(self):
        batches = list(_chunked_paths(["a.md"]))
        assert batches == [["a.md"]]

    def test_all_paths_fit_in_one_batch(self):
        paths = [f"page-{i}.md" for i in range(50)]
        batches = list(_chunked_paths(paths, max_bytes=10_000))
        assert batches == [paths]

    def test_splits_when_total_exceeds_budget(self):
        # Each path is ~50 bytes; 100 paths = 5000 bytes; budget 1500 → 4 batches
        paths = [f"some/deeply/nested/path/page-{i:03d}.md" for i in range(100)]
        batches = list(_chunked_paths(paths, max_bytes=1500))
        assert len(batches) >= 3
        # Round-trip: every path appears exactly once, in original order
        assert [p for batch in batches for p in batch] == paths
        # No batch exceeds the budget (each path counted with +1 overhead)
        for batch in batches:
            assert sum(len(p.encode("utf-8")) + 1 for p in batch) <= 1500

    def test_oversized_single_path_still_yielded(self):
        """A path larger than the budget cannot be split — yield it alone."""
        huge = "x" * 5000
        normal = "small.md"
        batches = list(_chunked_paths([huge, normal], max_bytes=1000))
        # Huge path gets its own batch even though it busts the budget;
        # alternative would be silently dropping it. Better to let git fail loudly.
        assert batches[0] == [huge]
        assert normal in batches[-1]

    def test_unicode_byte_length_respected(self):
        """Multi-byte UTF-8 chars count by encoded byte length, not codepoint count."""
        # Each "ä" is 2 bytes in UTF-8. 50 chars × 2 + ".md" = 103 bytes per path.
        paths = [("ä" * 50) + f"-{i}.md" for i in range(20)]
        batches = list(_chunked_paths(paths, max_bytes=300))
        # 300 / ~108 → ~2 paths per batch → ~10 batches
        assert len(batches) >= 5
        assert [p for batch in batches for p in batch] == paths


class TestSecretConfigPaths:
    def test_matches_anything_under_conex(self):
        assert _is_secret_config_relpath(".conex/config.json") is True
        assert _is_secret_config_relpath(".Conex/config.json") is True
        assert _is_secret_config_relpath(".conex/secrets.yaml") is True
        assert _is_secret_config_relpath(".conex/profiles/prod.json") is True

    def test_does_not_match_similar_names(self):
        assert _is_secret_config_relpath("docs/conex/config.json") is False
        assert _is_secret_config_relpath("docs/.conex-backup/config.json") is False


class TestGitAvailable:
    def test_found(self):
        with patch("confluence_export.git.shutil.which", return_value="/usr/bin/git"):
            assert git_available() is True

    def test_not_found(self):
        with patch("confluence_export.git.shutil.which", return_value=None):
            assert git_available() is False


class TestEnsureRepo:
    def test_already_a_repo(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        assert ensure_repo(tmp_path) is True

    def test_initializes_new_repo(self, tmp_path):
        assert ensure_repo(tmp_path) is True
        assert (tmp_path / ".git").is_dir()

    def test_sets_fallback_identity_on_init(self, tmp_path):
        ensure_repo(tmp_path)
        name = subprocess.run(
            ["git", "config", "user.name"], cwd=tmp_path, capture_output=True, text=True
        )
        assert name.stdout.strip() == "confluence-export"

    def test_does_not_overwrite_existing_identity(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Alice"], cwd=tmp_path, capture_output=True)
        ensure_repo(tmp_path)
        name = subprocess.run(
            ["git", "config", "user.name"], cwd=tmp_path, capture_output=True, text=True
        )
        assert name.stdout.strip() == "Alice"

    def test_inside_parent_repo(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        sub = tmp_path / "sub" / "dir"
        sub.mkdir(parents=True)
        assert ensure_repo(sub) is True
        # Should NOT create a nested .git
        assert not (sub / ".git").exists()


class TestCommitLocalChanges:
    def _init_repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        return tmp_path

    def test_no_changes(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "file.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        assert commit_local_changes(tmp_path) is False

    def test_with_modifications(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "file.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        # Modify tracked file
        (tmp_path / "file.md").write_text("modified")

        assert commit_local_changes(tmp_path) is True

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
        assert "Local changes" in log.stdout

    def test_ignores_untracked_files(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "tracked.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        # Create untracked file only
        (tmp_path / "local-notes.txt").write_text("my notes")

        assert commit_local_changes(tmp_path) is False

    def test_skips_on_fresh_repo(self, tmp_path):
        """No crash or noise on a freshly initialized repo with no commits."""
        self._init_repo(tmp_path)
        assert commit_local_changes(tmp_path) is False


class TestCommitExport:
    def _init_repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        # Need an initial commit for diff --cached to work
        (tmp_path / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        return tmp_path

    def test_commits_written_files(self, tmp_path):
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")
        media = tmp_path / ".media" / "img.png"
        media.parent.mkdir()
        media.write_bytes(b"\x89PNG")

        assert commit_export(tmp_path, [md, media], "TEST") is True

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
        assert "Export Confluence space TEST" in log.stdout

    def test_does_not_commit_other_files(self, tmp_path):
        self._init_repo(tmp_path)
        # A locally created file that should NOT be committed
        (tmp_path / "local-notes.txt").write_text("my notes")
        # An exporter-written file
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        commit_export(tmp_path, [md], "TEST")

        # local-notes.txt should still be untracked
        status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True)
        assert "local-notes.txt" in status.stdout
        assert "??" in status.stdout  # untracked marker

    def test_nothing_to_commit(self, tmp_path):
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        # First export commits the file
        commit_export(tmp_path, [md], "TEST")

        # Re-export with identical content — nothing to commit
        assert commit_export(tmp_path, [md], "TEST") is False

    def test_removes_deleted_page(self, tmp_path):
        """A previously exported page deleted upstream is removed from git."""
        self._init_repo(tmp_path)
        old = tmp_path / "OldPage.md"
        old.write_text("# Old")
        new = tmp_path / "NewPage.md"
        new.write_text("# New")
        subprocess.run(["git", "add", "OldPage.md", "NewPage.md"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Second export: OldPage no longer in Confluence, NewPage updated
        new.write_text("# New v2")
        commit_export(tmp_path, [new], "TEST")

        # OldPage should be removed from git
        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "OldPage.md" not in ls.stdout
        assert "NewPage.md" in ls.stdout

    def test_no_media_export_preserves_committed_media(self, tmp_path):
        """M1: a --no-media FULL export writes no media, so written_files has no
        .media paths. The stale prune must NOT git-rm the committed attachments
        (they are still valid on disk) — preserve_media gates that."""
        self._init_repo(tmp_path)
        page = tmp_path / "Page"
        media = page / ".media"
        media.mkdir(parents=True)
        (page / "Page.md").write_text("# Page")
        (media / "img.png").write_bytes(b"\x89PNG")
        (media / ".versions.json").write_text("{}")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # --no-media full export: only the markdown is (re)written.
        commit_export(
            tmp_path, [page / "Page.md"], "TEST",
            is_full=True, preserve_media=True,
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Page/.media/img.png" in ls, "committed media pruned on --no-media export"

    def test_no_media_prunes_deleted_attachment_when_current_media_is_known(self, tmp_path):
        """When the exporter materializes current no-media attachments, stale
        attachments under that same page are known-deleted and should be pruned."""
        self._init_repo(tmp_path)
        page = tmp_path / "Page"
        media = page / ".media"
        media.mkdir(parents=True)
        md = page / "Page.md"
        kept = media / "kept.png"
        gone = media / "gone.png"
        md.write_text("# Page")
        kept.write_bytes(b"kept")
        gone.write_bytes(b"gone")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        commit_export(
            tmp_path,
            [md, kept],
            "TEST",
            is_full=True,
            preserve_media=True,
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Page/.media/kept.png" in ls
        assert "Page/.media/gone.png" not in ls

    def test_no_media_prunes_all_media_for_empty_attachment_list(self, tmp_path):
        """A page whose current attachment list is empty should prune stale media
        when the exporter marks that media dir as authoritative."""
        self._init_repo(tmp_path)
        page = tmp_path / "Page"
        media = page / ".media"
        media.mkdir(parents=True)
        md = page / "Page.md"
        gone = media / "gone.png"
        md.write_text("# Page")
        gone.write_bytes(b"gone")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        commit_export(
            tmp_path,
            [md],
            "TEST",
            is_full=True,
            preserve_media=True,
            protection=_prot(prune_media=[media], output_dir=tmp_path),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Page/Page.md" in ls
        assert "Page/.media/gone.png" not in ls

    def test_no_media_prunes_media_for_deleted_pages(self, tmp_path):
        """--no-media preserves media only for pages still in the export plan; a
        page deleted upstream must not leave its tracked .media subtree behind."""
        self._init_repo(tmp_path)
        live = tmp_path / "Live"
        live.mkdir()
        (live / "Live.md").write_text("# Live")
        deleted = tmp_path / "Deleted"
        deleted_media = deleted / ".media"
        deleted_media.mkdir(parents=True)
        (deleted / "Deleted.md").write_text("# Deleted")
        (deleted_media / "img.png").write_bytes(b"\x89PNG")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Full --no-media export after Deleted disappeared upstream: only Live
        # is written, so Deleted's markdown and media should both be pruned.
        commit_export(tmp_path, [live / "Live.md"], "TEST", is_full=True, preserve_media=True)

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Live/Live.md" in ls
        assert "Deleted/Deleted.md" not in ls
        assert "Deleted/.media/img.png" not in ls

    def test_no_media_prunes_media_for_deleted_nested_pages(self, tmp_path):
        """A live parent must not preserve a deleted child's media just because
        page directories are nested under the parent path."""
        self._init_repo(tmp_path)
        parent = tmp_path / "Parent"
        child = parent / "DeletedChild"
        child_media = child / ".media"
        child_media.mkdir(parents=True)
        (parent / "Parent.md").write_text("# Parent")
        (child / "DeletedChild.md").write_text("# Deleted child")
        (child_media / "img.png").write_bytes(b"\x89PNG")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        commit_export(tmp_path, [parent / "Parent.md"], "TEST", is_full=True, preserve_media=True)

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Parent/Parent.md" in ls
        assert "Parent/DeletedChild/DeletedChild.md" not in ls
        assert "Parent/DeletedChild/.media/img.png" not in ls

    def test_full_export_with_media_still_prunes_orphaned_media(self, tmp_path):
        """Counterpart: a normal full export (media downloaded) still prunes an
        attachment deleted upstream — preserve_media defaults False."""
        self._init_repo(tmp_path)
        page = tmp_path / "Page"
        media = page / ".media"
        media.mkdir(parents=True)
        (page / "Page.md").write_text("# Page")
        (media / "gone.png").write_bytes(b"\x89PNG")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Full export with media: gone.png is no longer an attachment.
        commit_export(tmp_path, [page / "Page.md"], "TEST", is_full=True)

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Page/.media/gone.png" not in ls

    def test_no_media_prunes_reconcile_deleted_media(self, tmp_path):
        """RF-C: preserve_media must only preserve media STILL ON DISK. A moved
        page's old .media that reconcile already deleted from disk must still be
        pruned, not left tracked-but-deleted (a dirty index)."""
        import shutil

        self._init_repo(tmp_path)
        page = tmp_path / "P"
        media = page / ".media"
        media.mkdir(parents=True)
        (page / "P.md").write_text("# P")
        (media / "img.png").write_bytes(b"\x89PNG")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, capture_output=True)

        # Simulate reconcile having dropped the old .media from disk on a move.
        shutil.rmtree(media)

        # --no-media full export (P.md re-written in place for the test).
        commit_export(tmp_path, [page / "P.md"], "TEST", is_full=True, preserve_media=True)

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "P/.media/img.png" not in ls, "deleted media left tracked under --no-media"
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        assert status.strip() == "", f"dirty tree after export: {status!r}"

    def test_no_media_prunes_tracked_media_file_replaced_by_directory(self, tmp_path):
        self._init_repo(tmp_path)
        page = tmp_path / "Page"
        media = page / ".media"
        media.mkdir(parents=True)
        md = page / "Page.md"
        img = media / "img.png"
        md.write_text("# Page")
        img.write_bytes(b"old")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, capture_output=True)

        img.unlink()
        img.mkdir()
        (img / "nested.txt").write_text("not media")

        commit_export(tmp_path, [md], "TEST", is_full=True, preserve_media=True)

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Page/.media/img.png" not in ls
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        assert status.strip() == "", f"dirty tree after export: {status!r}"

    def test_protected_deletion_restored_so_next_local_commit_keeps_it(self, tmp_path):
        """RF-B: M2 keeps a moved-then-failed page's old copy in HEAD, but
        reconcile deleted it from disk. commit_export must restore the tracked-
        but-deleted file under the protected dir, or the NEXT run's
        commit_local_changes (git add -u) stages the deletion and drops the
        last-good copy."""
        ensure_repo(tmp_path)
        old = tmp_path / "A" / "P"
        old.mkdir(parents=True)
        (old / "P.md").write_text("# P last-good")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, capture_output=True)

        # Moved + failed this run: reconcile deleted A/P/P.md from disk; the page
        # was skipped, so its old dir is protected from the prune.
        (old / "P.md").unlink()
        commit_export(tmp_path, [], "TEST", is_full=True, protection=_prot(page_exact=[old]))

        # The working tree must be clean (the deletion restored), so the next
        # run's pre-export safety commit does not stage it away.
        commit_local_changes(tmp_path)

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        assert "A/P/P.md" in ls, "protection defeated by next-run commit_local_changes"

    def test_page_exact_protection_survives_prune_while_real_deletions_pruned(self, tmp_path):
        """A page-EXACT protected dir (PageExactProtection) keeps its last-good
        committed files while a genuinely upstream-deleted page is still pruned.
        Regression for the #34 skip-then-prune silent deletion."""
        self._init_repo(tmp_path)
        (tmp_path / "A").mkdir()
        (tmp_path / "A" / "A.md").write_text("# A")
        (tmp_path / "P").mkdir()
        (tmp_path / "P" / "P.md").write_text("# P last-good")
        (tmp_path / "Gone").mkdir()
        (tmp_path / "Gone" / "Gone.md").write_text("# upstream-deleted")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Full export: only A is written; P was skipped (protected), Gone is gone.
        commit_export(
            tmp_path, [tmp_path / "A" / "A.md"], "TEST",
            is_full=True, protection=_prot(page_exact=[tmp_path / "P"]),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "P/P.md" in ls  # skipped page's last-good copy preserved
        assert (tmp_path / "P" / "P.md").exists()  # and still on disk
        assert "Gone/Gone.md" not in ls  # genuine upstream deletion still pruned

    def test_page_exact_protection_not_child(self, tmp_path):
        """PageExactProtection is page-EXACT (e.g. an archived page whose precise
        target is known, M1-exact): it preserves the page's own files but NOT a
        nested child that is gone this run — the child is still reconciled by the
        stale prune. (Skipped pages use recursive SubtreeProtection instead.)"""
        self._init_repo(tmp_path)
        parent = tmp_path / "Parent"
        child = parent / "Child"
        child.mkdir(parents=True)
        (parent / "Parent.md").write_text("# Parent")
        (child / "Child.md").write_text("# Child")
        live = tmp_path / "Live"
        live.mkdir()
        (live / "Live.md").write_text("# Live")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        (child / "Child.md").unlink()
        # A live page IS written, so the stale prune runs; Parent is protected
        # page-exactly (its own file kept, its absent child reconciled away).
        commit_export(
            tmp_path, [live / "Live.md"], "TEST",
            is_full=True, protection=_prot(page_exact=[parent]),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Parent/Parent.md" in ls
        assert "Live/Live.md" in ls
        assert "Parent/Child/Child.md" not in ls

    def test_skipped_parent_protects_vanished_child_recursively(self, tmp_path):
        """Fix ②: a page SKIPPED this run is protected RECURSIVELY (it flows through
        SubtreeProtection). A committed child that transiently vanishes under it —
        not written this run, not a known deletion — is preserved, not pruned;
        page-exact protection would wrongly git-rm it (the regression vs main)."""
        self._init_repo(tmp_path)
        parent = tmp_path / "Parent"
        child = parent / "Child"
        child.mkdir(parents=True)
        (parent / "Parent.md").write_text("# Parent last-good")
        (child / "Child.md").write_text("# Child last-good")
        live = tmp_path / "Live"
        live.mkdir()
        (live / "Live.md").write_text("# Live")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Live written; Parent skipped (transient) -> recursive protection; Child
        # is absent from this run's tree (neither written nor an upstream deletion).
        commit_export(
            tmp_path, [live / "Live.md"], "TEST",
            is_full=True, protection=_prot(subtrees=[parent]),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Parent/Parent.md" in ls
        assert "Parent/Child/Child.md" in ls  # child preserved recursively
        assert "Live/Live.md" in ls

    def test_writeless_full_export_prunes_nothing(self, tmp_path):
        """Fix ① / Decision 1 at the git layer: a full export that wrote NO live
        pages must not prune, even with a protected archived dir. A zero-live-write
        run (a v2 space that returned its archived set but zero current pages, or a
        transient empty response) keeps the committed live pages and heals on the
        next successful export."""
        self._init_repo(tmp_path)
        live = tmp_path / "OldLive"
        live.mkdir()
        (live / "OldLive.md").write_text("# committed live page")
        archived = tmp_path / "_archived" / "Arch"
        archived.mkdir(parents=True)
        (archived / "Arch.md").write_text("# archived")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Zero live pages written; only the archived dir is protected (page-exact).
        commit_export(
            tmp_path, [], "TEST",
            is_full=True, protection=_prot(page_exact=[archived]),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "OldLive/OldLive.md" in ls  # NOT pruned despite zero writes
        assert "_archived/Arch/Arch.md" in ls

    def test_subtree_protection_survives_prune_recursively(self, tmp_path):
        """Preserved export subtrees, such as _archived, are intentionally
        omitted wholesale and must be protected recursively."""
        self._init_repo(tmp_path)
        live = tmp_path / "Live"
        archived = tmp_path / "_archived"
        old = archived / "Old"
        old.mkdir(parents=True)
        live.mkdir()
        (live / "Live.md").write_text("# Live")
        (old / "Old.md").write_text("# Old archived")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        commit_export(
            tmp_path,
            [live / "Live.md"],
            "TEST",
            is_full=True,
            protection=_prot(subtrees=[archived]),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "Live/Live.md" in ls
        assert "_archived/Old/Old.md" in ls

    def test_protected_subtree_deletion_restored_without_page_protection(self, tmp_path):
        """A subtree-only protection must also restore tracked deletions so the
        next local-changes commit cannot drop the preserved subtree."""
        ensure_repo(tmp_path)
        live = tmp_path / "Live"
        archived = tmp_path / "_archived"
        old = archived / "Old"
        old.mkdir(parents=True)
        live.mkdir()
        (live / "Live.md").write_text("# Live")
        (old / "Old.md").write_text("# Old archived")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        (old / "Old.md").unlink()
        commit_export(
            tmp_path,
            [live / "Live.md"],
            "TEST",
            is_full=True,
            protection=_prot(subtrees=[archived]),
        )
        commit_local_changes(tmp_path)

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "_archived/Old/Old.md" in ls
        assert (old / "Old.md").exists()

    def test_protected_subtree_restore_does_not_resurrect_conex_secret(self, tmp_path):
        """The restore path must honor the same .conex secret boundary as
        staging and stale-prune."""
        ensure_repo(tmp_path)
        archived = tmp_path / "_archived"
        secret_dir = archived / ".conex"
        secret_dir.mkdir(parents=True)
        secret = secret_dir / "config.json"
        secret.write_text('{"token":"secret"}')
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "tracked secret fixture"], cwd=tmp_path, capture_output=True)

        secret.unlink()
        commit_export(
            tmp_path,
            [],
            "TEST",
            is_full=True,
            protection=_prot(subtrees=[archived]),
        )

        assert not secret.exists()

    def test_protected_symlink_dir_is_not_restored_through_target(self, tmp_path):
        import shutil

        ensure_repo(tmp_path)
        page = tmp_path / "Page"
        page.mkdir()
        (page / "Page.md").write_text("# last good")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)
        shutil.rmtree(page)
        outside = tmp_path / "outside"
        outside.mkdir()
        page.symlink_to(outside, target_is_directory=True)

        commit_export(
            tmp_path,
            [],
            "TEST",
            is_full=True,
            protection=_prot(page_exact=[page]),
        )

        assert not (outside / "Page.md").exists()
        assert page.is_dir()
        assert not page.is_symlink()
        assert (page / "Page.md").read_text() == "# last good"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=True,
        )
        assert status.stdout == ""

    def test_protected_symlink_dir_restores_all_deleted_owned_files(self, tmp_path):
        import shutil

        ensure_repo(tmp_path)
        page = tmp_path / "Page"
        media = page / ".media"
        media.mkdir(parents=True)
        (page / "Page.md").write_text("# last good")
        (media / "img.png").write_bytes(b"PNG")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)
        shutil.rmtree(page)
        outside = tmp_path / "outside"
        outside.mkdir()
        page.symlink_to(outside, target_is_directory=True)

        commit_export(
            tmp_path,
            [],
            "TEST",
            is_full=True,
            protection=_prot(page_exact=[page]),
        )

        assert not (outside / "Page.md").exists()
        assert not (outside / ".media" / "img.png").exists()
        assert not page.is_symlink()
        assert (page / "Page.md").read_text() == "# last good"
        assert (media / "img.png").read_bytes() == b"PNG"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=True,
        )
        assert status.stdout == ""

    def test_protected_symlink_media_dir_is_not_pruned_through_target(self, tmp_path):
        import shutil

        ensure_repo(tmp_path)
        page = tmp_path / "Page"
        media = page / ".media"
        media.mkdir(parents=True)
        (page / "Page.md").write_text("# last good")
        (media / "img.png").write_bytes(b"PNG")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)
        shutil.rmtree(media)
        outside = tmp_path / "outside"
        outside.mkdir()
        media.symlink_to(outside, target_is_directory=True)

        commit_export(
            tmp_path,
            [],
            "TEST",
            is_full=True,
            protection=_prot(page_exact=[page]),
        )

        assert not (outside / "img.png").exists()
        assert not media.is_symlink()
        assert (media / "img.png").read_bytes() == b"PNG"
        ls = subprocess.run(
            ["git", "ls-files"],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "Page/.media/img.png" in ls.stdout
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=True,
        )
        assert status.stdout == ""

    def test_exact_archived_subtree_protection_does_not_restore_moved_out_page(self, tmp_path):
        """Only still-archived page roots should be protected recursively. A page
        that moved out of _archived must be pruned even while a sibling archived
        subtree is preserved."""
        self._init_repo(tmp_path)
        old_live = tmp_path / "_archived" / "OldLive"
        stay = tmp_path / "_archived" / "Stay"
        new_live = tmp_path / "OldLive"
        old_live.mkdir(parents=True)
        stay.mkdir(parents=True)
        (old_live / "OldLive.md").write_text("# old archived copy")
        (stay / "Stay.md").write_text("# still archived")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)
        (old_live / "OldLive.md").unlink()
        new_live.mkdir()
        (new_live / "OldLive.md").write_text("# now live")

        commit_export(
            tmp_path,
            [new_live / "OldLive.md"],
            "TEST",
            is_full=True,
            protection=_prot(subtrees=[stay]),
        )

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "OldLive/OldLive.md" in ls
        assert "_archived/Stay/Stay.md" in ls
        assert "_archived/OldLive/OldLive.md" not in ls

    def test_removes_renamed_page(self, tmp_path):
        """A page renamed upstream: old name removed, new name added."""
        self._init_repo(tmp_path)
        old = tmp_path / "Old-Name.md"
        old.write_text("# Page")
        subprocess.run(["git", "add", "Old-Name.md"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Renamed in Confluence — exporter writes new filename
        renamed = tmp_path / "New-Name.md"
        renamed.write_text("# Page")
        commit_export(tmp_path, [renamed], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "Old-Name.md" not in ls.stdout
        assert "New-Name.md" in ls.stdout

    def test_recased_page_does_not_trigger_stale_prune_warning(self, tmp_path, capsys):
        """On a case-insensitive FS a title whose only change is case must not be
        seen as both written (new case) and stale (old case): that makes git rm
        fail on staged content and leaves a 'survived the prune' warning that only
        clears on the next export (the real-export finding on PR #25)."""
        import pytest

        from confluence_export.git import _fs_is_case_insensitive

        self._init_repo(tmp_path)
        if not _fs_is_case_insensitive(tmp_path):
            pytest.skip("requires a case-insensitive filesystem")

        page = tmp_path / "Foo"
        page.mkdir()
        (page / "Foo.md").write_text("# Foo")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # The title is re-cased: the writer now produces "foo/foo.md" (same files
        # on a case-insensitive FS). A single full export must converge.
        (page / "foo.md").write_text("# Foo v2")
        commit_export(tmp_path, [page / "foo.md"], "TEST")

        assert "survived the prune" not in capsys.readouterr().err
        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert ls.lower().count("foo/foo.md") == 1  # one tracked copy, no case-dup churn

    def test_recase_converges_even_when_core_ignorecase_lies(self, tmp_path, capsys):
        """We must probe the real FS, not trust git's core.ignorecase: that value
        is fixed at init and drifts (a Linux/CI clone opened on a Mac carries
        ignorecase=false). With it forced false on a case-insensitive FS, the
        re-case must STILL converge in one export — the case the unit relies on."""
        import pytest

        from confluence_export.git import _fs_is_case_insensitive

        self._init_repo(tmp_path)
        if not _fs_is_case_insensitive(tmp_path):
            pytest.skip("requires a case-insensitive filesystem")
        # Force git's recorded verdict to disagree with the real (insensitive) FS.
        subprocess.run(["git", "config", "core.ignorecase", "false"], cwd=tmp_path, capture_output=True)

        page = tmp_path / "Bar"
        page.mkdir()
        (page / "Bar.md").write_text("# Bar")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        (page / "bar.md").write_text("# Bar v2")
        commit_export(tmp_path, [page / "bar.md"], "TEST")

        assert "survived the prune" not in capsys.readouterr().err  # FS probe, not config

    def test_moved_media_re_downloaded_at_new_path_follows_as_rename(self, tmp_path):
        """A moved page's .media is disposable (Option B): the reconciler drops it
        at the old path and the write walk re-downloads it at the new path. The
        stale-file prune removes the old tracked copy and git's own rename
        detection links the dropped + re-added attachment — no relocation."""
        self._init_repo(tmp_path)
        old = tmp_path / "A" / "P"
        (old / ".media").mkdir(parents=True)
        (old / "P.md").write_text("# P")
        (old / ".media" / "img.png").write_bytes(b"\x89PNG-bytes-for-rename-detection")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Simulate the export of the moved page: old markdown + media dropped (by
        # the reconciler), both rewritten/re-downloaded fresh at the new path.
        new = tmp_path / "B" / "P"
        (new / ".media").mkdir(parents=True)
        import shutil

        shutil.rmtree(old / ".media")
        (old / "P.md").unlink()
        old.rmdir()
        (new / "P.md").write_text("# P")
        (new / ".media" / "img.png").write_bytes(b"\x89PNG-bytes-for-rename-detection")

        commit_export(tmp_path, [new / "P.md", new / ".media" / "img.png"], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "B/P/.media/img.png" in ls          # tracked at the new path
        assert "A/P/.media/img.png" not in ls       # old tracked copy pruned
        follow = subprocess.run(
            ["git", "log", "--follow", "--oneline", "--", "B/P/.media/img.png"],
            cwd=tmp_path, capture_output=True, text=True,
        ).stdout
        assert len(follow.strip().splitlines()) >= 2  # history follows the rename

    def test_partial_export_does_not_prune(self, tmp_path):
        """is_full=False (filtered/subtree export) must NOT git-rm files outside
        the written subset — otherwise a subtree export wipes the rest of the repo."""
        self._init_repo(tmp_path)
        kept = tmp_path / "Sub" / "Sub.md"
        kept.parent.mkdir()
        kept.write_text("# Sub")
        rest = tmp_path / "Other" / "Other.md"
        rest.parent.mkdir()
        rest.write_text("# Other")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "full export"], cwd=tmp_path, capture_output=True)

        # A filtered re-export writes only the Sub subtree.
        kept.write_text("# Sub v2")
        commit_export(tmp_path, [kept], "TEST", is_full=False)

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "Other/Other.md" in ls.stdout  # untouched by a partial export
        assert (tmp_path / "Other" / "Other.md").exists()

    def test_commit_message_contains_timestamp(self, tmp_path):
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        commit_export(tmp_path, [md], "NB")

        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=tmp_path, capture_output=True, text=True
        )
        msg = log.stdout.strip()
        assert "NB" in msg
        assert "UTC" in msg

    def test_fresh_repo_no_identity_needed(self, tmp_path):
        """ensure_repo sets fallback identity; commit works without global config."""
        ensure_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        assert commit_export(tmp_path, [md], "TEST") is True

        log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
        assert "Export Confluence space TEST" in log.stdout

    def test_metadata_files_survive_repeated_exports(self, tmp_path):
        """Files like .versions.json included in written_files are not removed."""
        self._init_repo(tmp_path)
        media = tmp_path / ".media"
        media.mkdir()
        md = tmp_path / "Page.md"
        md.write_text("# Page")
        manifest = media / ".versions.json"
        manifest.write_text('{"img.png": 1}')

        # First export includes both md and manifest
        commit_export(tmp_path, [md, manifest], "TEST")

        # Second export with same files
        md.write_text("# Page v2")
        manifest.write_text('{"img.png": 2}')
        commit_export(tmp_path, [md, manifest], "TEST")

        # Manifest should still be tracked
        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert ".media/.versions.json" in ls.stdout

    def test_workspace_files_survive_reexport(self, tmp_path):
        """Files inside workspace/ directories are never removed as stale."""
        self._init_repo(tmp_path)
        md = tmp_path / "Page.md"
        md.write_text("# Page")
        ws = tmp_path / ".workspace"
        ws.mkdir()
        script = ws / "prep.py"
        script.write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Re-export: only md is in written_files, workspace file should survive
        md.write_text("# Page v2")
        commit_export(tmp_path, [md], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert ".workspace/prep.py" in ls.stdout
        assert "Page.md" in ls.stdout

    def test_aborts_when_batch_add_fails(self, tmp_path):
        """If a chunked git add batch fails, commit_export returns False without committing.

        Mixing a non-existent path in causes git add to fail with pathspec error,
        which exercises the per-batch early-return guard added with chunking.
        """
        self._init_repo(tmp_path)
        good = tmp_path / "page.md"
        good.write_text("# Page")
        bad = tmp_path / "does-not-exist.md"

        # Capture HEAD before the failed export to confirm no new commit follows
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
        ).stdout.strip()

        assert commit_export(tmp_path, [good, bad], "TEST") is False

        after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
        ).stdout.strip()
        assert before == after  # HEAD unchanged — no commit was made

    def test_commits_many_paths_without_argv_overflow(self, tmp_path):
        """Real-world large-space scenario: thousands of paths in one export.

        Without chunking this raises OSError(7) "Argument list too long" on
        macOS where ARG_MAX is 1 MiB. We synthesize a path list whose joined
        argv comfortably exceeds that limit (~3 MiB) so the chunker is the
        only thing standing between us and the bug.
        """
        self._init_repo(tmp_path)
        # 5000 files with ~100-char names → ~500 KB of paths alone, well past
        # the per-batch budget but still within disk reason for a unit test.
        files = []
        for i in range(5000):
            sub = tmp_path / f"section-{i // 100:03d}"
            sub.mkdir(exist_ok=True)
            f = sub / f"page-with-a-reasonably-long-name-{i:05d}.md"
            f.write_text(f"# Page {i}")
            files.append(f)

        assert commit_export(tmp_path, files, "BIG") is True

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        )
        # All 5000 should be tracked
        assert ls.stdout.count("\n") == 5000

    def test_removes_many_stale_files_without_argv_overflow(self, tmp_path):
        """Same chunking guard for `git rm` when thousands of pages get deleted upstream."""
        self._init_repo(tmp_path)
        # Create + commit 3000 files
        old_files = []
        for i in range(3000):
            f = tmp_path / f"deeply-nested-path-{i:05d}-with-some-extra-bytes.md"
            f.write_text(f"# Page {i}")
            old_files.append(f)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first export"], cwd=tmp_path, capture_output=True)

        # Re-export with only ONE file kept — the other 2999 are stale
        survivor = old_files[0]
        survivor.write_text("# Page 0 v2")
        assert commit_export(tmp_path, [survivor], "TEST") is True

        ls = subprocess.run(
            ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True
        )
        # Only the survivor should remain
        assert ls.stdout.strip() == survivor.name

    def test_removes_stale_file_with_non_ascii_path(self, tmp_path):
        """Paths with non-ASCII characters (umlauts etc.) are handled correctly."""
        self._init_repo(tmp_path)
        umlaut_dir = tmp_path / "Pascal-Spörri"
        umlaut_dir.mkdir()
        old = umlaut_dir / "old.md"
        old.write_text("# Old")
        new = tmp_path / "New.md"
        new.write_text("# New")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, capture_output=True)

        # Second export: only New.md remains
        new.write_text("# New v2")
        commit_export(tmp_path, [new], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "old.md" not in ls.stdout
        assert "New.md" in ls.stdout

    def test_does_not_stage_local_conex_config(self, tmp_path):
        self._init_repo(tmp_path)
        secret = tmp_path / ".conex" / "profiles" / "prod.json"
        secret.parent.mkdir(parents=True)
        secret.write_text('{"token": "secret"}')
        md = tmp_path / "Page.md"
        md.write_text("# Page")

        assert commit_export(tmp_path, [md, secret], "TEST") is True

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert "Page.md" in ls.stdout
        assert ".conex/profiles/prod.json" not in ls.stdout
        status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True)
        assert "?? .conex/" in status.stdout

    def test_commit_local_changes_unstages_tracked_conex_config(self, tmp_path):
        self._init_repo(tmp_path)
        secret = tmp_path / ".conex" / "config.json"
        secret.parent.mkdir()
        secret.write_text('{"token": "old"}')
        md = tmp_path / "Page.md"
        md.write_text("# Page")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "tracked"], cwd=tmp_path, capture_output=True)

        secret.write_text('{"token": "new"}')
        md.write_text("# Page v2")

        assert commit_local_changes(tmp_path) is True

        show = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "Page.md" in show.stdout
        assert ".conex/config.json" not in show.stdout
        status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True)
        assert " M .conex/config.json" in status.stdout

    def test_tracked_conex_config_not_removed_as_stale(self, tmp_path):
        self._init_repo(tmp_path)
        secret = tmp_path / ".conex" / "config.json"
        secret.parent.mkdir()
        secret.write_text('{"token": "old"}')
        old = tmp_path / "Old.md"
        old.write_text("# Old")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, capture_output=True)

        new = tmp_path / "New.md"
        new.write_text("# New")
        commit_export(tmp_path, [new], "TEST")

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True)
        assert ".conex/config.json" in ls.stdout
        assert "Old.md" not in ls.stdout


def _raise_filenotfound(*args, **kwargs):
    raise FileNotFoundError("git missing")


class TestGitDefensiveBranches:
    """Cover the error/fallback branches of the git helpers (no pragma policy in
    this repo — defensive paths are exercised with monkeypatched failures)."""

    def test_is_secret_config_path_outside_output_dir(self, tmp_path):
        from confluence_export.git import _is_secret_config_path

        # A path that is NOT under output_dir hits the ValueError fallback, which
        # inspects the raw path string instead of the relative one.
        assert _is_secret_config_path(tmp_path, Path("/elsewhere/.conex/c.json")) is True
        assert _is_secret_config_path(tmp_path, Path("/elsewhere/page.md")) is False

    def test_run_git_returns_none_when_binary_missing(self, tmp_path, monkeypatch, capsys):
        from confluence_export import git as G

        monkeypatch.setattr(subprocess, "run", _raise_filenotfound)
        assert G._run_git(tmp_path, "status") is None
        assert "git status failed" in capsys.readouterr().err

    def test_ensure_repo_false_when_init_fails(self, tmp_path, monkeypatch):
        from confluence_export import git as G

        real = G._run_git

        def fake(repo, *args, **kwargs):
            if args and args[0] == "init":
                return None
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(G, "_run_git", fake)
        assert G.ensure_repo(tmp_path) is False

    def test_commit_local_changes_false_when_add_fails(self, tmp_path, monkeypatch):
        from confluence_export import git as G

        ensure_repo(tmp_path)
        (tmp_path / "a.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, capture_output=True)

        real = G._run_git

        def fake(repo, *args, **kwargs):
            if args[:2] == ("add", "-u"):
                return None
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(G, "_run_git", fake)
        assert commit_local_changes(tmp_path) is False

    def test_fs_case_probe_false_when_mkstemp_fails(self, tmp_path, monkeypatch):
        import tempfile

        from confluence_export import git as G

        def _boom(*args, **kwargs):
            raise OSError("no temp")

        monkeypatch.setattr(tempfile, "mkstemp", _boom)
        assert G._fs_is_case_insensitive(tmp_path) is False

    def test_fs_case_probe_swallows_unlink_error(self, tmp_path, monkeypatch):
        from confluence_export import git as G

        def _boom(self, *args, **kwargs):
            raise OSError("locked")

        # mkstemp succeeds; the finally's probe.unlink() raises and is swallowed.
        monkeypatch.setattr(Path, "unlink", _boom)
        assert G._fs_is_case_insensitive(tmp_path) in (True, False)

    def test_remove_stale_files_returns_early_when_index_empty(self, tmp_path, monkeypatch):
        from confluence_export import git as G

        ensure_repo(tmp_path)
        (tmp_path / "a.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, capture_output=True)

        real = G._run_git

        class _Empty:
            stdout = ""
            returncode = 0

        def fake(repo, *args, **kwargs):
            if args and args[0] == "ls-files":
                return _Empty()
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(G, "_run_git", fake)
        G._remove_stale_files(tmp_path, [])  # hits the empty-index guard; no raise

    def test_remove_stale_files_warns_when_rm_fails(self, tmp_path, monkeypatch, capsys):
        from confluence_export import git as G

        ensure_repo(tmp_path)
        (tmp_path / "old.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, capture_output=True)

        real = G._run_git

        class _Fail:
            stdout = ""
            returncode = 1

        def fake(repo, *args, **kwargs):
            if args and args[0] == "rm":
                return _Fail()
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(G, "_run_git", fake)
        # old.md is tracked but not in written_files -> stale -> rm attempted ->
        # batch fails -> per-path fallback also fails -> warning.
        G._remove_stale_files(tmp_path, [])
        assert "survived the prune" in capsys.readouterr().err

    def test_remove_stale_files_per_path_fallback_succeeds(self, tmp_path, monkeypatch, capsys):
        # The point of the per-path fallback: a batch `git rm` fails but the
        # individual paths then succeed, so the stale files ARE pruned and no
        # warning is printed. (Previously only the all-fail path was covered.)
        from confluence_export import git as G

        ensure_repo(tmp_path)
        (tmp_path / "old.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, capture_output=True)

        real = G._run_git
        state = {"rm_calls": 0}

        class _Fail:
            stdout = ""
            returncode = 1

        def fake(repo, *args, **kwargs):
            if args and args[0] == "rm":
                state["rm_calls"] += 1
                if state["rm_calls"] == 1:
                    return _Fail()  # fail the first (batch) rm
            return real(repo, *args, **kwargs)  # the per-path rm runs for real

        monkeypatch.setattr(G, "_run_git", fake)
        G._remove_stale_files(tmp_path, [])

        ls = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
        assert "old.md" not in ls  # pruned via the per-path fallback
        assert "survived the prune" not in capsys.readouterr().err

    def test_remove_stale_files_nfd_attachment_matched_as_nfc(self, tmp_path, monkeypatch, capsys):
        # macOS: an attachment title arrives NFD (decomposed); git with
        # core.precomposeunicode stores it NFC, so `git ls-files` returns NFC while
        # written_files still holds the NFD path. They must compare equal (folded
        # to NFC) so the file is not wrongly flagged stale and git-rm'd (issue #15).
        # Deterministic on any OS: ls-files is stubbed to the NFC form.
        import unicodedata

        from confluence_export import git as G

        ensure_repo(tmp_path)
        (tmp_path / "seed.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, capture_output=True)

        name_nfd = unicodedata.normalize("NFD", "Übersicht.txt")
        name_nfc = unicodedata.normalize("NFC", "Übersicht.txt")
        assert name_nfd != name_nfc  # sanity: the two Unicode forms differ

        written = [tmp_path / ".media" / name_nfd]  # written_files in NFD form
        real = G._run_git
        rm_calls = []

        class _LsFiles:
            returncode = 0
            stdout = f".media/{name_nfc}\0"  # git returns NFC

        def fake(repo, *args, **kwargs):
            if args and args[0] == "ls-files":
                return _LsFiles()
            if args and args[0] == "rm":
                rm_calls.append(args)
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(G, "_run_git", fake)
        G._remove_stale_files(tmp_path, written)

        assert rm_calls == []  # NFD written == NFC tracked → not stale → no git rm
        assert "survived the prune" not in capsys.readouterr().err


def test_case_probe_does_not_delete_existing_prefix_files(tmp_path):
    from confluence_export.git import _fs_is_case_insensitive

    user_file = tmp_path / ".conex-case-user-file"
    user_file.write_text("keep")

    _fs_is_case_insensitive(tmp_path)

    assert user_file.read_text() == "keep"
