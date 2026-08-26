"""Making footage playable in a browser.

The camera records MPEG-TS and no browser can demux it -- Chrome, Firefox and Safari
support MP4, WebM and Ogg only. The stream endpoint was serving `video/mp2t` straight
through, so the player silently refused every recording while the API looked perfectly
healthy.

The fix remuxes to MP4 with `-c copy`: same frames, correct container. These tests cover
the container decision and the cache, and the ffmpeg-backed ones verify the output is
actually playable rather than merely produced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.media.playback import (
    BROWSER_NATIVE_SUFFIXES,
    cache_dir,
    cache_usage,
    ensure_playable,
    is_browser_native,
    prune_cache,
)


class TestContainerDecision:
    @pytest.mark.parametrize("name", ["clip.mp4", "clip.MP4", "clip.webm", "clip.mov"])
    def test_native_containers_are_served_directly(self, name):
        assert is_browser_native(Path(name))

    @pytest.mark.parametrize("name", ["clip.ts", "clip.TS", "clip.mkv", "clip.avi"])
    def test_everything_else_needs_remuxing(self, name):
        """.ts is the whole reason this module exists."""
        assert not is_browser_native(Path(name))

    def test_mpeg_ts_is_not_considered_native(self):
        assert ".ts" not in BROWSER_NATIVE_SUFFIXES
        assert ".mp4" in BROWSER_NATIVE_SUFFIXES

    async def test_native_source_is_not_copied(self, app_config, tmp_path):
        source = tmp_path / "already.mp4"
        source.write_bytes(b"\x00" * 64)
        result = await ensure_playable(source, 1)
        assert result.path == source, "a native container should be served in place"
        assert result.from_cache is False
        assert result.media_type == "video/mp4"


class TestCache:
    def test_cache_lives_under_data_never_in_the_footage_directory(self, app_config):
        """Remuxed copies are derived data; the footage mount is read-only."""
        assert cache_dir().is_relative_to(app_config.data_dir)

    def test_prune_evicts_least_recently_used_first(self, app_config):
        import os
        import time

        directory = cache_dir()
        for index in range(5):
            path = directory / f"{index:08d}.mp4"
            path.write_bytes(b"x" * 1024)
            # Oldest atime/mtime first, so index 0 is the coldest.
            os.utime(path, (time.time() - (5 - index) * 100,) * 2)

        before, count = cache_usage()
        assert count == 5

        removed = prune_cache(limit_bytes=2048)
        assert removed >= 3
        after, remaining = cache_usage()
        assert after <= 2048
        # The most recently watched clip survives.
        assert (directory / "00000004.mp4").exists()
        assert not (directory / "00000000.mp4").exists()

    def test_prune_never_evicts_the_clip_it_was_asked_to_keep(self, app_config):
        """A remux larger than the whole budget used to delete itself on the way out.

        Both callers sweep the moment they finish writing a clip and then hand that path
        straight to a `FileResponse`. When one recording remuxes to more than the configured
        cache size -- a long clip and a small budget, which is a setting away -- the loop
        can never reach the limit, so it deletes everything it has, newest included, and the
        request that paid for the transcode serves a file that is no longer there.
        """
        directory = cache_dir()
        cold = directory / "00000001.mp4"
        cold.write_bytes(b"x" * 1024)
        fresh = directory / "00000002.mp4"
        fresh.write_bytes(b"x" * 4096)

        removed = prune_cache(limit_bytes=512, keep=fresh)

        assert fresh.exists(), "the sweep deleted the clip its caller was about to serve"
        assert not cold.exists(), "the exemption stopped it evicting anything at all"
        assert removed == 1

    def test_prune_without_a_keep_still_evicts_everything_it_must(self, app_config):
        """The exemption is opt-in; a scheduled sweep is still free to take the newest."""
        directory = cache_dir()
        only = directory / "00000003.mp4"
        only.write_bytes(b"x" * 4096)

        assert prune_cache(limit_bytes=512) == 1
        assert not only.exists()

    def test_prune_is_a_noop_under_the_limit(self, app_config):
        (cache_dir() / "00000001.mp4").write_bytes(b"x" * 100)
        assert prune_cache(limit_bytes=10 * 1024) == 0

    def test_prune_survives_a_missing_cache_directory(self, app_config):
        from app.media.playback import clear_cache

        clear_cache()
        assert prune_cache(limit_bytes=1024) == 0


@pytest.mark.needs_ffmpeg
@pytest.mark.slow
class TestRemux:
    async def test_produces_a_playable_mp4(self, app_config, front_clip):
        """The end-to-end claim: a .ts goes in, a browser-playable .mp4 comes out."""
        result = await ensure_playable(front_clip, 42)

        assert result.media_type == "video/mp4"
        assert result.from_cache is True
        assert result.path.exists() and result.path.stat().st_size > 0
        assert result.path.suffix == ".mp4"

    async def test_video_is_copied_not_re_encoded(self, app_config, front_clip):
        """Remuxing must not touch the picture -- codec and resolution stay put."""
        from app.hardware.ffmpeg import probe

        source = await probe(front_clip)
        result = await ensure_playable(front_clip, 43)
        remuxed = await probe(result.path)

        assert remuxed.video_codec == source.video_codec == "h264"
        assert (remuxed.width, remuxed.height) == (source.width, source.height)

    async def test_starts_at_zero(self, app_config, front_clip):
        """Dashcam segments carry a rolling PTS.

        Without genpts the MP4 inherits a large start offset and the player reports a
        duration of hours for a two-minute clip.
        """
        from app.hardware.ffmpeg import probe

        result = await ensure_playable(front_clip, 44)
        remuxed = await probe(result.path)
        assert remuxed.start_time < 1.0
        assert remuxed.duration_s and remuxed.duration_s < 600

    async def test_index_is_at_the_front(self, app_config, front_clip):
        """faststart: playback and seeking must not wait for the whole file."""
        result = await ensure_playable(front_clip, 45)
        head = result.path.read_bytes()[:400_000]
        moov, mdat = head.find(b"moov"), head.find(b"mdat")
        assert moov != -1, "moov atom is not near the start; faststart did not apply"
        assert mdat == -1 or moov < mdat

    async def test_second_request_reuses_the_cache(self, app_config, front_clip, monkeypatch):
        first = await ensure_playable(front_clip, 46)
        size = first.path.stat().st_size

        # Asserting on mtime would be wrong: a cache hit deliberately touches the file so
        # the LRU sweep treats a recently watched clip as hot. Failing the remux itself is
        # the exact assertion -- if it runs a second time, the test fails.
        async def _fail(*_args, **_kwargs):
            raise AssertionError("remuxed a clip that was already cached")

        monkeypatch.setattr("app.media.playback._remux", _fail)

        second = await ensure_playable(front_clip, 46)
        assert second.path == first.path
        assert second.from_cache is True
        assert second.path.stat().st_size == size

    async def test_concurrent_requests_remux_once(self, app_config, front_clip):
        """Two viewers opening the same clip must not race each other."""
        results = await asyncio.gather(*(ensure_playable(front_clip, 47) for _ in range(4)))
        assert len({r.path for r in results}) == 1
        assert all(r.path.exists() for r in results)

    async def test_undecodable_input_degrades_instead_of_raising(self, app_config, damaged_clips):
        """A corrupt file must not 500 the stream endpoint."""
        result = await ensure_playable(damaged_clips["garbage"], 48)
        assert result.path.exists()
