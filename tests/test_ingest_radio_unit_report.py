"""The head unit's own pre-sleep radio report.

The failure these cover is the ordinary one, not an exotic one. A backup window ends when
the head unit sleeps; after that instant the server can prove nothing about either radio,
so it was left holding *restore attempted, restore not verified* -- which it cannot tell
apart from a watchdog that never ran, and therefore treats as recovery still owing. The
next backup is then blocked on evidence only the unit could ever have produced.

So the unit produces it: seconds before it sleeps, the detached watchdog restores both
radios, reads them back on the device, and posts the result with a token minted for that
one transition.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import IngestRadioTransition
from app.ingest import radio_coordinator, radios

TRANSITION_ID = "00000000-0000-0000-0000-0000000000aa"
TOKEN = "b" * 48


async def _seed(
    db_session,
    *,
    token: str | None = TOKEN,
    active: bool = True,
    lease_age_s: int = 600,
    recovery_required: bool = True,
) -> None:
    """A transition in the exact state the old code got stuck in.

    Both radios were taken down and verified down; both restores were *attempted* and
    neither could be verified, because by then the unit had gone.
    """
    now = datetime.now(UTC)
    db_session.add(
        IngestRadioTransition(
            transition_id=TRANSITION_ID,
            trigger="auto",
            phase="recovery_required" if recovery_required else "restoring_radios",
            active=active,
            created_at=now - timedelta(minutes=20),
            updated_at=now,
            heartbeat_at=now,
            lease_owner="owner",
            lease_expires_at=now - timedelta(seconds=lease_age_s),
            device_address="192.168.1.214:5555",
            transport_host="192.168.1.214",
            bluetooth_before="on",
            hotspot_before="on",
            hotspot_interface="wlan1",
            bluetooth_disable_attempted=True,
            bluetooth_disable_verified=True,
            hotspot_disable_attempted=True,
            hotspot_disable_verified=True,
            bluetooth_restore_attempted=True,
            bluetooth_restore_verified=False,
            hotspot_restore_attempted=True,
            hotspot_restore_verified=False,
            recovery_required=recovery_required,
            unit_report_token=token,
        )
    )
    await db_session.commit()


async def _row(db_session) -> IngestRadioTransition:
    db_session.expire_all()
    row = await db_session.scalar(
        select(IngestRadioTransition).where(IngestRadioTransition.transition_id == TRANSITION_ID)
    )
    assert row is not None
    return row


def _report(**overrides) -> radio_coordinator.UnitRadioReport:
    fields = {
        "transition_id": TRANSITION_ID,
        "token": TOKEN,
        "reason": "pre_sleep",
        "bluetooth": "1",
        "hotspot": "1",
        "interface": "wlan1",
    }
    fields.update(overrides)
    return radio_coordinator.UnitRadioReport(**fields)


async def test_a_verified_report_closes_the_transition_the_server_could_not(db_session):
    """The whole point: the block clears without the car having to come back."""
    await _seed(db_session)

    assert await radio_coordinator.apply_unit_report(_report()) is True

    row = await _row(db_session)
    assert row.recovery_required is False
    assert row.active is False
    assert row.phase == "complete"
    assert row.completed_at is not None
    assert row.bluetooth_restore_verified is True
    assert row.hotspot_restore_verified is True
    assert row.restore_evidence_source == "unit"
    assert row.unit_reported_at is not None
    assert row.unit_sleep_reported_at is not None
    # Single-use: the token that authorised this report cannot authorise another.
    assert row.unit_report_token is None


async def test_a_report_of_failure_is_worth_more_than_silence(db_session):
    """``0`` is a fact about the car, and it must not read as "restored"."""
    await _seed(db_session)

    assert await radio_coordinator.apply_unit_report(_report(hotspot="0")) is True

    row = await _row(db_session)
    assert row.hotspot_restore_verified is False
    assert row.bluetooth_restore_verified is True
    # Still owing, still blocking -- but now for a reason somebody can read.
    assert row.recovery_required is True
    assert row.phase == "recovery_required"
    assert "hotspot" in (row.last_error or "")
    assert row.unit_reported_at is not None


async def test_a_radio_the_watchdog_never_touched_is_left_alone(db_session):
    """``skip`` is not a claim about a readback and must not become one."""
    await _seed(db_session)
    seeded = await _row(db_session)
    seeded.bluetooth_disable_attempted = False
    seeded.bluetooth_disable_verified = False
    await db_session.commit()

    assert await radio_coordinator.apply_unit_report(_report(bluetooth="skip")) is True

    row = await _row(db_session)
    assert row.bluetooth_restore_verified is False
    assert row.hotspot_restore_verified is True
    assert row.recovery_required is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"token": "c" * 48},
        {"token": ""},
        {"transition_id": "00000000-0000-0000-0000-0000000000ff"},
        {"transition_id": "'; DROP TABLE ingest_radio_transitions; --"},
        {"bluetooth": "yes"},
        {"hotspot": "../../etc/passwd"},
    ],
)
async def test_a_report_that_cannot_be_trusted_changes_nothing(db_session, overrides):
    await _seed(db_session)

    assert await radio_coordinator.apply_unit_report(_report(**overrides)) is False

    row = await _row(db_session)
    assert row.recovery_required is True
    assert row.bluetooth_restore_verified is False
    assert row.restore_evidence_source is None
    assert row.unit_report_token == TOKEN


async def test_a_transition_with_no_token_never_accepts_a_report(db_session):
    """A row from before this feature, or one whose token was already spent."""
    await _seed(db_session, token=None)

    assert await radio_coordinator.apply_unit_report(_report()) is False

    row = await _row(db_session)
    assert row.recovery_required is True


async def test_a_live_owner_is_never_closed_out_from_underneath(db_session):
    """A running transfer still owns the row; the report is evidence, not a takeover."""
    await _seed(db_session, lease_age_s=-600, recovery_required=False)

    assert await radio_coordinator.apply_unit_report(_report()) is True

    row = await _row(db_session)
    assert row.active is True
    assert row.phase == "restoring_radios"
    assert row.completed_at is None
    # The evidence still lands, so the owner's own restore does not have to re-prove it.
    assert row.bluetooth_restore_verified is True
    assert row.restore_evidence_source == "unit"


async def test_the_endpoint_answers_the_watchdog_without_a_session(client, db_session):
    """The caller is a shell script fifteen seconds from sleep. It has no cookie."""
    await _seed(db_session)

    response = await client.post(
        "/api/ingest/radio-recovery",
        json={
            "transition_id": TRANSITION_ID,
            "token": TOKEN,
            "reason": "pre_sleep",
            "bluetooth": "1",
            "hotspot": "1",
            "interface": "wlan1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert (await _row(db_session)).recovery_required is False


async def test_a_wrong_token_is_refused_without_saying_why(client, db_session):
    await _seed(db_session)

    response = await client.post(
        "/api/ingest/radio-recovery",
        json={"transition_id": TRANSITION_ID, "token": "d" * 48, "bluetooth": "1", "hotspot": "1"},
    )

    # 200 and `accepted: false`, both times. A status code that told the difference between
    # a wrong token and an unknown transition would be an oracle.
    assert response.status_code == 200
    assert response.json() == {"accepted": False}
    assert (await _row(db_session)).recovery_required is True


# ---------------------------------------------------------------------------------------
# The shell the unit actually runs
# ---------------------------------------------------------------------------------------


@pytest.fixture(params=["sh", "mksh"])
def posix_shell(request):
    shell = shutil.which(request.param)
    if not shell and os.name == "nt":
        candidate = Path("C:/Program Files/Git/usr/bin") / f"{request.param}.exe"
        shell = str(candidate) if candidate.exists() else None
    if not shell:
        if os.name == "nt":
            pytest.skip(f"{request.param} unavailable on Windows")
        pytest.fail(f"{request.param} is required for Android shell regression tests")
    return shell


def _report_config(**overrides) -> radios.WatchdogReport:
    fields = {
        "host": "192.168.1.16",
        "port": 8199,
        "token": TOKEN,
        "transition_id": TRANSITION_ID,
    }
    fields.update(overrides)
    return radios.WatchdogReport(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "192.168.1.16; rm -rf /"},
        {"host": "$(id)"},
        {"host": "'"},
        {"port": 0},
        {"port": 99999},
        {"token": "not hex"},
        {"token": "e" * 100},
        {"transition_id": "../../etc"},
        {"guard_s": -1},
        {"guard_s": 6000},
        {"acc_off_elapsed_s": -5},
        {"path": "/api/settings"},
    ],
)
def test_a_report_target_that_could_carry_shell_syntax_is_refused(overrides):
    """Every field here is expanded into a script running on the head unit."""
    with pytest.raises(ValueError):
        _report_config(**overrides)


async def test_the_server_hands_the_watchdog_the_countdowns_age(monkeypatch):
    """Not the ignition's. The two differed by eighteen minutes on the run that broke.

    A top-up twenty minutes into a park had just rewritten the window, so the unit's
    countdown was seconds old. Handed the ignition-off figure instead, the watchdog took
    the countdown for long expired, decided the unit had already slept, and fired on its
    first poll mid-transfer -- and the server, finding its watchdog gone, aborted a healthy
    run. The number carried across has to be the countdown's own age.
    """
    from app.ingest import origin, radio_coordinator

    class Status:
        def sleep_countdown_elapsed_s(self):
            return 5

        def ignition_off_elapsed_s(self):
            return 1100

        def sleep_deadline_uptime_s(self):
            return None

    monkeypatch.setattr(radio_coordinator, "get_status", lambda: Status())
    monkeypatch.setattr(origin, "callback_endpoint", lambda: ("192.168.1.16", 8199))

    report = radio_coordinator._watchdog_report(TRANSITION_ID, TOKEN)

    assert report is not None
    assert report.acc_off_elapsed_s == 5


def test_the_sleep_guard_aims_at_the_real_sleep_not_the_arming_time(tmp_path, posix_shell):
    """The countdown starts at ignition-off, and arming happens well into it.

    A watchdog that anchored on its own start would aim sixty seconds late, which on this
    feature means firing *after* the unit has slept -- the failure it exists to prevent.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "getprop").write_text("#!/bin/sh\necho 1200\n")
    (stub / "settings").write_text("#!/bin/sh\necho 0\n")
    for entry in stub.iterdir():
        entry.chmod(0o755)

    guard = radios._watchdog_sleep_guard_functions(_report_config(acc_off_elapsed_s=60)).replace(
        "/system/bin/", ""
    )
    script = (
        'reason=lease_expired; acc_off_at=""; acc_elapsed=60; ' + guard +
        # Ignition dropped 60s before this first observation, so the unit sleeps 1140s
        # from here and the guard must fire 60s before that.
        'now=1000; remaining=99999; sleep_fold; echo "$acc_off_at $remaining $reason"; '
        'now=2079; remaining=99999; sleep_fold; echo "$remaining $reason"; '
        'now=2080; remaining=99999; sleep_fold; echo "$remaining $reason"'
    )
    result = subprocess.run(
        [posix_shell, "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub}:/usr/bin:/bin"},
        timeout=30,
    )

    first, before, at = result.stdout.strip().splitlines()
    # 1000 - 60 elapsed: the countdown is anchored where it really started.
    assert first == "940 1080 lease_expired"
    assert before == "1 lease_expired"
    # 940 + 1200 = 2140 is the sleep; 2080 gives restoration sixty seconds.
    assert at == "0 pre_sleep"


