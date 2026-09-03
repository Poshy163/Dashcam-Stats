"""Bouncing the head unit onto 5 GHz from the access point.

The unit cannot change its own band -- its Wi-Fi firmware owns BSSID selection and this
build's shell has no connect-network, disconnect or roam verb -- so the only lever left is
asking the access point to disassociate it. That makes this code sit directly on the path
to a backup, which is what these tests are really about: it must move the unit when it can,
and it must never cost a copy when it cannot.
"""

from __future__ import annotations

import httpx
import pytest

from app.ingest import band, unifi


@pytest.fixture(autouse=True)
def clean_cooldown():
    band.reset_kick_cooldown_for_tests()
    yield
    band.reset_kick_cooldown_for_tests()


class StubSettings:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get_nowait(self, key, default=None):
        return self.values.get(key, default)


def _config(**overrides) -> unifi.UnifiConfig:
    base = {
        "base_url": "https://192.168.1.1",
        "site": "default",
        "verify_tls": False,
        "api_key": "secret-key",
        "username": None,
        "password": None,
    }
    base.update(overrides)
    return unifi.UnifiConfig(**base)


def _mock(handler, monkeypatch, config: unifi.UnifiConfig) -> list[httpx.Request]:
    """Route every call this module makes through a transport that records requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def load_config():
        return config

    monkeypatch.setattr(unifi, "load_config", load_config)
    monkeypatch.setattr(
        unifi,
        "_build_client",
        lambda cfg: httpx.AsyncClient(base_url=cfg.base_url, transport=httpx.MockTransport(record)),
    )
    return seen


class TestTheKickItself:
    async def test_an_api_key_goes_in_the_header_and_the_command_is_kick_sta(self, monkeypatch):
        import json

        seen = _mock(
            lambda request: httpx.Response(200, json={"meta": {"rc": "ok"}}),
            monkeypatch,
            _config(),
        )

        ok, detail = await unifi.kick_client("40:45:DA:9B:3B:FE")

        assert ok, detail
        assert len(seen) == 1, "an API key needs no login round trip"
        request = seen[0]
        assert request.url.path == "/proxy/network/api/s/default/cmd/stamgr"
        assert request.headers["X-API-KEY"] == "secret-key"
        # Lower-cased: the console matches its client table on the normalised form.
        assert json.loads(request.content) == {
            "cmd": "kick-sta",
            "mac": "40:45:da:9b:3b:fe",
        }

    async def test_username_and_password_logs_in_first_and_carries_the_csrf_token(
        self, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, headers={"x-csrf-token": "tok"}, json={})
            return httpx.Response(200, json={"meta": {"rc": "ok"}})

        seen = _mock(
            handler,
            monkeypatch,
            _config(api_key=None, username="admin", password="pw"),
        )

        ok, _ = await unifi.kick_client("40:45:da:9b:3b:fe")

        assert ok
        assert [r.url.path for r in seen] == [
            "/api/auth/login",
            "/proxy/network/api/s/default/cmd/stamgr",
        ]
        # Without the token UniFi OS refuses every write.
        assert seen[1].headers["X-CSRF-Token"] == "tok"

    async def test_a_refused_sign_in_is_reported_not_raised(self, monkeypatch):
        _mock(
            lambda request: httpx.Response(401, json={}),
            monkeypatch,
            _config(api_key=None, username="admin", password="wrong"),
        )

        ok, detail = await unifi.kick_client("40:45:da:9b:3b:fe")

        assert not ok
        assert "sign-in" in detail

    async def test_an_unreachable_console_is_reported_not_raised(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

        _mock(handler, monkeypatch, _config())

        ok, detail = await unifi.kick_client("40:45:da:9b:3b:fe")

        assert not ok
        assert "Could not reach" in detail

    async def test_the_secret_never_reaches_the_message(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("failed talking to secret-key", request=request)

        _mock(handler, monkeypatch, _config())

        _ok, detail = await unifi.kick_client("40:45:da:9b:3b:fe")

        assert "secret-key" not in detail
        assert "<redacted>" in detail

    async def test_a_nonsense_mac_never_leaves_the_process(self, monkeypatch):
        seen = _mock(lambda request: httpx.Response(200, json={}), monkeypatch, _config())

        ok, detail = await unifi.kick_client("not-a-mac")

        assert not ok
        assert "MAC address" in detail
        assert seen == [], "an unvalidated MAC must not be sent anywhere"

    async def test_nothing_is_attempted_without_a_credential(self, monkeypatch):
        seen = _mock(
            lambda request: httpx.Response(200, json={}),
            monkeypatch,
            _config(api_key=None),
        )

        ok, detail = await unifi.kick_client("40:45:da:9b:3b:fe")

        assert not ok
        assert "not configured" in detail
        assert seen == []


class TestTheGateUsesIt:
    """The half that matters operationally: it must never cost a backup."""

    @pytest.fixture
    def unit(self, monkeypatch):
        state = {
            "frequency": 2472,
            "kicked": 0,
            "kick_result": (True, "asked"),
            "after_kick": 5520,
        }

        async def shell(address, command, **kwargs):
            if "cmd wifi status" in command:
                return (
                    'Wifi is connected to "Ubiquiti Router"\n'
                    'WifiInfo: SSID: "Ubiquiti Router", BSSID: 28:70:4e:d1:07:0f, '
                    "MAC: 40:45:da:9b:3b:fe, "
                    f"Frequency: {state['frequency']}MHz, Net ID: 0"
                )
            return ""

        async def kick_client(mac):
            state["kicked"] += 1
            state["mac"] = mac
            if state["kick_result"][0]:
                state["frequency"] = state["after_kick"]
            return state["kick_result"]

        monkeypatch.setattr(band.adb, "shell", shell)
        monkeypatch.setattr(band.unifi, "kick_client", kick_client)
        monkeypatch.setattr(band, "KICK_POLL_S", 0.0)
        monkeypatch.setattr(band, "KICK_SETTLE_S", 0.05)
        monkeypatch.setattr(band, "SCAN_SETTLE_S", 0.0)
        stub = StubSettings(
            {
                "ingest.wifi_band": "prefer_5ghz",
                "ingest.unifi_enabled": True,
                "ingest.wifi_selection_nudge": False,
            }
        )
        monkeypatch.setattr(band, "get_settings_service", lambda: stub)
        state["settings"] = stub
        return state

    async def test_a_unit_on_24_is_bounced_and_the_transfer_proceeds_on_5ghz(self, unit):
        assert await band.gate("u:5555")

        assert unit["kicked"] == 1
        assert unit["mac"] == "40:45:da:9b:3b:fe", "the MAC is read live from the unit"
        from app.ingest.status import get_status

        assert get_status().wifi_frequency_mhz == 5520

    async def test_a_unit_already_on_5ghz_is_never_bounced(self, unit):
        unit["frequency"] = 5520

        assert await band.gate("u:5555")

        assert unit["kicked"] == 0, "a good link must never be disconnected"

    async def test_a_console_that_refuses_never_holds_up_the_copy(self, unit):
        unit["kick_result"] = (False, "console said no")

        assert await band.gate("u:5555"), "prefer_5ghz must still copy on the slow band"
        assert unit["kicked"] == 1

    async def test_a_unit_that_comes_back_on_24_still_copies(self, unit):
        unit["after_kick"] = 2472

        assert await band.gate("u:5555")
        assert unit["kicked"] == 1

    async def test_the_bounce_is_off_unless_switched_on(self, unit):
        unit["settings"].values["ingest.unifi_enabled"] = False

        assert await band.gate("u:5555")
        assert unit["kicked"] == 0

    async def test_it_is_not_repeated_inside_the_cooldown(self, unit):
        """Under require_5ghz the gate re-runs every thirty seconds for the whole visit; a
        unit that keeps choosing 2.4 must not be disconnected over and over."""
        unit["after_kick"] = 2472

        await band.gate("u:5555")
        await band.gate("u:5555")
        await band.gate("u:5555")

        assert unit["kicked"] == 1


class TestReadingTheUnitsOwnMac:
    def test_the_mac_is_parsed_from_the_status_line(self):
        assert (
            band.parse_mac('WifiInfo: SSID: "x", BSSID: 28:70:4e:d1:07:0f, MAC: 40:45:DA:9B:3B:FE,')
            == "40:45:da:9b:3b:fe"
        )

    def test_an_unreadable_status_yields_no_mac_rather_than_a_guess(self):
        assert band.parse_mac("cmd: Can't find service: wifi") == ""
