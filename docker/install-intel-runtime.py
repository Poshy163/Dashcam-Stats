"""Install the hash-verified Intel package set, failing on any download/install error."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def download_package(package: dict[str, str], directory: Path) -> Path:
    url = package["url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ValueError(f"Unexpected package origin: {package['name']}")
    destination = directory / Path(parsed.path).name
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != package["sha256"]:
        raise ValueError(f"SHA256 mismatch for {package['name']}")
    destination.chmod(0o644)
    return destination


def install(manifest: dict, source: str = "pinned") -> None:
    if source not in {"pinned", "debian"}:
        raise ValueError(f"Unknown Intel runtime source: {source}")
    architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    if architecture != manifest["architecture"]:
        raise ValueError(f"Intel runtime requires {manifest['architecture']}, got {architecture}")
    if source == "debian":
        subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends", "intel-opencl-icd"], check=True
        )
    else:
        with tempfile.TemporaryDirectory(prefix="dashcam-intel-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o755)
            # Validate every archive before allowing apt to change any package.
            packages = [download_package(package, directory) for package in manifest["packages"]]
            subprocess.run(
                ["apt-get", "install", "-y", "--no-install-recommends", *map(str, packages)],
                check=True,
            )
    subprocess.run(["apt-get", "check"], check=True)
    subprocess.run(["ldconfig"], check=True)
    audit = subprocess.check_output(["dpkg", "--audit"], text=True).strip()
    if audit:
        raise RuntimeError(f"Incomplete package installation: {audit}")


if __name__ == "__main__":
    manifest_path = Path(sys.argv[1])
    source = sys.argv[2] if len(sys.argv) > 2 else "pinned"
    install(json.loads(manifest_path.read_text("utf-8")), source)
    manifest_path.with_name("intel-runtime-source").write_text(source + "\n", "utf-8")
