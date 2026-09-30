"""Check installed Intel package versions and eager shared-library linking without a GPU."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_MANIFEST = Path("/usr/local/share/dashcam/intel-runtime.json")
INTEL_ICD = Path("/etc/OpenCL/vendors/intel.icd")
MEDIA_DRIVER = "/usr/lib/x86_64-linux-gnu/dri/iHD_drv_video.so"


def check(manifest: dict, source: str = "pinned") -> None:
    if source not in {"pinned", "debian"}:
        raise ValueError(f"Unknown Intel runtime source: {source}")
    audit = subprocess.check_output(["dpkg", "--audit"], text=True).strip()
    if audit:
        raise RuntimeError(f"Incomplete package installation: {audit}")
    packages = (
        manifest["packages"]
        if source == "pinned"
        else [
            {"name": name} for name in ("intel-opencl-icd", "libigc1", "libigdfcl1", "libigdgmm12")
        ]
    )
    for package in packages:
        installed = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package["name"]], text=True
        ).strip()
        expected = f"installed {package['version']}" if source == "pinned" else "installed "
        if (source == "pinned" and installed != expected) or not installed.startswith("installed "):
            raise RuntimeError(f"{package['name']}: expected {expected}, got {installed}")
        print(f"{package['name']} {installed}")
    driver = INTEL_ICD.read_text("utf-8").strip()
    if not driver or "\n" in driver:
        raise RuntimeError("Invalid Intel OpenCL ICD registration")
    # NOW catches missing GLIBC/GLIBCXX symbols and transitive dependencies, not merely
    # the presence of the .so file. Hardware availability still requires a live probe.
    compilers = (
        ("libigc.so.2", "libiga64.so.2", "libigdfcl.so.2", "libopencl-clang2.so.15")
        if source == "pinned"
        else ("libigc.so.1", "libigdfcl.so.1")
    )
    for library in (*compilers, "libigdgmm.so.12", driver, MEDIA_DRIVER):
        ctypes.CDLL(library, mode=getattr(os, "RTLD_NOW", 2))
        print(f"linked: {library}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MANIFEST
    source = path.with_name("intel-runtime-source").read_text("utf-8").strip()
    check(json.loads(path.read_text("utf-8")), source)
