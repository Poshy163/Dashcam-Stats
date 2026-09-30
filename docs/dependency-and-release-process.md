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
