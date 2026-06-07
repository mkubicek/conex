"""Tests for media download helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from confluence_export.diagnostics import WarningCollector
from confluence_export.media import (
    _VERSIONS_FILE,
    _record_download_warning,
    _save_versions,
    available_attachment_names,
    download_attachments,
    ensure_media_dir,
    materialize_existing_attachments,
    migrate_media_dirs,
)
from confluence_export.paths import attachment_identity, plan_attachment_names
from confluence_export.types import Attachment, Version


def _att(
    title="img.png",
    version=1,
    file_size=100,
    download_link="/wiki/download/a1",
    page_id="p1",
    att_id="att1",
):
    return Attachment(
        id=att_id, title=title, file_size=file_size, page_id=page_id,
        download_link=download_link, version=Version(number=version),
    )


def _attachment_paths(result: list[Path]) -> list[Path]:
    """Filter out the manifest from download results."""
    return [p for p in result if p.name != _VERSIONS_FILE]


class TestEnsureMediaDir:
    def test_creates_dir(self, tmp_path):
        page_dir = tmp_path / "page"
        page_dir.mkdir()
        media = ensure_media_dir(page_dir)
        assert media.exists()
        assert media.name == ".media"

    def test_rejects_symlinked_media_dir(self, tmp_path):
        page_dir = tmp_path / "page"
        page_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (page_dir / ".media").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="symlink"):
            ensure_media_dir(page_dir)


class TestDownloadAttachments:
    def test_skip_when_version_matches(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"x" * 100)
        (media_dir / _VERSIONS_FILE).write_text(
            '{"img.png": {"version": 3, "id": "att1", "title": "img.png"}}'
        )

        client = MagicMock()
        result = download_attachments(client, [_att(version=3)], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_not_called()

    def test_redownload_when_version_changes(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"x" * 100)
        (media_dir / _VERSIONS_FILE).write_text('{"img.png": 2}')

        client = MagicMock()
        result = download_attachments(client, [_att(version=3)], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_called_once()

    def test_download_when_no_manifest(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"x" * 100)
        # No .versions.json — file exists but we don't know its version

        client = MagicMock()
        result = download_attachments(client, [_att(version=3)], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_called_once()

    def test_saves_version_after_download(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()

        client = MagicMock()
        download_attachments(client, [_att(title="new.png", version=5)], media_dir)

        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["new.png"]["version"] == 5
        assert versions["new.png"]["id"] == "att1"

    def test_download_uses_umask_mode_for_new_file(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        client = MagicMock()
        client.download_attachment_to_file.side_effect = (
            lambda _path, dest: Path(dest).write_bytes(b"new")
        )
        old_umask = os.umask(0o027)
        try:
            download_attachments(client, [_att(title="new.png", version=1)], media_dir)
        finally:
            os.umask(old_umask)

        assert (media_dir / "new.png").stat().st_mode & 0o777 == 0o640

    def test_download_preserves_existing_file_mode_on_replace(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        target = media_dir / "img.png"
        target.write_bytes(b"old")
        target.chmod(0o640)
        (media_dir / _VERSIONS_FILE).write_text(
            '{"img.png": {"version": 1, "id": "att1", "title": "img.png"}}'
        )
        client = MagicMock()
        client.download_attachment_to_file.side_effect = (
            lambda _path, dest: Path(dest).write_bytes(b"new")
        )

        download_attachments(client, [_att(title="img.png", version=2)], media_dir)

        assert target.read_bytes() == b"new"
        assert target.stat().st_mode & 0o777 == 0o640

    def test_download_computes_default_mode_once_before_worker_pool(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        attachments = [
            _att(title="a.png", att_id="a", version=1),
            _att(title="b.png", att_id="b", version=1),
        ]
        client = MagicMock()
        client.download_attachment_to_file.side_effect = (
            lambda _path, dest: Path(dest).write_bytes(b"new")
        )

        with patch("confluence_export.media.default_file_mode", return_value=0o640) as mode:
            download_attachments(client, attachments, media_dir)

        mode.assert_called_once()
        assert (media_dir / "a.png").stat().st_mode & 0o777 == 0o640
        assert (media_dir / "b.png").stat().st_mode & 0o777 == 0o640

    def test_save_versions_uses_umask_mode_for_new_manifest(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        old_umask = os.umask(0o027)
        try:
            _save_versions(media_dir, {"img.png": {"version": 1}})
        finally:
            os.umask(old_umask)

        assert (media_dir / _VERSIONS_FILE).stat().st_mode & 0o777 == 0o640

    def test_save_versions_preserves_existing_manifest_mode(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        manifest = media_dir / _VERSIONS_FILE
        manifest.write_text("{}")
        manifest.chmod(0o640)

        _save_versions(media_dir, {"img.png": {"version": 1}})

        assert manifest.stat().st_mode & 0o777 == 0o640

    def test_save_versions_is_atomic_on_dump_failure(self, tmp_path, monkeypatch):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        manifest = media_dir / _VERSIONS_FILE
        manifest.write_text('{"img.png": 1}')

        def fail_dump(_versions, file_obj, **_kwargs):
            file_obj.write('{"partial"')
            raise RuntimeError("boom")

        monkeypatch.setattr("confluence_export.media.json.dump", fail_dump)

        with pytest.raises(RuntimeError):
            _save_versions(media_dir, {"img.png": 2})

        assert manifest.read_text() == '{"img.png": 1}'

    def test_downloads_new(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()

        client = MagicMock()
        result = download_attachments(client, [_att()], media_dir)

        assert len(_attachment_paths(result)) == 1
        client.download_attachment_to_file.assert_called_once()

    def test_sanitized_name_collisions_get_unique_destinations(self, tmp_path):
        """Different raw titles can sanitize to the same component; downloads must
        not race onto one path or collapse one attachment in the manifest."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        attachments = [
            _att(title="a/b.png", att_id="att1"),
            _att(title="a-b.png", att_id="att2"),
        ]
        client = MagicMock()

        result = download_attachments(client, attachments, media_dir)

        paths = _attachment_paths(result)
        assert len(paths) == 2
        assert len({p.name for p in paths}) == 2
        destinations = [Path(call.args[1]) for call in client.download_attachment_to_file.call_args_list]
        assert len({p.name for p in destinations}) == 2

    def test_collision_with_legacy_manifest_redownloads_same_version(self, tmp_path):
        """A filename-only legacy manifest cannot prove which colliding
        attachment owns the bytes, so same-version collisions must download."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "a-b.png").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text('{"a-b.png": 1}')
        attachments = [
            _att(title="a/b.png", att_id="att1", version=1),
            _att(title="a-b.png", att_id="att2", version=1),
        ]
        client = MagicMock()

        download_attachments(client, attachments, media_dir)

        assert client.download_attachment_to_file.call_count == 2

    def test_legacy_manifest_with_id_redownloads_before_claiming_owner(self, tmp_path):
        """A legacy integer manifest proves only the title/version, not the
        attachment id. Redownload once before writing owner metadata."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text('{"img.png": 3}')
        client = MagicMock()

        download_attachments(client, [_att(version=3)], media_dir)

        client.download_attachment_to_file.assert_called_once()
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["img.png"]["version"] == 3
        assert versions["img.png"]["id"] == "att1"

    def test_casefold_collision_with_legacy_manifest_redownloads(self, tmp_path):
        """The planner treats case-only names as colliding for cross-platform
        stability, so a legacy manifest cannot prove ownership there either."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "Report.pdf").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text('{"Report.pdf": 1}')
        attachments = [
            _att(title="Report.pdf", att_id="att1", version=1),
            _att(title="report.pdf", att_id="att2", version=1),
        ]
        client = MagicMock()

        download_attachments(client, attachments, media_dir)

        assert client.download_attachment_to_file.call_count == 2

    def test_includes_manifest_in_result(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()

        client = MagicMock()
        result = download_attachments(client, [_att()], media_dir)

        manifest_paths = [p for p in result if p.name == _VERSIONS_FILE]
        assert len(manifest_paths) == 1

    def test_no_download_link(self, tmp_path, capsys):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()

        att = _att(download_link="")
        client = MagicMock()

        result = download_attachments(client, [att], media_dir)
        assert len(_attachment_paths(result)) == 0
        assert "no download link" in capsys.readouterr().err

    def test_download_failure(self, tmp_path, capsys):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()

        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [_att()], media_dir)
        assert len(_attachment_paths(result)) == 0
        assert "failed to download" in capsys.readouterr().err

    def test_failed_redownload_does_not_advance_manifest(self, tmp_path):
        """A failed version bump leaves old bytes on disk, so the manifest must
        keep the old version and retry on the next run."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text('{"img.png": 1}')
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        download_attachments(client, [_att(version=2)], media_dir)

        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["img.png"] == 1

    def test_failed_redownload_returns_existing_file_to_protect_from_prune(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text(
            '{"img.png": {"version": 1, "id": "att1", "title": "img.png"}}'
        )
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [_att(version=2)], media_dir)

        assert media_dir / "img.png" in _attachment_paths(result)
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["img.png"]["version"] == 1

    def test_failed_redownload_does_not_corrupt_existing_file(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        dest = media_dir / "img.png"
        dest.write_bytes(b"old-good")
        (media_dir / _VERSIONS_FILE).write_text(
            '{"img.png": {"version": 1, "id": "att1", "title": "img.png"}}'
        )
        client = MagicMock()

        def partial_write_then_fail(_download_path, output_path):
            Path(output_path).write_bytes(b"partial")
            raise Exception("network error")

        client.download_attachment_to_file.side_effect = partial_write_then_fail

        result = download_attachments(client, [_att(version=2)], media_dir)

        assert dest in _attachment_paths(result)
        assert dest.read_bytes() == b"old-good"

    def test_atomic_download_handles_long_attachment_names(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        title = "a" * 240 + ".png"
        client = MagicMock()

        result = download_attachments(client, [_att(title=title, att_id="long")], media_dir)

        assert media_dir / title in _attachment_paths(result)
        client.download_attachment_to_file.assert_called_once()

    def test_legacy_failed_redownload_preserves_existing_file_without_owner_upgrade(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        dest = media_dir / "img.png"
        dest.write_bytes(b"old-good")
        (media_dir / _VERSIONS_FILE).write_text('{"img.png": 1}')
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [_att(version=2)], media_dir)

        assert dest in _attachment_paths(result)
        assert dest.read_bytes() == b"old-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["img.png"] == 1

    def test_forced_failed_redownload_preserves_existing_manifest(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        dest = media_dir / "img.png"
        dest.write_bytes(b"old-good")
        (media_dir / _VERSIONS_FILE).write_text(
            '{"img.png": {"version": 1, "id": "att1", "title": "img.png"}}'
        )
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [_att(version=2)], media_dir, skip_existing=False)

        assert dest in _attachment_paths(result)
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["img.png"]["version"] == 1
        assert versions["img.png"]["id"] == "att1"

    def test_no_id_collision_manifest_skips_existing_files(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        attachments = [
            _att(title="same.png", att_id="", version=1, download_link="/wiki/a"),
            _att(title="same.png", att_id="", version=1, download_link="/wiki/b"),
        ]
        name_plan = plan_attachment_names(attachments)
        versions = {}
        for att in attachments:
            name = name_plan.for_attachment(att)
            (media_dir / name).write_bytes(b"old")
            versions[name] = {
                "version": 1,
                "id": "",
                "title": att.title,
                "key": attachment_identity(att),
            }
        (media_dir / _VERSIONS_FILE).write_text(json.dumps(versions))
        client = MagicMock()

        result = download_attachments(client, attachments, media_dir)

        assert set(_attachment_paths(result)) == {
            media_dir / name_plan.for_attachment(att) for att in attachments
        }
        client.download_attachment_to_file.assert_not_called()

    def test_no_id_structured_manifest_key_mismatch_redownloads(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "same.png").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "same.png": {
                "version": 1,
                "id": "",
                "title": "same.png",
                "key": "different",
            }
        }))
        client = MagicMock()

        download_attachments(
            client,
            [_att(title="same.png", att_id="", version=1, download_link="/wiki/a")],
            media_dir,
        )

        client.download_attachment_to_file.assert_called_once()

    def test_no_id_collision_failed_redownload_preserves_keyed_file(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        attachments = [
            _att(title="same.png", att_id="", version=2, download_link="/wiki/a"),
            _att(title="same.png", att_id="", version=2, download_link="/wiki/b"),
        ]
        name_plan = plan_attachment_names(attachments)
        versions = {}
        for att in attachments:
            name = name_plan.for_attachment(att)
            (media_dir / name).write_bytes(b"old")
            versions[name] = {
                "version": 1,
                "id": "",
                "title": att.title,
                "key": attachment_identity(att),
            }
        (media_dir / _VERSIONS_FILE).write_text(json.dumps(versions))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, attachments, media_dir)

        assert set(_attachment_paths(result)) == {
            media_dir / name_plan.for_attachment(att) for att in attachments
        }

    def test_no_id_rename_preserves_by_stable_key(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        current = _att(title="new.png", att_id="", version=2, download_link="/wiki/a")
        old_name = "old.png"
        new_name = plan_attachment_names([current]).for_attachment(current)
        (media_dir / old_name).write_bytes(b"old-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "",
                "title": "old.png",
                "key": attachment_identity(current),
            }
        }))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [current], media_dir)

        assert media_dir / new_name in _attachment_paths(result)
        assert (media_dir / new_name).read_bytes() == b"old-good"

    def test_failed_redownload_copies_last_good_file_when_plan_name_changes(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="a/b.png", att_id="att1", version=2)
        new_collision = _att(title="a-b.png", att_id="att2", version=1, download_link="")
        name_plan = plan_attachment_names([moved, new_collision])
        old_name = "a-b.png"
        new_name = name_plan.for_attachment(moved)
        assert new_name != old_name
        (media_dir / old_name).write_bytes(b"old-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": moved.title,
                "key": attachment_identity(moved),
            }
        }))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [moved, new_collision], media_dir)

        assert media_dir / new_name in _attachment_paths(result)
        assert (media_dir / new_name).read_bytes() == b"old-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions[new_name]["id"] == "att1"
        assert versions[new_name]["version"] == 1

    def test_failed_redownload_preserves_swapped_last_good_files(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        a_now_b = _att(title="b.png", att_id="att1", version=2)
        b_now_a = _att(title="a.png", att_id="att2", version=2)
        (media_dir / "a.png").write_bytes(b"att1-last-good")
        (media_dir / "b.png").write_bytes(b"att2-last-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "a.png": {
                "version": 1,
                "id": "att1",
                "title": "a.png",
                "key": attachment_identity(a_now_b),
            },
            "b.png": {
                "version": 1,
                "id": "att2",
                "title": "b.png",
                "key": attachment_identity(b_now_a),
            },
        }))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [a_now_b, b_now_a], media_dir)

        assert set(_attachment_paths(result)) == {
            media_dir / "a.png",
            media_dir / "b.png",
        }
        assert (media_dir / "a.png").read_bytes() == b"att2-last-good"
        assert (media_dir / "b.png").read_bytes() == b"att1-last-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["a.png"]["id"] == "att2"
        assert versions["b.png"]["id"] == "att1"

    def test_failed_redownload_does_not_return_snapshot_when_destination_is_directory(
        self, tmp_path
    ):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=2)
        old_name = "old.png"
        (media_dir / old_name).write_bytes(b"last-good")
        (media_dir / "new.png").mkdir()
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            }
        }))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [moved], media_dir)

        assert _attachment_paths(result) == []
        assert (media_dir / old_name).read_bytes() == b"last-good"

    def test_no_link_owner_manifest_directory_is_not_returned(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").mkdir()
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "img.png": {"version": 1, "id": "att1", "title": "img.png"}
        }))
        client = MagicMock()

        result = download_attachments(
            client,
            [_att(title="img.png", version=2, download_link="")],
            media_dir,
        )

        assert _attachment_paths(result) == []

    def test_failed_redownload_replaces_unmanifested_destination_when_owned_old_exists(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=2)
        old_name = "old.png"
        new_name = plan_attachment_names([moved]).for_attachment(moved)
        (media_dir / old_name).write_bytes(b"last-good")
        (media_dir / new_name).write_bytes(b"stray")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            }
        }))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [moved], media_dir)

        assert media_dir / new_name in _attachment_paths(result)
        assert (media_dir / old_name).read_bytes() == b"last-good"
        assert (media_dir / new_name).read_bytes() == b"last-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions[new_name]["id"] == "att1"

    def test_failed_redownload_keeps_owned_destination_when_previous_copy_exists(
        self, tmp_path
    ):
        """When a download fails but the planned destination already exists and is
        owned by this attachment, the existing destination is returned (protecting
        it from prune) even though a separate last-good copy lives at the old name."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=2, download_link="/wiki/x")
        new_name = plan_attachment_names([moved]).for_attachment(moved)
        old_name = "old.png"
        (media_dir / old_name).write_bytes(b"last-good")
        (media_dir / new_name).write_bytes(b"already-here")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            },
            new_name: {
                "version": 1,
                "id": "att1",
                "title": "new.png",
                "key": attachment_identity(moved),
            },
        }))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [moved], media_dir)

        assert media_dir / new_name in _attachment_paths(result)
        # The download failed, so the pre-existing owned destination is kept as-is.
        assert (media_dir / new_name).read_bytes() == b"already-here"

    def test_failed_redownload_ignores_symlinked_old_manifest_entry(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        (media_dir / "old.png").symlink_to(outside)
        moved = _att(title="new.png", att_id="att1", version=2)
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "old.png": {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            }
        }))
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [moved], media_dir)

        assert media_dir / "new.png" not in _attachment_paths(result)
        assert not (media_dir / "new.png").exists()
        assert outside.read_text() == "secret"

    def test_no_link_attachment_returns_existing_file_when_owner_matches(self, tmp_path, capsys):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text(
            '{"img.png": {"version": 3, "id": "att1", "title": "img.png"}}'
        )
        client = MagicMock()

        result = download_attachments(client, [_att(version=4, download_link="")], media_dir)

        assert media_dir / "img.png" in _attachment_paths(result)
        assert "no download link" in capsys.readouterr().err
        client.download_attachment_to_file.assert_not_called()

    def test_no_link_unmanifested_exact_file_is_returned_to_protect_prune(
        self, tmp_path
    ):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"present")
        client = MagicMock()

        result = download_attachments(client, [_att(download_link="")], media_dir)

        assert media_dir / "img.png" in _attachment_paths(result)

    @pytest.mark.parametrize("old_name", [".hidden.png", "-dash.png"])
    def test_no_link_legacy_unsafe_manifest_name_copied_to_safe_name(
        self, tmp_path, old_name
    ):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        current = _att(title=old_name, att_id="att1", version=1, download_link="")
        new_name = plan_attachment_names([current]).for_attachment(current)
        assert new_name != old_name
        (media_dir / old_name).write_bytes(b"last-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": old_name,
                "key": attachment_identity(current),
            }
        }))
        client = MagicMock()

        result = download_attachments(client, [current], media_dir)

        assert media_dir / new_name in _attachment_paths(result)
        assert (media_dir / new_name).read_bytes() == b"last-good"

    @pytest.mark.parametrize("old_name", [".hidden.png", "-dash.png"])
    def test_no_link_legacy_unsafe_integer_manifest_name_copied_to_safe_name(
        self, tmp_path, old_name
    ):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        current = _att(title=old_name, att_id="att1", version=1, download_link="")
        new_name = plan_attachment_names([current]).for_attachment(current)
        assert new_name != old_name
        (media_dir / old_name).write_bytes(b"last-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({old_name: 1}))
        client = MagicMock()

        result = download_attachments(client, [current], media_dir)

        assert media_dir / new_name in _attachment_paths(result)
        assert (media_dir / new_name).read_bytes() == b"last-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions[new_name] == 1

    def test_case_only_owned_file_kept_without_unlink_before_download(
        self, tmp_path, monkeypatch
    ):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        old_name = "Report.pdf"
        new_name = "report.pdf"
        old_path = media_dir / old_name
        dest = media_dir / new_name
        old_path.write_bytes(b"old-good")
        try:
            os.link(old_path, dest)
        except FileExistsError:
            pass
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {"version": 1, "id": "att1", "title": old_name}
        }))
        current = _att(
            title=new_name,
            att_id="att1",
            version=1,
            download_link="",
        )
        real_unlink = Path.unlink

        def fail_if_dest_unlinked(path, *args, **kwargs):
            if path == dest:
                raise AssertionError("case-only destination was unlinked")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_if_dest_unlinked)
        client = MagicMock()

        result = download_attachments(client, [current], media_dir)

        assert dest in _attachment_paths(result)
        assert dest.read_bytes() == b"old-good"
        client.download_attachment_to_file.assert_not_called()

    def test_failed_download_unmanifested_exact_file_is_returned_to_protect_prune(
        self, tmp_path
    ):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").write_bytes(b"present")
        client = MagicMock()
        client.download_attachment_to_file.side_effect = Exception("network error")

        result = download_attachments(client, [_att(version=2)], media_dir)

        assert media_dir / "img.png" in _attachment_paths(result)

    def test_legacy_no_link_attachment_preserves_non_colliding_file(self, tmp_path):
        """When there is no download link, a non-colliding legacy entry is the
        only last-good copy we can preserve."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "a-b.png").write_bytes(b"old")
        (media_dir / _VERSIONS_FILE).write_text('{"a-b.png": 1}')
        client = MagicMock()

        result = download_attachments(
            client,
            [_att(title="a-b.png", att_id="att2", version=1, download_link="")],
            media_dir,
        )

        assert media_dir / "a-b.png" in _attachment_paths(result)

    def test_uses_v1_content_attachment_endpoint(self, tmp_path):
        """The download path is built from page_id + att_id, ignoring the
        legacy `_links.download` URL — the v1 REST endpoint works on both
        the site URL and the OAuth gateway URL used for scoped tokens.
        """
        media_dir = tmp_path / ".media"
        media_dir.mkdir()

        att = _att(page_id="2172649563", att_id="att2173468762")
        client = MagicMock()

        download_attachments(client, [att], media_dir)
        path = client.download_attachment_to_file.call_args[0][0]
        assert path == "/wiki/rest/api/content/2172649563/child/attachment/att2173468762/download"

    def test_falls_back_to_download_link_for_legacy_cache(self, tmp_path):
        """Cached attachments written before page_id was tracked still work
        via the legacy `_links.download` path."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()

        att = _att(
            page_id="",
            att_id="",
            download_link="/download/attachments/123/img.png?version=1",
        )
        client = MagicMock()

        download_attachments(client, [att], media_dir)
        path = client.download_attachment_to_file.call_args[0][0]
        assert path.startswith("/wiki/download/attachments/")

    def test_no_media_materialization_removes_wrong_owner_destination(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="a/b.png", att_id="att1", version=1)
        moved.created_at = "2025-01-01"
        current_owner = _att(title="a-b.png", att_id="att2", version=1, download_link="")
        current_owner.created_at = "2024-01-01"
        name_plan = plan_attachment_names([moved, current_owner])
        old_name = "a-b.png"
        moved_name = name_plan.for_attachment(moved)
        assert name_plan.for_attachment(current_owner) == old_name
        assert moved_name != old_name
        (media_dir / old_name).write_bytes(b"att1-last-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": moved.title,
                "key": attachment_identity(moved),
            }
        }))

        written = materialize_existing_attachments([moved, current_owner], media_dir)

        assert media_dir / moved_name in written
        assert (media_dir / moved_name).read_bytes() == b"att1-last-good"
        assert not (media_dir / old_name).exists()
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert moved_name in versions
        assert old_name not in versions

    def test_no_media_materialization_preserves_swapped_last_good_files(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        a_now_b = _att(title="b.png", att_id="att1", version=2)
        b_now_a = _att(title="a.png", att_id="att2", version=2)
        (media_dir / "a.png").write_bytes(b"att1-last-good")
        (media_dir / "b.png").write_bytes(b"att2-last-good")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "a.png": {
                "version": 1,
                "id": "att1",
                "title": "a.png",
                "key": attachment_identity(a_now_b),
            },
            "b.png": {
                "version": 1,
                "id": "att2",
                "title": "b.png",
                "key": attachment_identity(b_now_a),
            },
        }))

        written = materialize_existing_attachments([a_now_b, b_now_a], media_dir)

        assert {media_dir / "a.png", media_dir / "b.png"} <= set(written)
        assert (media_dir / "a.png").read_bytes() == b"att2-last-good"
        assert (media_dir / "b.png").read_bytes() == b"att1-last-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions["a.png"]["id"] == "att2"
        assert versions["b.png"]["id"] == "att1"

    def test_no_media_materialization_replaces_unmanifested_destination_when_owned_old_exists(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=1)
        old_name = "old.png"
        new_name = plan_attachment_names([moved]).for_attachment(moved)
        (media_dir / old_name).write_bytes(b"last-good")
        (media_dir / new_name).write_bytes(b"stray")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            }
        }))

        written = materialize_existing_attachments([moved], media_dir)

        assert media_dir / new_name in written
        assert (media_dir / old_name).read_bytes() == b"last-good"
        assert (media_dir / new_name).read_bytes() == b"last-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions[new_name]["id"] == "att1"

    def test_no_media_case_only_owned_file_kept_without_unlink(
        self, tmp_path, monkeypatch
    ):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        old_name = "Report.pdf"
        new_name = "report.pdf"
        old_path = media_dir / old_name
        dest = media_dir / new_name
        old_path.write_bytes(b"old-good")
        try:
            os.link(old_path, dest)
        except FileExistsError:
            pass
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {"version": 1, "id": "att1", "title": old_name}
        }))
        moved = _att(title=new_name, att_id="att1", version=1)
        real_unlink = Path.unlink

        def fail_if_dest_unlinked(path, *args, **kwargs):
            if path == dest:
                raise AssertionError("case-only destination was unlinked")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_if_dest_unlinked)

        written = materialize_existing_attachments([moved], media_dir)

        assert dest in written
        assert dest.read_bytes() == b"old-good"

    def test_no_media_materialization_does_not_return_manifest_directory(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").mkdir()
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "img.png": {"version": 1, "id": "att1", "title": "img.png"}
        }))

        written = materialize_existing_attachments(
            [_att(att_id="att1", version=1)],
            media_dir,
        )

        assert media_dir / "img.png" not in written

    def test_available_names_rejects_unmanifested_when_owned_old_exists(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=1)
        (media_dir / "old.png").write_bytes(b"last-good")
        (media_dir / "new.png").write_bytes(b"stray")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "old.png": {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            }
        }))

        available = available_attachment_names([moved], media_dir)

        assert "new.png" not in available

    def test_available_names_rejects_manifest_directory(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        (media_dir / "img.png").mkdir()
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "img.png": {"version": 1, "id": "att1", "title": "img.png"}
        }))

        available = available_attachment_names([_att(att_id="att1", version=1)], media_dir)

        assert "img.png" not in available

    def test_no_media_materialization_replaces_wrong_owner_destination(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=1)
        old_name = "old.png"
        new_name = plan_attachment_names([moved]).for_attachment(moved)
        (media_dir / old_name).write_bytes(b"last-good")
        (media_dir / new_name).write_bytes(b"wrong-owner")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            },
            new_name: {
                "version": 1,
                "id": "other",
                "title": "new.png",
                "key": "other-id",
            },
        }))

        written = materialize_existing_attachments([moved], media_dir)

        assert media_dir / new_name in written
        assert (media_dir / new_name).read_bytes() == b"last-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert versions[new_name]["id"] == "att1"

    def test_no_media_materialization_keeps_last_good_when_wrong_owner_dest_unremovable(
        self, tmp_path
    ):
        """If a wrong-owner entry occupies the planned name as a non-empty directory
        it cannot be unlinked; materialization must not destroy the last-good file
        at the old name nor drop its manifest entry."""
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=1)
        new_name = plan_attachment_names([moved]).for_attachment(moved)
        old_name = "old.png"
        (media_dir / old_name).write_bytes(b"last-good")
        blocker = media_dir / new_name
        blocker.mkdir()
        (blocker / "inner").write_bytes(b"x")  # non-empty -> unlink() raises OSError
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            old_name: {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            },
            new_name: {"version": 1, "id": "other", "title": "new.png", "key": "other"},
        }))

        written = materialize_existing_attachments([moved], media_dir)

        # The directory blocks materialization; nothing new is written there.
        assert (media_dir / new_name).is_dir()
        assert media_dir / new_name not in written
        # Last-good file and its manifest entry survive untouched.
        assert (media_dir / old_name).read_bytes() == b"last-good"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert old_name in versions

    def test_no_media_materialization_preserves_unrelated_manifest_entries(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        current = _att(title="current.png", att_id="current", version=1)
        (media_dir / "current.png").write_bytes(b"current")
        (media_dir / "other.png").write_bytes(b"other")
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "current.png": {"version": 1, "id": "current", "title": "current.png"},
            "other.png": {"version": 1, "id": "other", "title": "other.png"},
        }))

        materialize_existing_attachments([current], media_dir)

        assert (media_dir / "other.png").read_bytes() == b"other"
        versions = json.loads((media_dir / _VERSIONS_FILE).read_text())
        assert "other.png" in versions

    def test_no_media_materialization_ignores_symlinked_old_manifest_entry(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        (media_dir / "old.png").symlink_to(outside)
        moved = _att(title="new.png", att_id="att1", version=1)
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            "old.png": {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            }
        }))

        written = materialize_existing_attachments([moved], media_dir)

        assert media_dir / "new.png" not in written
        assert not (media_dir / "new.png").exists()

    def test_no_media_materialization_ignores_overlong_manifest_entry(self, tmp_path):
        media_dir = tmp_path / ".media"
        media_dir.mkdir()
        moved = _att(title="new.png", att_id="att1", version=1)
        (media_dir / _VERSIONS_FILE).write_text(json.dumps({
            ("x" * 300) + ".png": {
                "version": 1,
                "id": "att1",
                "title": "old.png",
                "key": attachment_identity(moved),
            }
        }))

        written = materialize_existing_attachments([moved], media_dir)

        assert media_dir / "new.png" not in written
        assert not (media_dir / "new.png").exists()


class TestMigrateMediaDirs:
    def test_renames_media_with_versions_file(self, tmp_path):
        """media/ containing .versions.json is renamed to .media/."""
        page_dir = tmp_path / "Page"
        page_dir.mkdir()
        media = page_dir / "media"
        media.mkdir()
        (media / "img.png").write_bytes(b"PNG")
        (media / _VERSIONS_FILE).write_text('{"img.png": 1}')

        renamed = migrate_media_dirs(tmp_path)

        assert len(renamed) == 1
        assert not (page_dir / "media").exists()
        new_media = page_dir / ".media"
        assert new_media.is_dir()
        assert (new_media / "img.png").read_bytes() == b"PNG"
        assert (new_media / _VERSIONS_FILE).exists()

    def test_skips_dir_without_versions_file(self, tmp_path):
        """media/ without .versions.json is left alone (e.g., a page titled 'media')."""
        page_dir = tmp_path / "media"
        page_dir.mkdir()
        (page_dir / "media.md").write_text("# media")

        renamed = migrate_media_dirs(tmp_path)

        assert len(renamed) == 0
        assert (tmp_path / "media").is_dir()

    def test_idempotent_when_dotmedia_exists(self, tmp_path):
        """Skips if .media/ already exists as sibling."""
        page_dir = tmp_path / "Page"
        page_dir.mkdir()
        media = page_dir / "media"
        media.mkdir()
        (media / _VERSIONS_FILE).write_text("{}")
        dotmedia = page_dir / ".media"
        dotmedia.mkdir()

        renamed = migrate_media_dirs(tmp_path)

        assert len(renamed) == 0
        assert (page_dir / "media").is_dir()
        assert (page_dir / ".media").is_dir()

    def test_nested_tree(self, tmp_path):
        """Migrates multiple media/ dirs at different tree levels."""
        for name in ["Parent", "Parent/Child"]:
            d = tmp_path / name
            d.mkdir(parents=True, exist_ok=True)
            media = d / "media"
            media.mkdir()
            (media / _VERSIONS_FILE).write_text("{}")
            (media / "att.png").write_bytes(b"x")

        renamed = migrate_media_dirs(tmp_path)

        assert len(renamed) == 2
        assert (tmp_path / "Parent" / ".media" / "att.png").exists()
        assert (tmp_path / "Parent" / "Child" / ".media" / "att.png").exists()

    def test_skips_file_named_media(self, tmp_path):
        """A file named 'media' (not a directory) is ignored."""
        page_dir = tmp_path / "Page"
        page_dir.mkdir()
        (page_dir / "media").write_text("just a file")

        renamed = migrate_media_dirs(tmp_path)

        assert len(renamed) == 0
        assert (page_dir / "media").is_file()

    def test_empty_tree(self, tmp_path):
        """No media/ dirs at all — returns empty list."""
        assert migrate_media_dirs(tmp_path) == []

    def test_does_not_descend_internal_dirs(self, tmp_path):
        """P1: never descend .git/.media/.workspace/.conex.

        A legacy ``media/`` planted inside one of those is neither migrated nor
        even visited — both a correctness guard (we must not touch git internals
        or already-migrated trees) and the pruning that keeps the walk cheap.
        """
        internal = (".git", ".media", ".workspace", ".conex")
        for parent in internal:
            d = tmp_path / parent / "media"
            d.mkdir(parents=True)
            (d / _VERSIONS_FILE).write_text("{}")

        renamed = migrate_media_dirs(tmp_path)

        assert renamed == []
        for parent in internal:
            assert (tmp_path / parent / "media").is_dir()  # untouched


def test_load_versions_returns_empty_on_corrupt_json(tmp_path):
    from confluence_export.media import _VERSIONS_FILE, _load_versions

    (tmp_path / _VERSIONS_FILE).write_text("{ not valid json")
    assert _load_versions(tmp_path) == {}


def test_load_versions_returns_empty_on_invalid_utf8(tmp_path):
    from confluence_export.media import _VERSIONS_FILE, _load_versions

    (tmp_path / _VERSIONS_FILE).write_bytes(b"\xff\xfe{")
    assert _load_versions(tmp_path) == {}


def test_load_versions_returns_empty_on_non_object_json(tmp_path):
    from confluence_export.media import _VERSIONS_FILE, _load_versions

    (tmp_path / _VERSIONS_FILE).write_text("[]")
    assert _load_versions(tmp_path) == {}


class TestResolveManifestEntry:
    """_resolve_manifest_entry maps unsafe/legacy manifest names to a contained
    path or refuses them, so a hostile manifest can never read/write outside."""

    def test_rejects_name_with_separator(self, tmp_path):
        from confluence_export.media import _resolve_manifest_entry

        with pytest.raises(ValueError, match="unsafe manifest path component"):
            _resolve_manifest_entry(tmp_path, "a/b.png")

    def test_rejects_name_equal_to_manifest_file(self, tmp_path):
        from confluence_export.media import _VERSIONS_FILE, _resolve_manifest_entry

        with pytest.raises(ValueError, match="unsafe manifest path component"):
            _resolve_manifest_entry(tmp_path, _VERSIONS_FILE)

    def test_rejects_legacy_symlinked_name(self, tmp_path):
        from confluence_export.media import _resolve_manifest_entry

        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        # ".hidden.png" is not a safe component, so resolution takes the symlink
        # branch; pointing at a real file proves the symlink guard, not escape.
        (tmp_path / ".hidden.png").symlink_to(outside)

        with pytest.raises(ValueError, match="escapes base directory|symlink"):
            _resolve_manifest_entry(tmp_path, ".hidden.png")
        assert outside.read_text() == "secret"

    def test_legacy_name_resolve_oserror_becomes_value_error(self, tmp_path, monkeypatch):
        from confluence_export.media import _resolve_manifest_entry

        name = ".hidden.png"
        leaf = tmp_path / name
        leaf.write_bytes(b"x")  # real file (not a symlink), forces the resolve() path
        real_resolve = Path.resolve

        def resolve_with_race(self, *args, **kwargs):
            if self == leaf:
                raise OSError("resolve race")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_with_race)

        with pytest.raises(ValueError, match="unusable manifest path component"):
            _resolve_manifest_entry(tmp_path, name)


class TestManifestPredicates:
    """Branch behavior of the small manifest-classification helpers that decide
    whether an on-disk byte stream may be trusted for the current attachment."""

    def test_manifest_version_zero_for_non_int_dict_version(self):
        from confluence_export.media import _manifest_version

        assert _manifest_version({"version": "not-an-int"}) == 0

    def test_version_matches_legacy_non_colliding_empty_id(self):
        from confluence_export.media import _version_matches

        att = _att(title="img.png", version=1, att_id="")
        # Legacy integer record proves only title+version; with no id and no
        # name collision it is allowed to vouch for the bytes.
        assert _version_matches({"img.png": 1}, "img.png", att, set()) is True

    def test_version_matches_legacy_rejected_when_colliding(self):
        from confluence_export.media import _version_matches
        from confluence_export.paths import nfc_casefold, safe_attachment_name

        att = _att(title="img.png", version=1, att_id="")
        collisions = {nfc_casefold(safe_attachment_name("img.png"))}
        assert _version_matches({"img.png": 1}, "img.png", att, collisions) is False

    def test_legacy_can_preserve_rejects_name_mismatch(self):
        from confluence_export.media import _legacy_record_can_preserve

        att = _att(title="img.png", version=1, att_id="")
        # Disk filename differs from the attachment's safe name -> cannot vouch.
        assert _legacy_record_can_preserve(2, "other.png", att, set()) is False

    def test_legacy_can_migrate_rejects_zero_version(self):
        from confluence_export.media import _legacy_record_can_migrate

        att = _att(title="old.png", version=1, att_id="")
        assert _legacy_record_can_migrate(0, "old.png", "old.png", att, set()) is False

    def test_legacy_can_migrate_rejects_target_name_mismatch(self):
        from confluence_export.media import _legacy_record_can_migrate

        att = _att(title="old.png", version=1, att_id="")
        # old_name matches the title, but the planned target name does not match
        # the safe name -> not a clean migration.
        assert (
            _legacy_record_can_migrate(2, "old.png", "elsewhere.png", att, set())
            is False
        )

    def test_unmanifested_can_preserve_rejects_name_mismatch(self):
        from confluence_export.media import _unmanifested_file_can_preserve

        att = _att(title="img.png", version=1)
        assert _unmanifested_file_can_preserve(None, "other.png", att, set()) is False


class TestCopyAndSameFileHelpers:
    def test_copy_media_file_cleans_tmp_and_reraises_on_copy_failure(
        self, tmp_path, monkeypatch
    ):
        from confluence_export.media import _copy_media_file

        src = tmp_path / "src.bin"
        src.write_bytes(b"data")
        dest = tmp_path / "dest.bin"

        def boom(_src, _dst):
            raise RuntimeError("copy failed")

        monkeypatch.setattr("confluence_export.media.shutil.copy2", boom)

        with pytest.raises(RuntimeError, match="copy failed"):
            _copy_media_file(src, dest)

        assert not dest.exists()
        # No half-written .copy-*.tmp scratch file is left behind.
        assert [p.name for p in tmp_path.iterdir()] == ["src.bin"]

    def test_same_existing_file_returns_false_on_oserror(self, tmp_path, monkeypatch):
        from confluence_export.media import _same_existing_file

        a = tmp_path / "a"
        a.write_bytes(b"1")
        b = tmp_path / "b"
        b.write_bytes(b"2")

        def raising_samefile(self, _other):
            raise OSError("stat race")

        monkeypatch.setattr(Path, "samefile", raising_samefile)

        assert _same_existing_file(a, b) is False


class TestPathTraversal:
    """S1: an untrusted attachment title must never write outside .media/."""

    @staticmethod
    def _writing_client():
        client = MagicMock()

        def _write(download_path, dest):
            p = Path(dest)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"payload")
            return len(b"payload")

        client.download_attachment_to_file.side_effect = _write
        return client

    def _written_files(self, media_dir):
        return [
            p
            for p in media_dir.rglob("*")
            if p.is_file() and p.name != _VERSIONS_FILE
        ]

    def _assert_contained(self, media_dir):
        root = media_dir.resolve()
        for p in self._written_files(media_dir):
            assert root == p.resolve().parent or root in p.resolve().parents, p

    def test_relative_traversal_title_cannot_escape(self, tmp_path):
        # media_dir is 3 levels deep: export/page/.media
        media_dir = tmp_path / "export" / "page" / ".media"
        media_dir.mkdir(parents=True)
        sentinel = tmp_path / "outside.txt"  # export/page/.media/../../../outside.txt

        client = self._writing_client()
        download_attachments(client, [_att(title="../../../outside.txt")], media_dir)

        assert not sentinel.exists(), "attachment escaped .media via ../ traversal"
        self._assert_contained(media_dir)

    def test_absolute_title_cannot_escape(self, tmp_path):
        media_dir = tmp_path / "export" / "page" / ".media"
        media_dir.mkdir(parents=True)
        sentinel = tmp_path / "abs_evil.txt"

        client = self._writing_client()
        download_attachments(client, [_att(title=str(sentinel))], media_dir)

        assert not sentinel.exists(), "absolute-path title escaped .media"
        self._assert_contained(media_dir)

    def test_control_char_title_contained(self, tmp_path):
        media_dir = tmp_path / "export" / "page" / ".media"
        media_dir.mkdir(parents=True)

        client = self._writing_client()
        download_attachments(client, [_att(title="a\x00b/../c.png")], media_dir)

        self._assert_contained(media_dir)

    def test_benign_names_preserved_for_link_compatibility(self, tmp_path):
        # Common safe names must keep their EXACT on-disk name so existing
        # markdown links (built from the raw ri:filename) still resolve.
        media_dir = tmp_path / "export" / "page" / ".media"
        media_dir.mkdir(parents=True)

        client = self._writing_client()
        for title in ("report.pdf", "My Diagram (v2).png", "data.final.xlsx"):
            download_attachments(client, [_att(title=title, att_id=title)], media_dir)
            assert (media_dir / title).exists(), f"benign name changed: {title}"


class TestRecordDownloadWarning:
    def test_404_reads_as_unavailable_content_not_an_error(self, capsys):
        wc = WarningCollector()
        exc = requests.exceptions.HTTPError()
        exc.response = MagicMock(status_code=404)

        _record_download_warning(_att(title="gone.png"), exc, wc)

        err = capsys.readouterr().err
        assert "metadata exists but its binary is unavailable (HTTP 404)" in err
        assert wc.counts() == {"attachment unavailable (HTTP 404)": 1}

    def test_non_404_http_error_is_a_plain_failure(self, capsys):
        wc = WarningCollector()
        exc = requests.exceptions.HTTPError()
        exc.response = MagicMock(status_code=500)

        _record_download_warning(_att(title="boom.png"), exc, wc)

        assert "failed to download" in capsys.readouterr().err
        assert wc.counts() == {"attachment download failed": 1}

    def test_timeout_is_its_own_category(self, capsys):
        wc = WarningCollector()
        _record_download_warning(
            _att(title="slow.png"), requests.exceptions.ReadTimeout("slow"), wc
        )

        assert "timed out downloading slow.png" in capsys.readouterr().err
        assert wc.counts() == {"attachment download timeout": 1}

    def test_collector_is_optional(self, capsys):
        # The helper still prints when no collector is supplied.
        _record_download_warning(_att(title="x.png"), ValueError("nope"), None)
        assert "failed to download x.png" in capsys.readouterr().err
