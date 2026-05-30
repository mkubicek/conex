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
