"""Pure ELM327 and SAE J1979 parsing ported from the verified Tiida integration."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_HEX_LINE = re.compile(r"^[0-9A-Fa-f ]+$")
_INDEXED_HEX_LINE = re.compile(r"^[0-9A-Fa-f]+:\s*([0-9A-Fa-f ]+)$")
_VOLTAGE = re.compile(r"(?<![0-9.])([0-9]{1,2}(?:\.[0-9]+)?)\s*V\b", re.IGNORECASE)

NO_DATA_MARKERS = ("NO DATA",)
TRANSPORT_ERROR_MARKERS = (
    "UNABLE TO CONNECT",
    "BUS INIT: ERROR",
    "CAN ERROR",
    "STOPPED",
    "BUFFER FULL",
    "RX ERROR",
)

FUEL_SYSTEM_STATES = {
    0x01: "open_loop_insufficient_temperature",
    0x02: "closed_loop",
    0x04: "open_loop_engine_load_or_deceleration",
    0x08: "open_loop_system_failure",
    0x10: "closed_loop_with_fault",
}


def normalize_command(value: str) -> str:
    return "".join(value.split()).upper()


def response_text(raw: bytes | bytearray | str) -> str:
    if isinstance(raw, str):
        return raw.replace("\x00", "")
    return bytes(raw).decode("ascii", errors="ignore").replace("\x00", "")


def response_has_no_data(raw: bytes | bytearray | str) -> bool:
    upper = response_text(raw).upper()
    return any(marker in upper for marker in NO_DATA_MARKERS)


def response_transport_error(raw: bytes | bytearray | str) -> str | None:
    upper = response_text(raw).upper()
    return next((marker for marker in TRANSPORT_ERROR_MARKERS if marker in upper), None)


def cleaned_response_lines(raw: bytes | bytearray | str, command: str | None = None) -> list[str]:
    text = response_text(raw).replace(">", "\r")
    expected_echo = normalize_command(command) if command else None
    result: list[str] = []
    for original in re.split(r"[\r\n]+", text):
        line = original.strip()
        if not line:
            continue
        upper = line.upper()
        if expected_echo and normalize_command(line) == expected_echo:
            continue
        if upper.startswith("SEARCHING..."):
            line = line[len("SEARCHING...") :].strip()
            upper = line.upper()
            if not line:
                continue
        if upper in {"OK", "?", "BUS INIT: OK"}:
            continue
        if any(marker in upper for marker in NO_DATA_MARKERS + TRANSPORT_ERROR_MARKERS):
            continue
        result.append(line)
    return result


def _line_to_bytes(line: str) -> bytes | None:
    indexed = _INDEXED_HEX_LINE.fullmatch(line)
    if indexed:
        line = indexed.group(1)
    elif not _HEX_LINE.fullmatch(line):
        return None
    compact = line.replace(" ", "")
    if len(compact) < 2 or len(compact) % 2:
        return None
    try:
        return bytes.fromhex(compact)
    except ValueError:
        return None


def _split_iso_byte_run(data: bytes) -> list[bytes]:
    """Split byte-tokenized, concatenated ISO frames only at valid checksums."""
    if len(data) < 5:
        return [data]
    headers = [i for i in range(len(data) - 2) if data[i : i + 2] == b"\x48\x6b"]
    if not headers:
        return [data]
    boundaries = [*headers, len(data)]
    valid: dict[int, int] = {}
    for index, start in enumerate(headers):
        for end in boundaries[index + 1 :]:
            candidate = data[start:end]
            if len(candidate) >= 5 and sum(candidate[:-1]) & 0xFF == candidate[-1]:
                valid[start] = end
                break
    if not valid:
        return [data]
    result: list[bytes] = []
    cursor = 0
    while cursor < len(data):
        start = next((item for item in headers if item >= cursor and item in valid), None)
        if start is None:
            result.append(data[cursor:])
            break
        if start > cursor:
            result.append(data[cursor:start])
        result.append(data[start : valid[start]])
        cursor = valid[start]
    return [item for item in result if item]


def response_frames(raw: bytes | bytearray | str, command: str | None = None) -> list[bytes]:
    frames: list[bytes] = []
    byte_run = bytearray()

    def flush() -> None:
        if byte_run:
            frames.extend(_split_iso_byte_run(bytes(byte_run)))
            byte_run.clear()

    for line in cleaned_response_lines(raw, command):
        parsed = _line_to_bytes(line)
        if parsed is None:
            flush()
        elif len(parsed) == 1:
            byte_run.extend(parsed)
        else:
            flush()
            frames.append(parsed)
    flush()
    return frames


def _valid_frame(
    frame: bytes,
    marker_index: int,
    *,
    strict_iso: bool,
    expected_source: int | None,
    exact_end: int | None = None,
) -> bool:
    checksum_ok = marker_index != 3 or (len(frame) >= 5 and sum(frame[:-1]) & 0xFF == frame[-1])
    if strict_iso:
        if marker_index != 3 or len(frame) < 5 or frame[:2] != b"\x48\x6b" or not checksum_ok:
            return False
        if expected_source is not None and frame[2] != expected_source:
            return False
        return exact_end is None or len(frame) == exact_end + 1
    return checksum_ok


def extract_pid_payload(
    raw: bytes | bytearray | str,
    pid: int,
    expected_length: int,
    *,
    mode: int = 0x01,
    command: str | None = None,
    strict_iso: bool = False,
    expected_source: int | None = None,
) -> bytes | None:
    marker = bytes((mode + 0x40, pid))
    for frame in response_frames(raw, command):
        start = frame.find(marker)
        payload_end = start + len(marker) + expected_length
        if start < 0 or not _valid_frame(
            frame,
            start,
            strict_iso=strict_iso,
            expected_source=expected_source,
            exact_end=payload_end,
        ):
            continue
        return frame[start + len(marker) : payload_end]
    return None


def extract_iso_source(
    raw: bytes | bytearray | str,
    mode: int,
    pid: int | None = None,
    *,
    command: str | None = None,
) -> int | None:
    marker = bytes((mode + 0x40,)) if pid is None else bytes((mode + 0x40, pid))
    for frame in response_frames(raw, command):
        index = frame.find(marker)
        if (
            index == 3
            and len(frame) >= 5
            and frame[:2] == b"\x48\x6b"
            and sum(frame[:-1]) & 0xFF == frame[-1]
        ):
            return frame[2]
    return None


def extract_mode_payloads(
    raw: bytes | bytearray | str,
    mode: int,
    *,
    command: str | None = None,
    strict_iso: bool = False,
    expected_source: int | None = None,
) -> list[bytes]:
    result: list[bytes] = []
    for frame in response_frames(raw, command):
        try:
            index = frame.index(mode + 0x40)
        except ValueError:
            continue
        if not _valid_frame(frame, index, strict_iso=strict_iso, expected_source=expected_source):
            continue
        payload = frame[index + 1 :]
        if index >= 3 and payload:
            payload = payload[:-1]
        if payload:
            result.append(payload)
    return result


def parse_voltage(raw: bytes | bytearray | str) -> float | None:
    match = _VOLTAGE.search(response_text(raw))
    return float(match.group(1)) if match else None


def decode_supported_pids(base_pid: int, payload: bytes) -> frozenset[int]:
    if len(payload) < 4:
        return frozenset()
    mask = int.from_bytes(payload[:4], "big")
    return frozenset(base_pid + offset for offset in range(1, 33) if mask & (1 << (32 - offset)))


def _percentage(value: int) -> float:
    return value * 100.0 / 255.0


def _fuel_trim(value: int) -> float | None:
    return None if value == 0xFF else (value * 100.0 / 128.0) - 100.0


def decode_pid(pid: int, payload: bytes) -> dict[str, Any]:
    """Decode only the read-only, confirmed Mode 01 values logged by this vehicle."""
    if pid == 0x03 and len(payload) >= 2:
        states = [label for bit, label in FUEL_SYSTEM_STATES.items() if payload[0] & bit]
        return {"fuel_system_1": ", ".join(states) if states else "not_available"}
    if pid == 0x04 and payload:
        return {"engine_load": _percentage(payload[0])}
    if pid == 0x05 and payload:
        return {"coolant_temperature": payload[0] - 40.0}
    if pid == 0x06 and payload:
        return {"short_term_fuel_trim_bank_1": _fuel_trim(payload[0])}
    if pid == 0x07 and payload:
        return {"long_term_fuel_trim_bank_1": _fuel_trim(payload[0])}
    if pid == 0x0C and len(payload) >= 2:
        return {"engine_rpm": int.from_bytes(payload[:2], "big") / 4.0}
    if pid == 0x0D and payload:
        return {"vehicle_speed": float(payload[0])}
    if pid == 0x0E and payload:
        return {"timing_advance": payload[0] / 2.0 - 64.0}
    if pid == 0x0F and payload:
        return {"intake_air_temperature": payload[0] - 40.0}
    if pid == 0x10 and len(payload) >= 2:
        return {"mass_air_flow": int.from_bytes(payload[:2], "big") / 100.0}
    if pid == 0x11 and payload:
        return {"throttle_position": _percentage(payload[0])}
    if 0x14 <= pid <= 0x15 and len(payload) >= 2:
        sensor = pid - 0x13
        return {
            f"oxygen_sensor_{sensor}_voltage": payload[0] / 200.0,
            f"oxygen_sensor_{sensor}_short_term_fuel_trim": _fuel_trim(payload[1]),
        }
    return {}


def decode_dtcs(payloads: Iterable[bytes]) -> tuple[str, ...]:
    codes: list[str] = []
    for payload in payloads:
        for index in range(0, len(payload) - 1, 2):
            first, second = payload[index : index + 2]
            if first == second == 0:
                continue
            code = f"{'PCBU'[(first >> 6) & 3]}{(first >> 4) & 3:X}{first & 15:X}{second:02X}"
            if code not in codes:
                codes.append(code)
    return tuple(codes)


def calculate_estimates(values: dict[str, Any]) -> dict[str, float]:
    maf = values.get("mass_air_flow")
    if not isinstance(maf, int | float) or maf < 0:
        return {}
    litres_per_hour = float(maf) * 3600.0 / (14.7 * 745.0)
    result = {"estimated_fuel_rate": litres_per_hour}
    speed = values.get("vehicle_speed")
    if isinstance(speed, int | float) and speed >= 5:
        result["estimated_fuel_consumption"] = litres_per_hour * 100.0 / float(speed)
    return result
