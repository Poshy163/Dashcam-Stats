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

# non-free-firmware carries the Intel iHD VAAPI driver, which is what Gen9+ iGPUs
# (including the Iris Xe in a 13th-gen i9) actually use for hardware decode.
RUN echo 'deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware' \
        > /etc/apt/sources.list.d/nonfree.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libva2 \
        libva-drm2 \
        vainfo \
        intel-media-va-driver-non-free \
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
    && rm -rf /var/lib/apt/lists/*

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
