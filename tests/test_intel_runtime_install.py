"""Intel runtime builds fail on corrupt packages, broken installs and ABI mismatches."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer():
    return _module("intel_installer", "docker/install-intel-runtime.py")


@pytest.fixture
def checker():
    return _module("intel_checker", "backend/scripts/check_intel_runtime.py")


def _package(payload=b"verified Debian package"):
    return {
        "name": "intel-opencl-icd",
        "version": "25.13.33276.16",
        "url": "https://github.com/intel/compute-runtime/releases/download/test/runtime.deb",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_download_accepts_only_the_expected_package_bytes(installer, monkeypatch, tmp_path):
    payload = b"verified Debian package"
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(payload))
    path = installer.download_package(_package(payload), tmp_path)
    assert path.read_bytes() == payload


def test_corrupt_download_cannot_reach_package_installation(installer, monkeypatch):
    monkeypatch.setattr(installer.subprocess, "check_output", lambda *a, **kw: "amd64\n")
    monkeypatch.setattr(
        installer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"corrupt")
    )
    commands = []
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: commands.append(a))
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        installer.install({"architecture": "amd64", "packages": [_package()]})
    assert commands == []


def test_failed_apt_install_is_not_swallowed(installer, monkeypatch):
    monkeypatch.setattr(installer.subprocess, "check_output", lambda *a, **kw: "amd64\n")
    monkeypatch.setattr(
        installer, "download_package", lambda package, directory: directory / "p.deb"
    )

    def fail(command, **kwargs):
        assert kwargs["check"] is True
        raise subprocess.CalledProcessError(100, command)

    monkeypatch.setattr(installer.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install({"architecture": "amd64", "packages": [_package()]})


def test_wrong_architecture_cannot_start_downloads(installer, monkeypatch):
    monkeypatch.setattr(installer.subprocess, "check_output", lambda *a, **kw: "arm64\n")
    with pytest.raises(ValueError, match="requires amd64"):
        installer.install({"architecture": "amd64", "packages": [_package()]})


def test_unknown_runtime_source_cannot_start_installation(installer):
    with pytest.raises(ValueError, match="Unknown Intel runtime source"):
        installer.install({}, "typo")


def test_legacy_selector_uses_the_distribution_packages(installer, monkeypatch):
    monkeypatch.setattr(
        installer.subprocess,
        "check_output",
        lambda command, **kw: "amd64\n" if "--print-architecture" in command else "",
    )
    commands = []
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **kw: commands.append(command))
    installer.install({"architecture": "amd64"}, "debian")
    assert commands[0] == [
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "intel-opencl-icd",
    ]
    assert ["apt-get", "check"] in commands
    assert ["ldconfig"] in commands


def test_checker_rejects_broken_package_state(checker, monkeypatch):
    monkeypatch.setattr(checker.subprocess, "check_output", lambda *a, **kw: "package is unpacked")
    with pytest.raises(RuntimeError, match="Incomplete package installation"):
        checker.check({"packages": []})


def test_checker_rejects_a_different_installed_version(checker, monkeypatch):
    monkeypatch.setattr(
        checker.subprocess,
        "check_output",
        lambda command, **kw: "" if command[0] == "dpkg" else "installed 22.43.24595.41",
    )
    with pytest.raises(RuntimeError, match=r"expected installed 25\.13\.33276\.16"):
        checker.check({"packages": [_package()]})


def test_checker_fails_if_media_driver_exists_but_cannot_link(checker, monkeypatch, tmp_path):
    monkeypatch.setattr(checker.subprocess, "check_output", lambda *a, **kw: "")
    icd = tmp_path / "intel.icd"
    icd.write_text("/usr/lib/intel-opencl/libigdrcl.so\n")
    monkeypatch.setattr(checker, "INTEL_ICD", icd)
    loaded = []

    def load(library, *, mode):
        assert mode == 2  # RTLD_NOW; lazy symbol checks are insufficient.
        loaded.append(library)
        if library == checker.MEDIA_DRIVER:
            raise OSError("GLIBC_2.38 not found")

    monkeypatch.setattr(checker.ctypes, "CDLL", load)
    with pytest.raises(OSError, match=r"GLIBC_2\.38"):
        checker.check({"packages": []})
    assert "/usr/lib/intel-opencl/libigdrcl.so" in loaded
    assert "libigc.so.2" in loaded


def test_manifest_keeps_the_matched_runtime_compiler_and_gmm_set():
    manifest = json.loads((ROOT / "docker/intel-runtime.json").read_text("utf-8"))
    assert {package["name"]: package["version"] for package in manifest["packages"]} == {
        "intel-opencl-icd": "25.13.33276.16",
        "intel-igc-core-2": "2.10.8",
        "intel-igc-opencl-2": "2.10.8",
        "libigdgmm12": "22.7.0",
    }
    for package in manifest["packages"]:
        assert len(bytes.fromhex(package["sha256"])) == 32
        assert package["url"].endswith("_amd64.deb")
