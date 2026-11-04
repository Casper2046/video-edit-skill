# video-edit render engine image: the caller's edit packet in, finished film and QA report out, offline.
#
# Build from the repo root (the context is the repo; .dockerignore keeps it to what a packet run needs):
#   docker build --platform linux/arm64 -f docker/Dockerfile -t video-edit-render .
#
# Derived from HyperFrames 0.8.34 dist/docker/Dockerfile.render. Changes from it:
#   - hyperframes is pinned (0.8.34), not `latest`.
#   - amd64 chrome-headless-shell is pinned to 152.0.7977.30 (HyperFrames
#     0.8.34's own CHROME_VERSION), not `@stable`, installed by a pinned
#     @puppeteer/browsers CLI.
#   - Debian `chromium` is not installed: it is unpinned and SIGTRAPs on arm64
#     (HyperFrames #2039), and nothing uses it once PRODUCER_HEADLESS_SHELL_PATH
#     points at the pinned headless shell.
#   - PRODUCER_HEADLESS_SHELL_PATH is a plain ENV (stable symlink), so any
#     `hyperframes render` invocation uses the pinned shell, not only a wrapper.
#   - `time` is installed for /usr/bin/time -v peak-memory measurement.
#   - Telemetry is off; renders run with --network none.
# Added for the skill: Python 3.11 venv (docker/requirements.lock, hash-pinned),
# the faster-whisper models at pinned revisions with HF_HUB_OFFLINE=1, YuNet,
# GSAP 3.14.2, the template fonts, and /opt/video-edit/MANIFEST.json.
# Never add sound-effect files to this image: their licence forbids it.

# node:22-bookworm-slim multi-arch index digest, pulled 2026-11-04.
ARG BASE_IMAGE=node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG HYPERFRAMES_VERSION=0.8.34
# Set by BuildKit from --platform.
ARG TARGETARCH
# arm64: Playwright's pinned linux-arm64 headless shell (chrome-for-testing
# publishes no linux-arm64 build).
ARG PLAYWRIGHT_VERSION=1.61.1
# amd64: chrome-for-testing headless shell, HyperFrames 0.8.34's CHROME_VERSION.
ARG CHROME_HEADLESS_SHELL_VERSION=152.0.7977.30
ARG PUPPETEER_BROWSERS_VERSION=3.2.6

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip ffmpeg time python3 python3-venv \
    libgbm1 libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
    libxdamage1 libxrandr2 libcups2 libasound2 libpangocairo-1.0-0 \
    libxshmfence1 libgtk-3-0 \
    fonts-liberation fonts-noto-color-emoji fonts-noto-cjk fonts-noto-core \
    fonts-noto-extra fonts-noto-ui-core fonts-freefont-ttf fonts-dejavu-core \
    fontconfig \
    && rm -rf /var/lib/apt/lists/* && apt-get clean && fc-cache -fv

ENV CONTAINER=true \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    HYPERFRAMES_NO_TELEMETRY=1

RUN set -eu; \
    if [ "$TARGETARCH" = "amd64" ]; then \
      npx --yes "@puppeteer/browsers@${PUPPETEER_BROWSERS_VERSION}" install \
        "chrome-headless-shell@${CHROME_HEADLESS_SHELL_VERSION}" --path /opt/puppeteer; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
      npx --yes "playwright-core@${PLAYWRIGHT_VERSION}" install chromium-headless-shell; \
    else \
      echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1; \
    fi; \
    SHELL_PATH=$(find /opt/puppeteer /opt/ms-playwright \
      \( -name chrome-headless-shell -o -name headless_shell \) -type f 2>/dev/null | head -1); \
    if [ -z "$SHELL_PATH" ]; then \
      echo "chrome-headless-shell not found for ${TARGETARCH}" >&2; exit 1; \
    fi; \
    if ldd "$SHELL_PATH" | grep -q "not found"; then \
      ldd "$SHELL_PATH" | grep "not found" >&2; exit 1; \
    fi; \
    mkdir -p /opt/chrome; ln -s "$SHELL_PATH" /opt/chrome/headless-shell; \
    ("$SHELL_PATH" --version || echo "version: unknown") > /opt/chrome/VERSION; \
    echo "$SHELL_PATH" >> /opt/chrome/VERSION; \
    rm -rf /root/.npm

ENV PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome/headless-shell \
    PUPPETEER_EXECUTABLE_PATH=/opt/chrome/headless-shell

RUN npm install -g "hyperframes@${HYPERFRAMES_VERSION}" && rm -rf /root/.npm \
    && hyperframes --version

# The skill lives in /opt/video-edit, its venv in /opt/video-edit/.venv (where skillenv looks for it).
ENV SK=/opt/video-edit \
    HF_HOME=/opt/hf

COPY docker/requirements.lock /opt/video-edit/docker/requirements.lock
RUN python3 -m venv /opt/video-edit/.venv \
    && /opt/video-edit/.venv/bin/pip install --no-cache-dir --require-hashes --no-deps \
       --report /opt/video-edit/docker/pip-report.json -r /opt/video-edit/docker/requirements.lock

# The models are the largest layer: fetched before the skill code, so a code change does not fetch them again.
COPY docker/image_assets.py /opt/video-edit/docker/image_assets.py
RUN HF_HUB_DISABLE_XET=1 /opt/video-edit/.venv/bin/python /opt/video-edit/docker/image_assets.py models \
    && rm -rf /opt/hf/xet /opt/hf/hub/.locks

COPY . /opt/video-edit
RUN /opt/video-edit/.venv/bin/python /opt/video-edit/docker/image_assets.py assets

ARG SKILL_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV HF_HUB_OFFLINE=1 \
    VIDEO_EDIT_HYPERFRAMES=/usr/local/bin/hyperframes \
    VIDEO_EDIT_GSAP=/opt/video-edit/vendor/gsap.min.js

# Proof at build time that a run needs no network, then the manifest. No .mp3 may be anywhere in the image.
RUN --network=none set -eu; \
    /opt/video-edit/.venv/bin/python /opt/video-edit/docker/image_assets.py verify; \
    if find / -xdev -name '*.mp3' | grep -q .; then find / -xdev -name '*.mp3' >&2; exit 1; fi; \
    BASE_IMAGE="$BASE_IMAGE" SKILL_COMMIT="$SKILL_COMMIT" BUILD_DATE="$BUILD_DATE" \
    PLAYWRIGHT_VERSION="$PLAYWRIGHT_VERSION" CHROME_HEADLESS_SHELL_VERSION="$CHROME_HEADLESS_SHELL_VERSION" \
    PUPPETEER_BROWSERS_VERSION="$PUPPETEER_BROWSERS_VERSION" \
    /opt/video-edit/.venv/bin/python /opt/video-edit/docker/image_assets.py manifest > /opt/video-edit/MANIFEST.json

WORKDIR /work

# render_packet.py <packet.json> --shots DIR --out DIR [--look L] [--platform P]
ENTRYPOINT ["/opt/video-edit/.venv/bin/python", "/opt/video-edit/scripts/render_packet.py"]
