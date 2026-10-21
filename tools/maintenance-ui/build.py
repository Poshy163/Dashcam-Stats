"""Rebuild the bundled read-only Android UI helper with an already installed SDK.

No dependencies are downloaded. --check verifies reproducibility without changing
the packaged asset. The tested toolchain is JDK 17, Android SDK platform 35 and
Android build-tools 35.0.0 (D8 8.6.2-dev).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android-sdk", type=Path)
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sdk = args.android_sdk or Path(
        os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or "."
    )
    java = args.java_home or Path(os.environ.get("JAVA_HOME") or ".")
    suffix = ".exe" if os.name == "nt" else ""
    javac = java / "bin" / f"javac{suffix}"
    d8 = sdk / "build-tools" / "35.0.0" / ("d8.bat" if os.name == "nt" else "d8")
    library = sdk / "platforms" / "android-35" / "android.jar"
    for path in (javac, d8, library):
        if not path.is_file():
            parser.error(f"Existing toolchain file is required: {path}")
    directory = Path(__file__).resolve().parent
    asset = directory.parents[1] / "backend/app/ingest/assets/fresh-ui.dex"
    with tempfile.TemporaryDirectory(prefix="dashcam-maintenance-ui-") as temporary:
        output = Path(temporary)
        classes, dex = output / "classes", output / "dex"
        classes.mkdir()
        dex.mkdir()
        subprocess.run(
            [
                str(javac),
                "--release",
                "8",
                "-g:none",
                "-cp",
                str(library),
                "-d",
                str(classes),
                str(directory / "FreshUiSnapshot.java"),
            ],
            check=True,
        )
        subprocess.run(
            [
                str(d8),
                "--release",
                "--min-api",
                "23",
                "--lib",
                str(library),
                "--output",
                str(dex),
                str(classes / "com/dashcamstats/maintenance/FreshUiSnapshot.class"),
            ],
            check=True,
            env={**os.environ, "JAVA_HOME": str(java)},
        )
        generated = dex / "classes.dex"
        raw = generated.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if args.check:
            if not asset.is_file() or raw != asset.read_bytes():
                raise SystemExit("Generated UI helper differs from the packaged asset.")
        else:
            asset.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(generated, asset)
        print(f"fresh-ui.dex SHA-256 {digest} ({len(raw)} bytes)")


if __name__ == "__main__":
    main()
