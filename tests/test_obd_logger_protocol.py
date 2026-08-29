from __future__ import annotations

from collections.abc import Iterable

import pytest

from app.obd.elm import (
    EcuUnavailable,
    Elm327Session,
    ElmCommandTimeout,
    ElmError,
    ElmSessionTainted,
    UnsafeObdCommand,
    command_is_safe,
)
from app.obd.protocol import (
    decode_pid,
    extract_pid_payload,
    response_frames,
)


class FakeChannel:
    def __init__(self, responses: Iterable[Iterable[bytes]]) -> None:
        self.responses = [list(item) for item in responses]
        self.notification = None
        self.writes: list[bytes] = []
        self.disconnects = 0

    async def connect(self, notification) -> None:
        self.notification = notification

    async def write_without_response(self, payload: bytes) -> None:
        self.writes.append(payload)
        for fragment in self.responses.pop(0) if self.responses else ():
            self.notification(fragment)

    async def disconnect(self) -> None:
        self.disconnects += 1


@pytest.mark.asyncio
async def test_obd_logger_fragmented_prompt_and_uppercase_command() -> None:
    channel = FakeChannel([(b"at", b"rv\r12.", b"7V\r", b">")])
    session = Elm327Session(channel, command_delay_s=0)
    await session.connect()
    response = await session.command("at rv")
    assert channel.writes == [b"ATRV\r"]
    assert "12.7V" in response


@pytest.mark.asyncio
async def test_obd_logger_missing_prompt_taints_until_disconnect() -> None:
    channel = FakeChannel([(b"48 6B",), (b"12.7V\r>",)])
    session = Elm327Session(channel, command_delay_s=0)
    await session.connect()
    with pytest.raises(ElmCommandTimeout):
        await session.command("010C", timeout_s=0.01)
    with pytest.raises(ElmSessionTainted):
        await session.command("ATRV")
    await session.disconnect()
    await session.connect()
    assert "12.7V" in await session.command("ATRV")
    assert channel.disconnects == 1


@pytest.mark.asyncio
async def test_obd_logger_checksum_valid_0100_is_required_and_taints_failure() -> None:
    good = b"0100\rSEARCHING...\r48\r6B\r10\r41\r00\rBE\r1F\rB8\r11\rAA\r>"
    extension = b"0120\r48 6B 10 41 20 80 00 00 00 A4\r>"
    channel = FakeChannel([(b"OK\r>",), (good,), (extension,)])
    session = Elm327Session(channel, command_delay_s=0)
    await session.connect()
    supported = await session.prove_ecu_online()
    assert session.ecu_source == 0x10
    assert {0x01, 0x0C, 0x20, 0x21}.issubset(supported)

    bad = FakeChannel([(b"OK\r>",), (good.replace(b"AA", b"AB"),)])
    rejected = Elm327Session(bad, command_delay_s=0)
    await rejected.connect()
    with pytest.raises(EcuUnavailable, match="checksum"):
        await rejected.prove_ecu_online()
    assert rejected.tainted is True


def test_obd_logger_multiple_byte_tokenized_frames_and_invalid_checksum() -> None:
    first = bytes.fromhex("48 6B 10 41 0C 0D 7A 97")
    second_body = bytes.fromhex("48 6B 10 41 0D 2A")
    second = bytes((*second_body, sum(second_body) & 0xFF))
    response = "\r".join(f"{value:02X}" for value in first + second) + "\r>"
    assert response_frames(response) == [first, second]
    payload = extract_pid_payload(
        response,
        0x0C,
        2,
        strict_iso=True,
        expected_source=0x10,
    )
    assert payload == bytes.fromhex("0D7A")
    assert decode_pid(0x0C, payload or b"") == {"engine_rpm": 862.5}
    assert (
        extract_pid_payload(
            "48 6B 10 41 0C 0D 7A 98\r>",
            0x0C,
            2,
            strict_iso=True,
            expected_source=0x10,
        )
        is None
    )
    wrong_header = bytes.fromhex("49 6B 10 41 0C 0D 7A")
    wrong_header = bytes((*wrong_header, sum(wrong_header) & 0xFF))
    assert (
        extract_pid_payload(
            wrong_header.hex(" "),
            0x0C,
            2,
            strict_iso=True,
            expected_source=0x10,
        )
        is None
    )


@pytest.mark.asyncio
async def test_obd_logger_allowlist_has_no_write_or_monitor_commands() -> None:
    unsafe = ("04", "08", "ATMA", "ATMR", "ATCV1234", "ATPP01SVFF", "AT@3", "ATSD", "ATLP")
    assert all(not command_is_safe(command) for command in unsafe)
    assert command_is_safe("010C") is True
    assert command_is_safe("020000") is True
    assert command_is_safe("020200") is True
    session = Elm327Session(FakeChannel([]), command_delay_s=0)
    with pytest.raises(UnsafeObdCommand):
        await session.command("04")


@pytest.mark.asyncio
async def test_obd_logger_malformed_pid_is_not_conflated_with_explicit_no_data() -> None:
    malformed = Elm327Session(
        FakeChannel([(b"48 6B 10 41 0C 0D 7A 98\r>",)]),
        command_delay_s=0,
    )
    await malformed.connect()
    malformed.ecu_source = 0x10
    with pytest.raises(ElmError, match="header/length/source/checksum"):
        await malformed.query_pid(0x0C)
    assert malformed.tainted is True

    absent = Elm327Session(FakeChannel([(b"NO DATA\r>",)]), command_delay_s=0)
    await absent.connect()
    absent.ecu_source = 0x10
    assert await absent.query_pid(0x0C) == {}
    assert absent.tainted is False
