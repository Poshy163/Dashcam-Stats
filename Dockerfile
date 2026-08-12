# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------------------
# Stage 1 — build the web UI
# ---------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
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

ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Dashcam Analyser" \
      org.opencontainers.image.description="Self-hosted dashcam footage analysis: telemetry, vehicle and licence-plate detection, journeys and maps" \
      org.opencontainers.image.source="https://github.com/Poshy163/Dashcam-Stats" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

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

# Intel's own compute-runtime, replacing Debian's.
#
# This is the layer the deployment's iGPU actually aborts in. The abort names
# ``shared/source/os_interface/linux/drm_buffer_object.cpp``, which belongs to
# intel/compute-runtime -- and Bookworm ships that from 2022, against a Raptor Lake iGPU
# and a 2025 OpenVINO. Every other variable has now been changed without effect: VAAPI and
# OpenVINO were separated, concurrency was cut to one worker, and OpenVINO was pinned back
# from 2026.3 to 2025.4.1. All three still produced the identical
# ``clFlush -5 CL_OUT_OF_RESOURCES``. The driver is what is left.
#
# OpenCL specifically, because that is what fails: the traceback is ``ocl_stream.cpp`` and
# the call is ``clFlush``. Level Zero is deliberately not installed -- it would pull the
# loader in behind it and is not on the path that breaks.
#
# Pinned to a version pair Intel publishes together, and allowed to fail. If the downloads
# or the install do not work the Debian packages above stay exactly as they were, and the
# app already falls back to CPU inference on its own. An image that will not build is a
# worse outcome than an old driver.
ARG NEO_VERSION=26.27.39122.11
ARG IGC_VERSION=2.38.2
# The IGC filenames carry a build number after a '+', which has to be %2B in the URL.
ARG IGC_BUILD=22051
ARG GMMLIB_VERSION=22.10.0
RUN set -eu; \
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
        echo "WARNING: could not install Intel compute-runtime ${NEO_VERSION};" \
             "keeping Debian's driver. GPU inference may abort; the app falls back to CPU."; \
        dpkg --configure -a || true; \
    fi; \
    rm -rf "$tmp" /var/lib/apt/lists/*

COPY --from=pydeps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DASHCAM_DATA_DIR=/data \
    DASHCAM_FOOTAGE_DIR=/dashcam \
    DASHCAM_PORT=8080 \
    DASHCAM_VERSION=${VERSION} \
    # iHD is the right driver for modern Intel; the entrypoint overrides this if the
    # detected hardware needs the older i965 or a Mesa driver instead.
    LIBVA_DRIVER_NAME=iHD

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

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${DASHCAM_PORT}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
