"""Safe prompt-delimited ELM327 session used as the Android implementation's spec."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from .protocol import (
    cleaned_response_lines,
    decode_pid,
    extract_iso_source,
    extract_pid_payload,
    normalize_command,
    parse_voltage,
    response_has_no_data,
    response_transport_error,
)

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = NOTIFY_UUID

CONNECT_TIMEOUT_S = 20.0
COMMAND_TIMEOUT_S = 6.0
PROTOCOL_SEARCH_TIMEOUT_S = 35.0
RESET_TIMEOUT_S = 12.0
COMMAND_DELAY_S = 0.1
MAX_RESPONSE_BYTES = 8192

INITIALIZATION_COMMANDS = (
    "ATZ",
    "ATI",
    "ATD",
    "ATD0",
    "ATE0",
    "ATL0",
    "ATH1",
    "ATSP0",
    "ATE0",
    "ATH1",
    "ATM0",
    "ATS0",
    "ATAT1",
    "ATAL",
    "ATST64",
)

PID_LENGTHS = {
    0x01: 4,
    0x03: 2,
    0x04: 1,
    0x05: 1,
    0x06: 1,
    0x07: 1,
    0x0C: 2,
    0x0D: 1,
    0x0E: 1,
    0x0F: 1,
    0x10: 2,
    0x11: 1,
    0x13: 1,
    0x14: 2,
    0x15: 2,
    0x1C: 1,
    0x20: 4,
    0x21: 2,
}

_SAFE_AT = frozenset(INITIALIZATION_COMMANDS) | {
    "ATRV",
    "AT@1",
    "AT@2",
    "ATIGN",
    "ATDP",
    "ATDPN",
    "ATPC",
}
_SAFE_DIAGNOSTICS = frozenset({"03", "07", "0A", "0900", "0902", "0904", "0906", "090A"})
_FREEZE_FRAME = re.compile(r"^02(?:00|01|02|03|04|05|06|07|0C|0D|0E|0F|10|11|14|15)00$")


class ElmError(RuntimeError):
    """Base ELM session failure."""


class ElmCommandTimeout(ElmError):
    """No ELM prompt arrived before the command deadline."""


class ElmSessionTainted(ElmError):
    """A failed command makes this BLE session unsafe to reuse."""


class UnsafeObdCommand(ElmError):
    """The caller tried to leave the read-only safety boundary."""


class EcuUnavailable(ElmError):
    """No checksum-valid ECU reply was received."""


Notification = Callable[[bytes], None]


class BleChannel(Protocol):
    """Smallest BLE surface the ELM session needs."""

    async def connect(self, notification: Notification) -> None: ...

    async def write_without_response(self, payload: bytes) -> None: ...

    async def disconnect(self) -> None: ...


def command_is_safe(command: str) -> bool:
    """Allow only proven read-only adapter and vehicle requests."""
    command = normalize_command(command)
    if command in _SAFE_AT or command in _SAFE_DIAGNOSTICS or _FREEZE_FRAME.fullmatch(command):
        return True
    if len(command) == 4 and command.startswith("01"):
        try:
            return int(command[2:], 16) in PID_LENGTHS or command == "0100"
        except ValueError:
            return False
    return False


class Elm327Session:
    """Serialize commands, assemble fragments to ``>``, and taint uncertain sessions."""

    def __init__(
        self,
        channel: BleChannel,
        *,
        command_delay_s: float = COMMAND_DELAY_S,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.channel = channel
        self.command_delay_s = command_delay_s
        self._sleep = sleeper
        self._buffer = bytearray()
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_completed = 0.0
        self._connected = False
        self.tainted = False
        self.ecu_source: int | None = None
        self.protocol: str | None = None
        self.adapter_version: str | None = None

    def feed_notification(self, fragment: bytes) -> None:
        self._buffer.extend(fragment)
        if len(self._buffer) > MAX_RESPONSE_BYTES or b">" in self._buffer:
            self._ready.set()

    async def connect(self) -> None:
        await asyncio.wait_for(
            self.channel.connect(self.feed_notification), timeout=CONNECT_TIMEOUT_S
        )
        self._connected = True
        self.tainted = False

    async def command(self, command: str, *, timeout_s: float = COMMAND_TIMEOUT_S) -> str:
        command = normalize_command(command)
        if not command_is_safe(command):
            raise UnsafeObdCommand(f"command is outside the read-only allowlist: {command}")
        async with self._lock:
            if not self._connected:
                raise ElmError("BLE channel is not connected")
            if self.tainted:
                raise ElmSessionTainted("disconnect and run a fresh ATZ before reuse")
            self._buffer.clear()
            self._ready.clear()
            loop = asyncio.get_running_loop()
            delay = self.command_delay_s - (loop.time() - self._last_completed)
            if delay > 0:
                await self._sleep(delay)
            try:
                await self.channel.write_without_response(f"{command}\r".encode("ascii"))
                await asyncio.wait_for(self._ready.wait(), timeout=timeout_s)
            except TimeoutError as exc:
                self.tainted = True
                raise ElmCommandTimeout(f"timed out waiting for {command}") from exc
            except Exception:
                self.tainted = True
                raise
            raw = bytes(self._buffer)
            self._last_completed = loop.time()
            if len(raw) > MAX_RESPONSE_BYTES:
                self.tainted = True
                raise ElmSessionTainted("ELM response exceeded the bounded buffer")
            if b">" not in raw:
                self.tainted = True
                raise ElmCommandTimeout(f"response to {command} had no prompt")
            return raw.decode("ascii", errors="ignore").replace("\x00", "")

    async def initialize(self) -> float | None:
        """Run the known-good trace exactly; the first command after connect is ATZ."""
        replies: dict[str, str] = {}
        for command in INITIALIZATION_COMMANDS:
            timeout = RESET_TIMEOUT_S if command == "ATZ" else COMMAND_TIMEOUT_S
            replies[command] = await self.command(command, timeout_s=timeout)
        version = cleaned_response_lines(replies["ATI"], "ATI")
        self.adapter_version = " ".join(version) or None
        return await self.read_voltage()

    async def read_voltage(self) -> float | None:
        reply = await self.command("ATRV")
        if marker := response_transport_error(reply):
            raise ElmError(f"adapter reported {marker} for ATRV")
        return parse_voltage(reply)

    async def prove_ecu_online(self) -> frozenset[int]:
        """Require a checksum-valid 0100 reply; voltage never proves ECU presence."""
        await self.command("ATSP0")
        reply = await self.command("0100", timeout_s=PROTOCOL_SEARCH_TIMEOUT_S)
        if response_has_no_data(reply) or response_transport_error(reply):
            self.tainted = True
            raise EcuUnavailable("no usable ECU reply to 0100")
        payload = extract_pid_payload(reply, 0x00, 4, command="0100", strict_iso=True)
        source = extract_iso_source(reply, 0x01, 0x00, command="0100")
        if payload is None or source is None:
            self.tainted = True
            raise EcuUnavailable("0100 reply failed ISO length/source/checksum validation")
        self.ecu_source = source
        supported = {
            offset
            for offset in range(1, 33)
            if int.from_bytes(payload, "big") & (1 << (32 - offset))
        }
        if 0x20 in supported:
            extension = await self.command("0120")
            extension_payload = extract_pid_payload(
                extension,
                0x20,
                4,
                command="0120",
                strict_iso=True,
                expected_source=self.ecu_source,
            )
            if extension_payload is not None:
                mask = int.from_bytes(extension_payload, "big")
                supported.update(
                    0x20 + offset for offset in range(1, 33) if mask & (1 << (32 - offset))
                )
        return frozenset(supported)

    async def query_pid(self, pid: int) -> dict[str, object]:
        length = PID_LENGTHS.get(pid)
        if length is None:
            return {}
        command = f"01{pid:02X}"
        reply = await self.command(command)
        if response_has_no_data(reply):
            return {}
        if marker := response_transport_error(reply):
            raise ElmError(f"adapter reported {marker} for {command}")
        payload = extract_pid_payload(
            reply,
            pid,
            length,
            command=command,
            strict_iso=True,
            expected_source=self.ecu_source,
        )
        if payload is None:
            self.tainted = True
            raise ElmError(f"{command} reply failed ISO header/length/source/checksum validation")
        return decode_pid(pid, payload)

    async def disconnect(self) -> None:
        if self._connected and not self.tainted:
            try:
                await self.command("ATPC")
            except Exception:
                pass
        try:
            await self.channel.disconnect()
        finally:
            self._connected = False
            self.tainted = False
            self.ecu_source = None


class SessionRunner:
    """Bounded exponential reconnect policy; no tight loop after BLE contention."""

    def __init__(self, *, maximum_attempts: int = 5, maximum_delay_s: float = 300.0) -> None:
        self.maximum_attempts = maximum_attempts
        self.maximum_delay_s = maximum_delay_s

    def delays(self) -> tuple[float, ...]:
        return tuple(
            min(2.0**attempt, self.maximum_delay_s) for attempt in range(self.maximum_attempts)
        )