@pytest.mark.parametrize("acc", ["1", "on", "", "null", "garbage"])
def test_an_unreadable_ignition_never_invents_a_deadline(tmp_path, acc, posix_shell):
    """Restoring the driver's Bluetooth twenty minutes early is its own kind of damage."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "getprop").write_text("#!/bin/sh\necho 1200\n")
    (stub / "settings").write_text(f"#!/bin/sh\necho '{acc}'\n")
    for entry in stub.iterdir():
        entry.chmod(0o755)

    guard = radios._watchdog_sleep_guard_functions(_report_config()).replace("/system/bin/", "")
    result = subprocess.run(
        [
            posix_shell,
            "-c",
            'reason=lease_expired; acc_off_at=""; acc_elapsed=0; '
            + guard
            + 'now=1000; remaining=500; sleep_fold; echo "$remaining $reason"',
        ],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub}:/usr/bin:/bin"},
        timeout=30,
    )

    # Untouched: the server's lease remains the only deadline.
    assert result.stdout.strip() == "500 lease_expired"


def test_the_watchdog_reads_both_radios_back_and_posts_valid_json(tmp_path, posix_shell):
    """The readbacks are the server's own two, taken on the device instead of over ADB."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "settings").write_text("#!/bin/sh\necho 1\n")
    (stub / "ip").write_text(
        "#!/bin/sh\n"
        # wlan0 carries the address this app reached the unit on: the transport, not an AP.
        "echo '2: wlan0    inet 192.168.1.214/24 brd 192.168.1.255 scope global wlan0'\n"
        "echo '5: wlan1    inet 192.168.43.1/24 brd 192.168.43.255 scope global wlan1'\n"
    )
    for entry in stub.iterdir():
        entry.chmod(0o755)

    body = radios._watchdog_report_functions(
        _report_config(),
        restore_bluetooth=True,
        hotspot_baseline="on",
        transport_host="192.168.1.214",
        expected_interface="wlan1",
    ).replace("/system/bin/", "")
    report_file = tmp_path / "report.json"
    body = body.replace(radios.WATCHDOG_REPORT_PARTIAL_PATH, str(tmp_path / "scan"))
    body = body.replace(radios.WATCHDOG_REPORT_PATH, str(report_file))

    result = subprocess.run(
        [
            posix_shell,
            "-c",
            "reason=pre_sleep; " + body + 'verify_radios; echo "$bt $hs $ap_iface"',
        ],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub}:/usr/bin:/bin"},
        timeout=30,
    )

    # The serving AP is found by the address it holds, never by being called wlan-anything.
    assert result.stdout.strip() == "1 1 wlan1"


