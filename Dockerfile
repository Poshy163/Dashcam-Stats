# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------------------
# Stage 1 — build the web UI
# ---------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS frontend

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
FROM python:3.12-slim-bookworm AS pydeps

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt


# ---------------------------------------------------------------------------------------
# Stage 3 — runtime
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

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
# The Intel media driver is installed separately and allowed to fail: it is preferred on
# Intel hardware, but on an AMD iGPU or a host with no GPU at all mesa-va-drivers already
# covers decode, and failing the whole image build over a driver the machine may never use
# would be the wrong trade.
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
        intel-opencl-icd \
        ocl-icd-libopencl1 \
        clinfo \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        tini \
        gosu \
        curl \
        android-tools-adb; \
    apt-get install -y --no-install-recommends intel-media-va-driver-non-free \
        || apt-get install -y --no-install-recommends intel-media-va-driver \
        || echo 'WARNING: no Intel media driver available; VAAPI will fall back to Mesa'; \
    rm -rf /var/lib/apt/lists/*

# Intel's own compute-runtime, off by default -- because it was tried, and it lost VAAPI.
#
# The reasoning for trying it was sound. The deployment's iGPU aborts inside
# ``shared/source/os_interface/linux/drm_buffer_object.cpp``, which belongs to
# intel/compute-runtime, and Bookworm ships that from 2022 against a Raptor Lake chip.
# Every other variable had been changed without effect: VAAPI separated from OpenVINO,
# concurrency cut to one worker, OpenVINO pinned back from 2026.3 to 2025.4.1. All three
# still produced the identical ``clFlush -5 CL_OUT_OF_RESOURCES``.
#
# Measured on the deployment with NEO 26.27.39122.11 and IGC 2.38.2 installed:
#
#     openvino_devices : ["CPU"]        (GPU gone entirely)
#     vaapi_available  : false          (was true)
#     hardware_decode  : false          (was true)
#
# The render node and i915 were untouched, so this is not the hardware disappearing. NEO
# 26.27 requires gmmlib 22.10, and ``dpkg -i`` of Intel's ``libigdgmm12`` replaces the one
# Bookworm's iHD media driver was built against -- so the media driver stops loading and
# VAAPI goes with it. The OpenCL device did not come back either. Strictly worse than the
# old driver, which at least decoded.
#
# Left here because the diagnosis still points at this layer, and because doing it properly
# means bringing the *media* driver forward at the same time rather than half of a matched
# set. Enable with --build-arg INTEL_COMPUTE_RUNTIME=1 once that is worked out; leave it
# off and Bookworm's driver stays, which is the configuration that decodes.
ARG INTEL_COMPUTE_RUNTIME=
ARG NEO_VERSION=26.27.39122.11
ARG IGC_VERSION=2.38.2
# The IGC filenames carry a build number after a '+', which has to be %2B in the URL.
ARG IGC_BUILD=22051
ARG GMMLIB_VERSION=22.10.0
RUN set -eu; \
    if [ -z "${INTEL_COMPUTE_RUNTIME}" ]; then \
        echo "keeping Debian's Intel driver (INTEL_COMPUTE_RUNTIME unset)"; \
    else \
        neo="https://github.com/intel/compute-runtime/releases/download/${NEO_VERSION}"; \
        igc="https://github.com/intel/intel-graphics-compiler/releases/download/v${IGC_VERSION}"; \
        ver="${IGC_VERSION}%2B${IGC_BUILD}"; \
        tmp="$(mktemp -d)"; \
        if cd "$tmp" \
            && curl -fsSL -o igc-core.deb "${igc}/intel-igc-core-2_${ver}_amd64.deb" \
            && curl -fsSL -o igc-opencl.deb "${igc}/intel-igc-opencl-2_${ver}_amd64.deb" \
            && curl -fsSL -o gmmlib.deb "${neo}/libigdgmm12_${GMMLIB_VERSION}_amd64.deb" \
            && curl -fsSL -o icd.deb "${neo}/intel-opencl-icd_${NEO_VERSION}-0_amd64.deb" \
            && dpkg -i gmmlib.deb igc-core.deb igc-opencl.deb icd.deb; \
        then \
            echo "installed Intel compute-runtime ${NEO_VERSION} with IGC ${IGC_VERSION}"; \
        else \
            echo "WARNING: could not install Intel compute-runtime ${NEO_VERSION}"; \
            dpkg --configure -a || true; \
        fi; \
        rm -rf "$tmp"; \
    fi; \
    rm -rf /var/lib/apt/lists/*

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
RUN chmod +x /usr/local/bin/entrypoint.sh

# Runs unprivileged. The entrypoint joins this account to whatever group owns the render
# node before dropping privileges, because that GID differs from host to host.
RUN useradd --system --create-home --uid 1000 --shell /usr/sbin/nologin dashcam \
    && mkdir -p /data /dashcam \
    && chown -R dashcam:dashcam /app /data

ENV PYTHONPATH=/app/backend

# The build stamp, last, because it is the only thing in this file that changes on every
# build -- and a layer that changes invalidates every layer beneath it.
#
# It used to sit at the top of this stage. CI builds the image twice, once to test it and
# once to publish it, and the two pass different values: `ci-<sha>` against `main`, plus a
# BUILD_DATE that is a fresh timestamp every run. So the publish build shared no cached
# layer with the test build that had just finished -- it re-ran the apt install, re-fetched
# the Intel compute runtime, and rebuilt the venv, about ninety seconds of work that had
# been done minutes earlier. It also meant no build ever reused the previous one's cache,
# because BUILD_DATE alone guaranteed a miss on the second instruction.
#
# Down here the same three values land after everything expensive, so changing them costs
# one metadata layer. Labels and environment do not care where they are declared; the
# cache does.
ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

ENV DASHCAM_VERSION=${VERSION}

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
