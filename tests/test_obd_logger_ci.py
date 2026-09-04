from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_obd_logger_build_is_inside_the_reusable_release_gate() -> None:
    """A broken Android companion must not be able to ship a server image.

    The gate is ``publish: needs: ci``: it waits on the whole called workflow, and the
    Android job is in that workflow, so that job failing fails the workflow and the
    publish never runs.

    This used to also assert ``needs: [backend, frontend, android-obd-logger]`` on the
    ``docker`` job, which was a different thing wearing the same clothes -- an ordering
    inside ci.yml rather than the release gate, and one that cost three minutes on every
    green run by holding the image build behind a test job it shares nothing with.
    Dropping it changes nothing about what can be published, so what is pinned here now
    is the gate itself plus the property that makes building early safe: the image built
    inside ci.yml is never pushed anywhere.
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "  android-obd-logger:\n" in ci
    assert "push: false" in ci, "the image built inside ci.yml must never be published"
    assert "push: true" not in ci
    for task in (
        ":app:testDebugUnitTest",
        ":app:lintDebug",
        ":app:lintRelease",
        ":app:assembleDebug",
        ":app:assembleRelease",
    ):
        assert task in ci
    assert "uses: ./.github/workflows/ci.yml" in release
    assert "  publish:\n    needs: ci\n" in release
    assert not (ROOT / ".github" / "workflows" / "android-obd-logger.yml").exists()


def test_obd_logger_requests_only_the_bluetooth_permission_it_uses() -> None:
    android = ROOT / "android" / "obd-logger" / "app" / "src" / "main"
    manifest = (android / "AndroidManifest.xml").read_text(encoding="utf-8")
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (android / "java").rglob("*.kt")
    )

    assert "android.permission.BLUETOOTH_CONNECT" in manifest
    assert "android.permission.BLUETOOTH_SCAN" not in manifest
    assert "startScan(" not in sources


def test_obd_logger_production_signing_is_explicit_and_secrets_are_ignored() -> None:
    gradle = (ROOT / "android" / "obd-logger" / "app" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "obd-dashcam-logger.md").read_text(encoding="utf-8")

    assert "OBD_PRODUCTION_SIGNING" in gradle
    assert "Production OBD signing was requested but signing inputs are absent" in gradle
    for secret_name in (
        "OBD_RELEASE_KEYSTORE_PATH",
        "OBD_RELEASE_KEYSTORE_PASSWORD",
        "OBD_RELEASE_KEY_ALIAS",
        "OBD_RELEASE_KEY_PASSWORD",
    ):
        assert secret_name in gradle
    assert "keystore.properties" in ignored
    assert "*.jks" in ignored
    assert "*.keystore" in ignored
    assert "development/build-verification artifacts only" in docs
    assert "Signer #1 certificate SHA-256 digest" in docs
