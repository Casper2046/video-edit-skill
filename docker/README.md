# Render engine image

A Linux image that turns the caller's edit packet into a finished 1080x1920 film and its QA report, on CPU, offline.
Its entrypoint is `scripts/render_packet.py` (SKILL.md, "Render engine"). Nothing is downloaded at run time: every
model, browser and script is fetched and checked when the image is built.

## What is in it

- `node:22-bookworm-slim` (pinned by digest), with ffmpeg, GNU `time`, Python 3.11, fonts and Chrome's shared
  libraries from apt.
- chrome-headless-shell, pinned per architecture:
  - amd64: Chrome for Testing `152.0.7977.30` (HyperFrames 0.8.34's own `CHROME_VERSION`), installed by
    `@puppeteer/browsers@3.2.6`;
  - arm64: Playwright `1.61.1`'s pinned linux-arm64 headless shell (Chrome for Testing has no arm64 build, and
    Debian's `chromium` crashes on arm64, HyperFrames issue #2039).
- `hyperframes@0.8.34`, installed globally. `VIDEO_EDIT_HYPERFRAMES` points the skill at it, so no `npx --yes`.
- The skill in `/opt/video-edit`: `scripts/`, `looks/`, `assets/template/` with the OFL fonts, `SKILL.md`.
- A venv in `/opt/video-edit/.venv` from `docker/requirements.lock`: setup.py's requirements and their dependencies,
  each pinned to a version and wheel sha256 (`pip install --require-hashes --no-deps`).
- faster-whisper `Systran/faster-whisper-small.en@d1d751a5…` and `Systran/faster-whisper-medium.en@a29b04bd…` in
  `HF_HOME=/opt/hf`, `model.bin` checked against its pinned sha256, `refs/main` set to the pinned revision.
  `HF_HUB_OFFLINE=1`.
- YuNet in `/opt/video-edit/.models` (faces.py's URL and sha256), GSAP 3.14.2 in `/opt/video-edit/vendor`
  (make_reel.py's sha256; `VIDEO_EDIT_GSAP` points at it).
- `/opt/video-edit/MANIFEST.json`: versions, revisions and sha256s of everything above, every skill file and every
  apt package.

Not in it: sound effects (their licence forbids redistribution, and an image is redistribution; the build fails if
any `.mp3` is present), tests, `.git`, a host `.platform.json` (the scripts find ffmpeg and node on PATH). The build
context is the repo root, cut down by `.dockerignore`.

The last build step runs with `--network=none` and proves the image works offline: both models load, YuNet opens,
GSAP vendors from the local copy, the venv passes setup.py's self-test and hyperframes is the pinned version.

It is derived from HyperFrames' `dist/docker/Dockerfile.render`. The header of `Dockerfile` lists what changed
and why.

## Build

From the repo root:

```sh
docker build --platform linux/arm64 -f docker/Dockerfile -t video-edit-render .
docker buildx build --platform linux/amd64 --load -f docker/Dockerfile -t video-edit-render:amd64 .
```

Optional: `--build-arg SKILL_COMMIT=$(git rev-parse HEAD)` records the commit in the manifest.

Read the manifest: `docker run --rm --entrypoint cat video-edit-render /opt/video-edit/MANIFEST.json`.

## Render a packet

Mount the packet and the staged shots read-only, give the run an output folder, and cut the network:

```sh
docker run --rm --network none \
  -v "$PACKET_DIR:/packet:ro" -v "$OUT:/out" \
  video-edit-render /packet/packet.json --shots /packet/shots --out /out/film [--look bold] [--platform reels]
```

The exit code is render_packet's:
- `0` every check passes;
- `1` a QA check failed (`/out/film/qa-report.json` and the `QA FAIL` line name it);
- `2` the packet was refused before any stage ran (a shot missing, mismatched or not decodable, narration or approval
  not as approved, a packet that is not JSON, a usage error);
- `3` a pipeline stage failed or crashed: its own exit code is printed on its `== <stage>: exit N` line, with the
  render's output when a render fails.

The film is in `/out/film/renders/`.

### What is checked on the render

The render stage renders the page twice: the film, then an overlays-only copy (no video or audio; captions and text
shadows tagged by colour) as RGBA PNG frames in `work/overlays/`. Its alpha is the overlay mask. `reel_check.py`
measures on the film's own frames and audio, with the mask saying where the overlays are: caption onset against the
speech onset on the render's audio (a failure names the sentences over the limit), caption presence (every caption in
`layout.json` shown within its planned span), the safe zone on every settled frame (an element's planned span minus
its fades) and face clearance (YuNet on every 3rd rendered frame; `faces.py` plans on every source frame plus 3px of
render headroom). Text-shadow halo and ink during fades (spill) are recorded as information, never failures.
`openings.json` (each shot's unclean-opening note) is copied into `qa-report.json` under `openings`.

`VIDEO_EDIT_HYPERFRAMES` (the installed hyperframes) and `VIDEO_EDIT_GSAP` (the vendored GSAP) are set in the image;
outside it the skill uses a hyperframes on PATH at the pinned version, else `npx --yes hyperframes@0.8.34`, and the
GSAP CDN, sha256-checked.

### Memory mode

HyperFrames switches to its low-memory profile (1 worker, screenshot capture) when the container sees 8 GiB
(8192 MiB) of memory or less, counting a cgroup limit. To force the normal profile, set
`PRODUCER_LOW_MEMORY_MODE=false` (`docker run -e PRODUCER_LOW_MEMORY_MODE=false ...`). A host with an 8 GiB memory
limit gets the low-memory profile unless it is set.

## Tests

The image holds no tests. From a checkout, with the skill's Python: `PY -m unittest discover -s tests` (synthetic only,
no network; `test_offline_image.py` covers this image's offline wiring, `test_render_packet.py` the entry point and
its exit codes, `test_render_checks.py` the render-measured checks). The suites on private labelled footage run only
when `VIDEO_EDIT_ONSET_FIXTURES` or `VIDEO_EDIT_OPENING_FIXTURES` points at a folder with its `labels.json`, and skip
otherwise.

## Measurements

Measured on arm64 (8 CPUs, 8 GiB Docker VM, CPU-only Chrome, `--network none`), as a lower bound for amd64:
- a 9-shot, 34s packet: 5:39 end to end, largest process 2.07 GB, container peak 5.31 GB (mostly page cache);
  the film render alone is about 2 min.
- HyperFrames uses 1 render worker at 8 GiB or less; `PRODUCER_LOW_MEMORY_MODE=false` allows more.
Measure real amd64 time and memory on the target host.
