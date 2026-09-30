# Dependency and release process

Runtime requirements are maintained in `backend/requirements.txt`; package metadata in
`backend/pyproject.toml` declares the same requirements. Development tools are maintained
in `backend/requirements-dev.txt`. The exact Ruff version is shared by local checks and CI.

Production and Linux CI use committed, hashed locks for CPython 3.12 on Linux x86-64:

- `backend/requirements-linux-py312.lock` contains the complete runtime dependency graph.
- `backend/requirements-dev-linux-py312.lock` adds development dependencies while retaining
  exactly the runtime versions above.
- `backend/requirements-build.lock` pins the patched pip bootstrap installer separately
  from application metadata. Both the runtime base and the application virtualenv receive it.

Docker installs binary wheels with `--require-hashes --only-binary=:all:`. It does not
resolve fresh versions or compile unrecorded source distributions. The Node and Python
base images and Dockerfile frontend are pinned by registry digest.

## Refreshing dependencies

The current lock files were generated with uv 0.12.0. After changing a supported range,
resolve the runtime graph first and use it as a constraint for the development graph:

```powershell
uv pip compile backend/requirements.txt --upgrade --python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes --only-binary :all: -o backend/requirements-linux-py312.lock
uv pip compile backend/requirements-dev.txt --upgrade --constraint backend/requirements-linux-py312.lock --python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes --only-binary :all: -o backend/requirements-dev-linux-py312.lock
uv pip compile backend/requirements-build.txt --python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes --only-binary :all: -o backend/requirements-build.lock
uv pip install --python .venv/Scripts/python.exe --upgrade -r backend/requirements-dev.txt
uv pip check --python .venv/Scripts/python.exe
uvx --from pip-audit==2.10.1 pip-audit --path .venv/Lib/site-packages
uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps -r backend/requirements-linux-py312.lock
uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps -r backend/requirements-dev-linux-py312.lock
uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps -r backend/requirements-build.lock
```

The Windows development environment resolves its own platform wheels; it is audited
separately and does not replace Linux image verification. OpenVINO 2025.4.1 and ONNX
Runtime 1.27.0 remain deliberate pins. OpenVINO also constrains NumPy below 2.4; do not
force the newest NumPy past that contract.

For a base image update, inspect the published manifest digest with
`docker buildx imagetools inspect node:22-bookworm-slim` and
`docker buildx imagetools inspect python:3.12-slim-bookworm`, then update both Python stages
together. Debian packages are still resolved from the configured Debian repositories at
build time. Consequently, these pins improve dependency reproducibility but are not a
claim of bit-for-bit reproducible rebuilds of the operating-system layers.

Run the full backend/media/auth/migration suite, frontend checks, and the actual Linux
Docker build after an upgrade. CI audits both Python locks and the packages installed in
the built image. `pip-audit` covers Python advisories; it does not constitute a Debian
package or physical GPU certification.

## Intel GPU runtime

The image installs the complete Intel OpenCL package set recorded in
`docker/intel-runtime.json`: NEO 25.13.33276.16, IGC 2.10.8 (archive build 18926),
and GMM 22.7.0. The Debian Bookworm base and its iHD media driver are retained.
NEO 25.13 is the last series built for Ubuntu 22.04; later upstream binaries can
require libc and libstdc++ symbols unavailable on Bookworm. This replaces the
optional 26.27 installation that could leave partially configured packages after
an ignored failure.

Package URLs, installed versions and SHA256 values are pinned together. Every
archive must verify before apt runs; download, checksum, dependency and package
configuration errors fail the build. Build and CI checks eagerly load the compiler,
OpenCL ICD, GMM and iHD shared libraries, catching missing ABI symbols even on a
runner without a GPU. Level Zero is not needed for this OpenCL workload.

The default `INTEL_COMPUTE_RUNTIME=pinned` build targets supported modern Intel GPUs,
including Raptor Lake. Older Intel generations removed from the newer upstream runtime
can retain Debian's OpenCL packages with
`docker build --build-arg INTEL_COMPUTE_RUNTIME=debian .`. That branch also requires
successful package configuration and shared-library checks; unknown selector values
fail the build. The former opt-in `=1` value is no longer accepted. This selector changes
container userspace packages, not the host kernel driver.

After successful package and ABI checks, the image build records a deterministic
SHA256 fingerprint of the selected source and actual installed NEO/IGC/GMM versions
in `/usr/local/share/dashcam/intel-runtime-cache-key`. GPU model caches use that key
alongside the OpenVINO version, so driver/compiler changes start a fresh cache without
deleting the previous cache. Normal verification remains read-only. OpenVINO 2025.4.1
intentionally permits compatible ZeBin cache reuse across driver versions; this extra
namespace ensures new compiler versions are exercised and does not imply those older
blobs are inherently invalid.

Upstream sources: [NEO 25.13 release](https://github.com/intel/compute-runtime/releases/tag/25.13.33276.16)
and [IGC 2.10.8 release](https://github.com/intel/intel-graphics-compiler/releases/tag/v2.10.8).
The package compatibility checks do not establish successful VAAPI decode or
sustained inference on physical hardware. Preserve the durable GPU-disable marker
through an image update; clear it only after separate device probes and stress
testing succeed. The application remains protected by its CPU fallback meanwhile.

## Publishing exactly what passed

`release.yml` calls the complete reusable CI workflow and cannot publish until backend,
frontend, Android, and Docker checks have all succeeded. CI builds the Linux image once,
runs inference and application smoke checks, verifies the privilege drop, and audits its
installed Python packages. Only release runs on `main` or a `v*` tag export that tested
image as a short-lived artifact. Pull requests do not export a publishable artifact and
CI has no package-write permission.

The publishing job downloads the artifact from the same workflow run and attempt. Before
logging into GHCR it verifies the archive checksum, the loaded image's immutable
ID against the CI output, and the source-revision label against the run's commit.
It then adds release tags and pushes that loaded image without another build. All pushed
tags are checked for the same registry manifest digest, which is used for provenance.
Both `v1.2.3` and semver aliases are published for a version tag so release-note pull
commands resolve correctly; `latest` is updated only for a version-tag release.

The source-revision label establishes the committed source used by CI. A local image
built from a dirty working tree is a local validation artifact, even if its label names
the checkout's baseline commit; it is not deployment proof.