@pytest.mark.parametrize(
    ("baseline", "scenario", "expected", "scans"),
    [
        ("on", "delayed", "1 wlan2", 4),
        ("on", "flap", "1 wlan2", 4),
        ("on", "wrong", "0 wlan1", 11),
        ("on", "alongside", "1 wlan2", 2),
        ("off", "unreadable", "0", 11),
        ("off", "malformed", "0", 11),
        ("off", "absent", "1", 2),
        ("transport", "unreadable", "skip", 0),
    ],
)
def test_watchdog_requires_stable_exact_ap_and_readable_absence(
    tmp_path, posix_shell, baseline, scenario, expected, scans
):
    stub = tmp_path / "bin"
    stub.mkdir()
    counter = tmp_path / "scans"
    counter.write_text("0")
    # A deterministic clock replaces only waiting, not the generated verification logic.
    # ip/settings still execute as real child processes through timeout, as on Android.
    (stub / "settings").write_text("#!/bin/sh\necho 1\n", newline="\n")
    (stub / "ip").write_text(
        "#!/bin/sh\n"
        f"n=$(cat '{counter.as_posix()}'); n=$((n+1)); echo $n > '{counter.as_posix()}'\n"
        + ("exit 1\n" if scenario == "unreadable" else "")
        + ("echo 'not an interface inventory'; exit 0\n" if scenario == "malformed" else "")
        + "echo '2: wlan0 inet 192.0.2.10/24 scope global wlan0'\n"
        + {
            "delayed": '[ "$n" -ge 3 ] && echo "5: wlan2 inet 192.0.2.1/24 scope global wlan2"\n',
            "flap": '[ "$n" -ne 2 ] && echo "5: wlan2 inet 192.0.2.1/24 scope global wlan2"\n',
            "wrong": 'echo "5: wlan1 inet 192.0.2.1/24 scope global wlan1"\n',
            "alongside": 'echo "5: wlan1 inet 192.0.2.2/24 scope global wlan1"\n'
            'echo "6: wlan2 inet 192.0.2.1/24 scope global wlan2"\n',
        }.get(scenario, "")
        + "exit 0\n",
        newline="\n",
    )
    for entry in stub.iterdir():
        entry.chmod(0o755)
    body = radios._watchdog_report_functions(
        _report_config(),
        restore_bluetooth=True,
        hotspot_baseline=baseline,
        transport_host="192.0.2.10",
        expected_interface="wlan2",
    ).replace("/system/bin/", "")
    body = body.replace(radios.WATCHDOG_REPORT_PARTIAL_PATH, (tmp_path / "scan").as_posix())
    result = subprocess.run(
        [
            posix_shell,
            "-c",
            "ticks=0; sleep() { ticks=$((ticks+1)); }; cut() { echo $ticks; }; "
            + body
            + 'verify_radios; echo "$hs $ap_iface"',
        ],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub.as_posix()}:/usr/bin:/bin"},
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    assert int(counter.read_text()) == scans


