"""Check installed Intel package versions and eager shared-library linking without a GPU."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
from pathlib import Path

DEFAULT_MANIFEST = Path("/usr/local/share/dashcam/intel-runtime.json")
INTEL_ICD = Path("/etc/OpenCL/vendors/intel.icd")
MEDIA_DRIVER = "/usr/lib/x86_64-linux-gnu/dri/iHD_drv_video.so"


def check(manifest: dict, source: str = "pinned") -> str:
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
    versions = {}
    for package in packages:
        installed = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package["name"]], text=True
        ).strip()
        expected = f"installed {package['version']}" if source == "pinned" else "installed "
        if (source == "pinned" and installed != expected) or not installed.startswith("installed "):
            raise RuntimeError(f"{package['name']}: expected {expected}, got {installed}")
        versions[package["name"]] = installed.removeprefix("installed ")
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
    payload = json.dumps(
        {"source": source, "packages": versions}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main(arguments: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-key-output", type=Path)
    args = parser.parse_args(arguments)
    source = args.manifest.with_name("intel-runtime-source").read_text("utf-8").strip()
    fingerprint = check(json.loads(args.manifest.read_text("utf-8")), source)
    # Normal CI/runtime verification is read-only. Image construction opts in only
    # after successful package validation and eager linking of the entire stack.
    if args.cache_key_output is not None:
        args.cache_key_output.write_bytes((fingerprint + "\n").encode("ascii"))


if __name__ == "__main__":
    main()
