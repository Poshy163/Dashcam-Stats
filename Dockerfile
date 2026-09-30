# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# ---------------------------------------------------------------------------------------
# Stage 1 — build the web UI
# ---------------------------------------------------------------------------------------
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` with no fallback, deliberately.
#
# The `|| npm install` this replaced turned the one check that catches a package.json /
# package-lock.json drift into a warning nobody sees: the release build fell through to
# `npm install`, resolved versions nobody had tested, and shipped them. If the lockfile is
# out of step the right outcome is a red build, not a quietly different bundle. The
# lockfile is committed, so the glob that made it optional was papering over the same gap.
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# ---------------------------------------------------------------------------------------
# Stage 2 — python dependencies
#
# Built separately so the (large, slow) wheel installation is not invalidated every time
# application code changes.
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS pydeps

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY backend/requirements-linux-py312.lock /tmp/requirements.lock
COPY backend/requirements-build.lock /tmp/bootstrap.lock
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /tmp/bootstrap.lock \
    && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
    && /opt/venv/bin/pip check


# ---------------------------------------------------------------------------------------
# Stage 3 — runtime
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS runtime

# The build stamp -- VERSION, VCS_REF, BUILD_DATE -- is deliberately NOT declared here.
# It lives at the very end of this stage instead. See the note above the LABEL there:
# consuming a build argument at the top invalidates every layer beneath it, and these
# three change on every single build.

# The non-free component carries the Intel iHD VAAPI driver, which is what Gen9+ iGPUs
# (including the Iris Xe in a 13th-gen i9) actually use for hardware decode.
#
# The components are added to the *existing* source entry rather than declared in a new
# .list file. Recent Debian images ship deb822 (`debian.sources`) with a `Signed-By` key,
# and a second entry for the same suite without that key makes apt abort with
# "Conflicting values set for option Signed-By" before it installs anything. Both source
# formats are handled so the build does not depend on which one the base image ships.
#
# Install both Intel and Mesa media drivers. CI checks that the Intel stack links, even
# without a GPU device, so a broken media dependency cannot silently ship.
#
# android-tools-adb is the control channel for the head-unit backup, and only that:
# connect, list the card, start a listener. The recordings themselves never pass through
# adbd, which caps at about 10 MB/s however many streams it is given -- they come over a
# plain socket at roughly 34. Nothing else is needed for it: the tar stream is unpacked in
# Python here, and the unit already ships toybox tar/nc/setsid/timeout.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/^Components:.*/Components: main contrib non-free non-free-firmware/' \
            /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's/^\(deb.*bookworm[^ ]*\) main.*$/\1 main contrib non-free non-free-firmware/' \
            /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libva2 \
        libva-drm2 \
        vainfo \
        i965-va-driver \
        mesa-va-drivers \
        ocl-icd-libopencl1 \
        clinfo \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        tini \
        gosu \
        curl \
        android-tools-adb; \
    apt-get install -y --no-install-recommends intel-media-va-driver-non-free; \
    rm -rf /var/lib/apt/lists/*

# A complete, hash-pinned Intel OpenCL stack compatible with Bookworm's libc/libstdc++.
# NEO 25.13 is the last upstream series built for Ubuntu 22.04; newer Ubuntu 24.04
# binaries require symbols Bookworm cannot provide. Keep its matched IGC/GMM versions
# together and check eager linking of both OpenCL and the existing iHD media driver.
# An unavailable archive, checksum mismatch or broken package must fail the build.
# Legacy Intel GPUs can explicitly retain Debian's older OpenCL package set.
ARG INTEL_COMPUTE_RUNTIME=pinned
COPY docker/intel-runtime.json /usr/local/share/dashcam/intel-runtime.json
COPY docker/install-intel-runtime.py /tmp/install-intel-runtime.py
COPY backend/scripts/check_intel_runtime.py /tmp/check_intel_runtime.py
RUN apt-get update \
    && python /tmp/install-intel-runtime.py /usr/local/share/dashcam/intel-runtime.json "${INTEL_COMPUTE_RUNTIME}" \
    && python /tmp/check_intel_runtime.py \
    && rm -rf /var/lib/apt/lists/* /tmp/install-intel-runtime.py /tmp/check_intel_runtime.py

COPY backend/requirements-build.lock /tmp/bootstrap.lock
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/bootstrap.lock
COPY --from=pydeps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DASHCAM_DATA_DIR=/data \
    DASHCAM_FOOTAGE_DIR=/dashcam \
    DASHCAM_PORT=8080

# LIBVA_DRIVER_NAME is deliberately NOT set here.
#
# It used to be pinned to `iHD`, with a comment claiming the entrypoint would override it on
# hardware that needed something else. It did not, and that mattered more than it looks:
# libva only probes the DRM driver and chooses a backend while the variable is *unset*, so a
# pinned value silently disables auto-detection everywhere. On AMD and pre-Gen8 Intel the
# image loaded a driver that could not initialise, every decode fell back to software, and
# the mesa-va-drivers and i965-va-driver packages installed above for exactly those hosts
# were unreachable -- while Settings reported `vaapi_driver: iHD`, naming a driver that had
# never loaded. `docker/entrypoint.sh` now reads the render node's PCI vendor and exports
# the right name, and leaves an operator-supplied value alone.

# Keep the ADB key on the data volume: the head unit authorises the key, so losing it on an
# image rebuild means the car has to be re-authorised by hand from its own screen.
ENV ANDROID_USER_HOME=/data/.android

WORKDIR /app
COPY backend/ /app/backend/
COPY --from=frontend /build/dist /app/frontend/dist
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
# Carriage returns stripped before the script is ever run, as well as being kept out of the
# checkout by .gitattributes. Belt and braces, because the two failures are not the same
# failure: .gitattributes fixes `git clone`, and this fixes everything else -- a source zip
# from the Releases page, an editor that rewrites on save, a `COPY` from a Windows host that
# never went through git at all. The cost is one sed; the symptom it prevents is
# `/usr/bin/env: 'bash\r': No such file or directory` and a container that exits 127 before
# a single line of the application runs.
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh \
    && chmod +x /usr/local/bin/entrypoint.sh

# Runs unprivileged. The entrypoint joins this account to whatever group owns the render
# node before dropping privileges, because that GID differs from host to host.
RUN useradd --system --create-home --uid 1000 --shell /usr/sbin/nologin dashcam \
    && mkdir -p /data /dashcam \
    && chown -R dashcam:dashcam /app /data

ENV PYTHONPATH=/app/backend

# Keep per-revision metadata below the expensive layers so source revisions can reuse
# their dependency cache. CI stamps the commit time and publishes the exact tested image;
# release promotion does not rebuild or alter these labels.
ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

ENV DASHCAM_VERSION=${VERSION}
ENV DASHCAM_SOURCE_REVISION=${VCS_REF}

LABEL org.opencontainers.image.title="Dashcam Analyser" \
      org.opencontainers.image.description="Self-hosted dashcam footage analysis: telemetry, vehicle and licence-plate detection, journeys and maps" \
      org.opencontainers.image.source="https://github.com/Poshy163/Dashcam-Stats" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${DASHCAM_PORT}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
