"""Path containment and filesystem introspection.

These are the primitives the retention guards and the media endpoints are built on, so a
hole here is either "the web UI can read arbitrary host files" or "retention deleted
something it should not have". They are tested directly rather than only through the
callers.
"""

from __future__ import annotations

import os
import sys

import pytest

from app.core.paths import (
    PathTraversalError,
    directory_size,
    is_within,
    is_writable,
    safe_join,
    same_filesystem,
)


class TestSafeJoin:
    def test_joins_a_normal_relative_path(self, tmp_path):
        (tmp_path / "sub").mkdir()
        target = tmp_path / "sub" / "clip.ts"
        target.write_bytes(b"x")
        assert safe_join(tmp_path, "sub/clip.ts") == target.resolve()

    @pytest.mark.parametrize(
        "attack",
        [
            "../secret",
            "../../etc/passwd",
            "sub/../../outside",
            "a/b/../../../escape",
        ],
    )
    def test_parent_traversal_is_refused(self, tmp_path, attack):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, attack)

    @pytest.mark.parametrize("attack", ["/etc/passwd", "\\windows\\system32"])
    def test_absolute_paths_are_refused(self, tmp_path, attack):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, attack)

    def test_drive_qualified_paths_are_refused(self, tmp_path):
        # Both C:\x and the drive-relative C:x form; the latter resolves against a
        # per-drive working directory and would otherwise slip past a naive join.
        for attack in ("C:\\Windows", "C:Windows"):
            with pytest.raises(PathTraversalError):
                safe_join(tmp_path, attack)

    def test_nul_byte_is_refused(self, tmp_path):
        with pytest.raises(PathTraversalError):
            safe_join(tmp_path, "clip\x00.ts")

    def test_redundant_separators_and_dots_are_harmless(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "clip.ts").write_bytes(b"x")
        assert safe_join(tmp_path, "./sub//clip.ts") == (tmp_path / "sub" / "clip.ts").resolve()

    def test_empty_parts_resolve_to_the_root(self, tmp_path):
        assert safe_join(tmp_path) == tmp_path.resolve()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlink_escaping_the_root_is_refused(self, tmp_path):
        # The dangerous case: a link *inside* the share pointing outside it. String
        # inspection alone would pass this, which is why resolution is symlink-aware.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("sensitive")
        root = tmp_path / "footage"
        root.mkdir()
        (root / "link").symlink_to(outside)

        with pytest.raises(PathTraversalError):
            safe_join(root, "link/secret")

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlink_staying_inside_the_root_is_allowed(self, tmp_path):
        root = tmp_path / "footage"
        (root / "real").mkdir(parents=True)
        (root / "real" / "clip.ts").write_bytes(b"x")
        (root / "link").symlink_to(root / "real")
        assert safe_join(root, "link/clip.ts").exists()


class TestIsWithin:
    def test_identifies_containment(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        assert is_within(tmp_path, tmp_path / "a" / "b")
        assert is_within(tmp_path, tmp_path)

    def test_sibling_with_a_shared_prefix_is_not_contained(self, tmp_path):
        # "/data" must not be judged to contain "/database" -- a plain startswith would.
        root = tmp_path / "data"
        sibling = tmp_path / "database"
        root.mkdir()
        sibling.mkdir()
        assert not is_within(root, sibling)

    def test_unrelated_paths_are_not_contained(self, tmp_path):
        (tmp_path / "one").mkdir()
        (tmp_path / "two").mkdir()
        assert not is_within(tmp_path / "one", tmp_path / "two")


class TestDirectorySize:
    def test_sums_matching_files_recursively(self, tmp_path):
        (tmp_path / "nested").mkdir()
        (tmp_path / "a.ts").write_bytes(b"x" * 100)
        (tmp_path / "nested" / "b.ts").write_bytes(b"x" * 250)
        (tmp_path / "note.txt").write_bytes(b"x" * 999)

        total, count = directory_size(tmp_path, [".ts"])
        assert (total, count) == (350, 2)

    def test_without_a_filter_counts_everything(self, tmp_path):
        (tmp_path / "a.ts").write_bytes(b"x" * 10)
        (tmp_path / "b.txt").write_bytes(b"x" * 20)
        total, count = directory_size(tmp_path, None)
        assert (total, count) == (30, 2)

    def test_extension_matching_is_case_insensitive_and_dot_agnostic(self, tmp_path):
        (tmp_path / "a.TS").write_bytes(b"x" * 5)
        assert directory_size(tmp_path, ["ts"])[1] == 1

    def test_empty_directory_is_zero_not_an_error(self, tmp_path):
        assert directory_size(tmp_path, [".ts"]) == (0, 0)

    def test_missing_directory_reports_zero_rather_than_raising(self, tmp_path):
        # Retention asks this question about a share that may not be mounted; it must get
        # an answer it can act on, not an exception.
        assert directory_size(tmp_path / "absent", [".ts"]) == (0, 0)

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlinks_are_not_followed_or_double_counted(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.ts").write_bytes(b"x" * 100)
        (tmp_path / "link").symlink_to(real)

        total, count = directory_size(tmp_path, [".ts"])
        assert (total, count) == (100, 1), "a symlinked directory was counted twice"


class TestWritability:
    def test_detects_a_writable_directory(self, tmp_path):
        assert is_writable(tmp_path) is True

    def test_probe_leaves_nothing_behind(self, tmp_path):
        before = set(os.listdir(tmp_path))
        is_writable(tmp_path)
        assert set(os.listdir(tmp_path)) == before

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_detects_a_read_only_directory(self, tmp_path):
        # The recommended deployment mounts footage read-only, and retention must DETECT
        # that rather than assume it. os.access() would answer from the permission bits
        # and get this wrong on a read-only mount, which is why the probe writes.
        target = tmp_path / "ro"
        target.mkdir()
        target.chmod(0o555)
        try:
            if os.geteuid() == 0:
                pytest.skip("root ignores permission bits")
            assert is_writable(target) is False
        finally:
            target.chmod(0o755)

    def test_missing_directory_is_not_writable(self, tmp_path):
        assert is_writable(tmp_path / "absent" / "deeper") is False


class TestSameFilesystem:
    def test_two_paths_on_one_filesystem(self, tmp_path):
        (tmp_path / "a").mkdir()
        assert same_filesystem(tmp_path, tmp_path / "a")

    def test_unstattable_path_is_false_rather_than_raising(self, tmp_path):
        assert same_filesystem(tmp_path, tmp_path / "absent") is False