def test_expected_hotspot_interface_cannot_inject_shell():
    with pytest.raises(ValueError, match="interface"):
        radios._watchdog_report_functions(
            _report_config(),
            restore_bluetooth=True,
            hotspot_baseline="on",
            transport_host="192.0.2.10",
            expected_interface="wlan2; exit 0",
        )


async def test_watchdog_reports_before_bounded_courtesy_screen_change(monkeypatch):
    from tests.test_ingest_radios import _arm_and_capture_watchdog

    _handle, captured = await _arm_and_capture_watchdog(
        monkeypatch,
        restore_bluetooth=True,
        hotspot_baseline="on",
        hotspot_capsule_path=f"{radios.HOTSPOT_CAPSULE_PREFIX}{TRANSITION_ID}.json",
        hotspot_restore_mode=radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
        expected_interface="wlan2",
        report=_report_config(),
    )
    command = captured["launcher"]
    assert command.index("action.start.tethering") < command.index("verify_radios; publish_report")
    assert command.index("verify_radios; publish_report") < command.index("timeout 6 monkey")
    assert "expected_ap=" in command and "wlan2" in command
    assert radios.WATCHDOG_SLEEP_GUARD_S == 60


async def test_watchdog_without_working_timeout_never_publishes_readiness(
    monkeypatch, tmp_path, posix_shell
):
    from tests.test_ingest_radios import _arm_and_capture_watchdog

    handle, captured = await _arm_and_capture_watchdog(monkeypatch)
    script = (
        captured["launcher"]
        .split("<<'DASHCAM_RADIO_WATCHDOG'\n", 1)[1]
        .split("\nDASHCAM_RADIO_WATCHDOG\n", 1)[0]
    )
    # Execute the actual generated script; a missing/broken timeout must stop it
    # before ownership, readiness, or any restoration command is published.
    script = script.replace("/sdcard/", tmp_path.as_posix() + "/")
    result = subprocess.run(
        [posix_shell, "-c", "timeout() { return 127; }; " + script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1, result.stderr
    assert not list(tmp_path.iterdir()), handle.ready_path


async def test_hotspot_watchdog_requires_captured_interface():
    assert (
        await radios._arm_watchdog(
            "unit:5555",
            300,
            hotspot_baseline="on",
            hotspot_capsule_path=f"{radios.HOTSPOT_CAPSULE_PREFIX}{TRANSITION_ID}.json",
            hotspot_restore_mode=radios.HOTSPOT_RESTORE_BLUETOOTH_REARM,
        )
        is None
    )
