"""The CarPlay timing sampler: how it is armed, and how its lines are read back.

No device here. Arming is asserted as the commands it sends; parsing is exercised on lines
copied verbatim from the unit, including the heartbeat that must not become a sample.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.ingest import adb, carplay_timing

VIDEO_LINE = (
    "acc=1 phone=1 load=21.83 soc=74.6 zlink_cpu=55 rx_kbit=1631 "
    "sta=RSSI:-33/Frequency:5520MHz/ ap=5180 | layer=#104 fps=23.4 med=35.3 p95=70.6 "
    "max=88.2 late=38% n=126 period=17.5"
)
HEARTBEAT_LINE = (
    "acc=1 phone=0 load=15.95 soc=71.6 zlink_cpu=na rx_kbit=na "
    "sta=RSSI:-35/Frequency:5520MHz/ ap=5180 | no phone on hotspot"
)


class _Settings:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get_nowait(self, key: str, default=None):
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def clean():
    carplay_timing.reset_for_tests()
    yield
    carplay_timing.reset_for_tests()


class TestParsing:
    def test_a_video_line_becomes_numbers(self):
        at = datetime(2026, 9, 3, 7, 30, tzinfo=UTC)
        sample = carplay_timing.parse_sample(at, VIDEO_LINE)
        assert sample is not None
        assert sample["occurred_at"] == at
        assert sample["acc_on"] is True
        assert sample["phone_attached"] is True
        assert sample["load"] == 21.83
        assert sample["soc_c"] == 74.6
        assert sample["zlink_cpu_pct"] == 55
        assert sample["hotspot_rx_kbit"] == 1631
        assert sample["sta_mhz"] == 5520
        assert sample["sta_rssi"] == -33
        assert sample["ap_mhz"] == 5180
        assert sample["layer"] == "#104"
        assert sample["fps"] == 23.4
        assert sample["median_ms"] == 35.3
        assert sample["p95_ms"] == 70.6
        assert sample["max_ms"] == 88.2
        assert sample["late_pct"] == 38
        assert sample["frames"] == 126
        assert sample["period_ms"] == 17.5

    def test_the_coexistence_columns_are_read(self):
        """Recorded so the BLE-versus-CarPlay question can be settled rather than argued.

        The logger polls the car over Bluetooth while driving and Bluetooth shares this
        unit's one radio with the hotspot CarPlay runs over. Nothing in the samples
        collected so far can test that: the logger polls whenever the engine runs and
        CarPlay samples exist only when the engine runs, so there was no contrast.
        """
        line = VIDEO_LINE.replace("rx_kbit=1631", "rx_kbit=1631 ap_drops=42 obd_cpu=9 bt=1")

        sample = carplay_timing.parse_sample(datetime(2026, 9, 4, 9, 0, tzinfo=UTC), line)

        assert sample["ap_drops"] == 42.0
        assert sample["obd_cpu_pct"] == 9.0
        assert sample["bluetooth_on"] is True

    def test_an_unreadable_coexistence_column_is_none_not_zero(self):
        """``na`` means the read failed. Zero would read as a healthy link."""
        line = VIDEO_LINE.replace("rx_kbit=1631", "rx_kbit=1631 ap_drops=na obd_cpu=na bt=na")

        sample = carplay_timing.parse_sample(datetime(2026, 9, 4, 9, 0, tzinfo=UTC), line)

        assert sample["ap_drops"] is None
        assert sample["obd_cpu_pct"] is None
        assert sample["bluetooth_on"] is None

    def test_a_line_from_before_the_new_columns_still_parses(self):
        """Samples already in the database predate these fields."""
        sample = carplay_timing.parse_sample(datetime(2026, 9, 4, 9, 0, tzinfo=UTC), VIDEO_LINE)

        assert sample is not None and sample["fps"] is not None
        assert sample["ap_drops"] is None and sample["obd_cpu_pct"] is None

    def test_a_heartbeat_is_not_a_sample(self):
        assert carplay_timing.parse_sample(datetime.now(UTC), HEARTBEAT_LINE) is None

    def test_unreadable_fields_become_none_not_zero(self):
        line = VIDEO_LINE.replace("zlink_cpu=55", "zlink_cpu=na").replace("ap=5180", "ap=na")
        sample = carplay_timing.parse_sample(datetime.now(UTC), line)
        assert sample is not None
        assert sample["zlink_cpu_pct"] is None
        assert sample["ap_mhz"] is None

    def test_anything_else_is_ignored(self):
        assert carplay_timing.parse_sample(datetime.now(UTC), "garbage") is None
        assert carplay_timing.parse_sample(datetime.now(UTC), "a=1 | layer=#1 fps=x") is None


class TestSummarising:
    def test_buckets_average_the_rates_and_keep_the_worst_of_the_rest(self):
        t0 = datetime(2026, 9, 3, 7, 30, 5, tzinfo=UTC)
        samples = [
            carplay_timing.parse_sample(t0, VIDEO_LINE),
            carplay_timing.parse_sample(
                t0 + timedelta(seconds=15),
                VIDEO_LINE.replace("fps=23.4", "fps=26.2")
                .replace("late=38%", "late=24%")
                .replace("soc=74.6", "soc=75.1")
                .replace("max=88.2", "max=70.7"),
            ),
            carplay_timing.parse_sample(t0 + timedelta(seconds=61), VIDEO_LINE),
        ]
        minutes = carplay_timing.summarise([s for s in samples if s])
        assert [m["samples"] for m in minutes] == [2, 1]
        first = minutes[0]
        assert first["bucket_start"] == datetime(2026, 9, 3, 7, 30, tzinfo=UTC)
        assert first["fps"] == pytest.approx((23.4 + 26.2) / 2)
        assert first["late_pct"] == pytest.approx((38 + 24) / 2)
        assert first["soc_c"] == 75.1, "worst temperature in the bucket"
        assert first["max_ms"] == 88.2, "worst interval in the bucket"
        assert first["sta_mhz"] == 5520 and first["ap_mhz"] == 5180

    def test_two_surfaces_in_one_minute_stay_apart(self):
        """Pooling them produced a figure that described neither.

        Measured over 234 live samples: in the same minute one surface ran a 35 ms cadence
        and the other 53 ms, and the mean of the two was a rate no surface ever achieved.
        """
        t0 = datetime(2026, 9, 3, 7, 30, 5, tzinfo=UTC)
        fast = carplay_timing.parse_sample(t0, VIDEO_LINE)
        slow = carplay_timing.parse_sample(
            t0 + timedelta(seconds=5),
            VIDEO_LINE.replace("layer=#104", "layer=#100")
            .replace("fps=23.4", "fps=16.8")
            .replace("med=35.3", "med=53.0"),
        )

        minutes = carplay_timing.summarise([s for s in (fast, slow) if s])

        assert len(minutes) == 2, "one row per surface, not one blended row"
        by_layer = {m["layer"]: m for m in minutes}
        assert by_layer["#104"]["fps"] == pytest.approx(23.4)
        assert by_layer["#100"]["fps"] == pytest.approx(16.8)
        # Both describe the same minute; neither is an average of the other.
        assert {m["bucket_start"] for m in minutes} == {datetime(2026, 9, 3, 7, 30, tzinfo=UTC)}

    def test_the_surface_index_survives_into_the_bucket(self):
        """`#N` is reassigned between sessions, so the position is what can be followed."""
        t0 = datetime(2026, 9, 3, 7, 30, 5, tzinfo=UTC)
        sample = carplay_timing.parse_sample(
            t0, VIDEO_LINE.replace("layer=#104", "layer=#104 idx=2")
        )

        assert sample["layer_index"] == 2
        assert carplay_timing.summarise([sample])[0]["layer_index"] == 2


class TestArming:
    async def test_the_script_is_deployed_by_base64_and_started_detached(self, monkeypatch):
        shells: list[str] = []

        async def fake_shell(address, command, **kwargs):
            shells.append(command)
            return ""

        monkeypatch.setattr(adb, "shell", fake_shell)
        monkeypatch.setattr(
            carplay_timing,
            "get_settings_service",
            lambda: _Settings({carplay_timing.ENABLED_KEY: True, carplay_timing.INTERVAL_KEY: 20}),
        )

        assert await carplay_timing.arm("u:5555")

        assert any("base64 -d" in c and carplay_timing.REMOTE_SCRIPT in c for c in shells)
        launch = next(c for c in shells if "setsid" in c)
        # The interval from the setting, then the logcat priority the collector keeps.
        assert f"sh {carplay_timing.REMOTE_SCRIPT} 20 e </dev/null >/dev/null 2>&1 &" in launch

    async def test_the_shipped_script_is_what_gets_deployed(self):
        text = carplay_timing.script()
        assert text.startswith("#!/system/bin/sh")
        assert "SurfaceFlinger --latency" in text
        assert carplay_timing.TAG in text
        assert "\r" not in text, "CRLF would break the unit's shell"

    async def test_nothing_is_sent_when_switched_off(self, monkeypatch):
        shells: list[str] = []

        async def fake_shell(address, command, **kwargs):
            shells.append(command)
            return ""

        monkeypatch.setattr(adb, "shell", fake_shell)
        monkeypatch.setattr(
            carplay_timing,
            "get_settings_service",
            lambda: _Settings({carplay_timing.ENABLED_KEY: False}),
        )

        assert not await carplay_timing.arm("u:5555")
        assert shells == []

    def test_the_interval_is_clamped_to_something_the_unit_can_afford(self, monkeypatch):
        monkeypatch.setattr(
            carplay_timing,
            "get_settings_service",
            lambda: _Settings({carplay_timing.INTERVAL_KEY: 1}),
        )
        assert carplay_timing.interval_s() == carplay_timing.MIN_INTERVAL_S
        monkeypatch.setattr(
            carplay_timing,
            "get_settings_service",
            lambda: _Settings({carplay_timing.INTERVAL_KEY: 9999}),
        )
        assert carplay_timing.interval_s() == carplay_timing.MAX_INTERVAL_S

    async def test_presence_arms_once_per_debounce(self, monkeypatch):
        calls: list[str] = []

        async def fake_arm(address):
            calls.append(address)
            return True

        monkeypatch.setattr(carplay_timing, "arm", fake_arm)
        monkeypatch.setattr(
            carplay_timing,
            "get_settings_service",
            lambda: _Settings({carplay_timing.ENABLED_KEY: True}),
        )
        carplay_timing.reset_for_tests()
        try:
            carplay_timing.on_unit_present("u:5555")
            carplay_timing.on_unit_present("u:5555")
            # Let the scheduled task actually run before judging what it did.
            for _ in range(10):
                await asyncio.sleep(0)
            if carplay_timing._tasks:
                await asyncio.gather(*list(carplay_timing._tasks), return_exceptions=True)
            assert calls == ["u:5555"], "the second call inside the debounce must not re-arm"
        finally:
            await carplay_timing.shutdown()
            carplay_timing.reset_for_tests()

    async def test_presence_arms_even_when_system_uptime_is_under_debounce_window(
        self, monkeypatch
    ):
        calls: list[str] = []

        async def fake_arm(address):
            calls.append(address)
            return True

        # When runner / VM uptime is under ARM_DEBOUNCE_S (e.g. 12s on fresh container).
        monkeypatch.setattr("time.monotonic", lambda: 12.0)
        monkeypatch.setattr(carplay_timing, "arm", fake_arm)
        monkeypatch.setattr(
            carplay_timing,
            "get_settings_service",
            lambda: _Settings({carplay_timing.ENABLED_KEY: True}),
        )
        carplay_timing.reset_for_tests()
        try:
            carplay_timing.on_unit_present("u:5555")
            for _ in range(10):
                await asyncio.sleep(0)
            if carplay_timing._tasks:
                await asyncio.gather(*list(carplay_timing._tasks), return_exceptions=True)
            assert calls == ["u:5555"]
        finally:
            await carplay_timing.shutdown()
            carplay_timing.reset_for_tests()
