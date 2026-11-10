---
name: video-edit
description: "Turns raw 9:16 talking-head clips into a finished, premium Instagram reel end to end: picks the cleanest take of every line across your clips, cuts dead air, punches in from 4K, cuts you out of the background for words behind your head, applies one of three designed looks (bold kinetic words, cool girl, cool dude), builds animated UI inserts with parallel sub-agents, adds subtle sound design, and checks every cut frame by frame inside the Reels safe zone. Trigger with /video-edit or phrases like 'edit these clips', 'I dropped clips in my downloads', 'cut this into a reel', 'make this reel look fire'."
---

# /video-edit

Raw clips in, finished reel out. The user never opens an editor: they review a phone copy and give notes; every
round is a ~2 minute re-render.

Read before building: `references/looks.md` (the three looks, exact values), `references/layout.md` (safe zone,
placement, behind-head formula), `references/gotchas.md` (every trap already hit once). Read
`references/overlays.md` when the script calls for an animated insert.

Works on macOS, Linux and Windows. setup.py detects the machine at the start of every run and every command below
comes from that record, so never guess paths or shells: see "This machine" right after Step 0.

## Step 0: First-time setup (runs once)

`SK` is this skill's folder (the one holding this SKILL.md), as a full path. Usually `~/.claude/skills/video-edit`;
on Windows that is `C:/Users/<name>/.claude/skills/video-edit`.

Look for `SK/config.json`.

**If it exists:** load it (name, instagram handle, default look, folders), run the setup script exactly as in
step 3 below (a few seconds: it only checks, and repairs the environment after an update), read "This machine",
and go to Step 1 once it prints READY.

**If it does not exist:**
1. Say: "Welcome to /video-edit! Let's get you set up. This takes about 2 minutes and only happens once."
2. Ask, in one message:
   - Your name (used on your Instagram profile insert)
   - Your Instagram handle (for your profile insert and to pull your reels for the orbit effect)
   - Default look: **bold** (kinetic words behind your head), **cool girl** (warm, yellow serif italic, your reels
     orbiting you) or **cool dude** (dark editorial, tracked caps + serif). Offer bold if they are unsure.
   - Where your raw clips usually land (default `~/Downloads`) and where projects should live (default `~/reel-edits`)
3. Run the setup script with whichever Python the machine has. Try these in order until one prints STATUS lines:
   `python3 "SK/scripts/setup.py"`, then `python "SK/scripts/setup.py"`, then `py -3 "SK/scripts/setup.py"`.
   (On Windows `python3` is often a Microsoft Store stub that prints "Python was not found": just try the next one.)
   None of them runs: Python is missing. Give the fix for their system and stop (Windows:
   `winget install --id Python.Python.3.12 -e`, Mac: `brew install python`, Linux: `sudo apt install python3 python3-venv`).
   Then read the STATUS lines:
   - ffmpeg / node / python venv MISSING: the line ends with the exact fix for THIS machine (winget on Windows,
     brew on Mac, apt on Linux). Give it to the user and stop until the script prints READY.
   - Windows: the user pastes the winget lines into their own PowerShell window (winget asks them to agree to its
     terms the first time), then quits Claude Code completely and reopens it, or the new programs are not found.
   - The script downloads the 6 sound effects itself. If it still reports them MISSING (offline), that is optional:
     the reel just has no SFX until they are added.
4. Optional, Apify (pulls reels and profile stats automatically): try a simple Apify MCP call. If it is not
   connected, say it is optional and how to add it:
   1. Create a free account at https://apify.com and copy the API token from Settings > API & Integrations.
   2. In a terminal, paste this one line with the token in place of YOUR_TOKEN:
      `claude mcp add apify -e APIFY_TOKEN=YOUR_TOKEN -- npx -y @apify/actors-mcp-server`
      (heads up: Claude Code stores the token in plain text in `~/.claude.json`; keep that file private).
   3. Reopen Claude and run /video-edit again.
   Without Apify, the skill asks them to drop their reel files into the project instead.
5. Save `SK/config.json` (no secrets, ever):
   ```json
   {"name": "...", "instagram": "handle-without-@", "defaultLook": "bold", "clipsDir": "~/Downloads",
    "projectsDir": "~/reel-edits", "setupComplete": true, "setupDate": "YYYY-MM-DD"}
   ```
6. Say: "Setup complete! Running /video-edit now..." and continue.

## This machine (read on every run)

Read `SK/.platform.json`, written by setup.py. It records the OS (`os`: macos, linux or windows), the CPU and the
exact commands for this computer:
- `PY` = its `python` value: the skill's own Python (`.venv/bin/python` on Mac and Linux,
  `.venv/Scripts/python.exe` on Windows). Run EVERY Python script of this skill with it, never a bare `python3`.
- `HF` = `PY "SK/scripts/hf.py"`: starts the pinned HyperFrames (`npx --yes hyperframes@0.8.34 ...`) with the
  right Node on any OS. No `nvm use`, no `npx.cmd` trouble.
- `cutout`: `CoreML` (Apple Silicon) or `CPU` (Windows, Linux, Intel Macs: no GPU help, so the cutout step is the
  slow one; say so, run it in the background and keep building).

setup.py rewrites this file on every run (Step 0), so it is always current. File missing, or `PY` no longer
exists: setup did not run or did not reach READY; run it as in Step 0.3 and follow its fix.

One command style that works in zsh, bash, Git Bash and PowerShell:
- Full paths in double quotes with forward slashes (fine on Windows too): `"PY" "SK/scripts/ingest.py" "<project>"`.
- In PowerShell (Windows without Git for Windows, or whenever the PowerShell tool is the one you have) put `&` in
  front: `& "PY" "SK/scripts/ingest.py" "<project>"`.
- One command per call. No `&&`, no `VAR=1 command`, no `~` inside arguments, no `source`, no `zsh`.
- `ffmpeg` and `ffprobe` are on PATH on every OS (setup checks it), so one-off ffmpeg commands are the same everywhere.

## Spec mode (generated shots + a reel spec, no hand edits)

For shots that already say their script (AI-generated talking shots, one narration per shot), skip take picking and
the hand-adapted template: describe the reel once in a **reel spec** and run one command. Start from
`examples/reel.example.json`:
- `shots[]`: `file`, `narration` (the caption truth: words, spelling, quotes), optional `caption_top` (y of the first
  caption line; without it the `faces` stage samples every frame, takes the lowest face bottom (YuNet box, or 60px
  under a landmark chin estimate if lower) and starts captions 23px under it (20px margin, 3px render headroom), never above the chest band y940, always
  leaving one line above the safe bottom: an explicit value wins), optional `clean_from` (seconds: the shot's first
  clean frame; the first in point moves there when no speech is lost, else the segment carries an "opening" note).
- `provenance` (optional): where the shots and script come from (episode, production, approval, a passed review, each
  shot's narration and sha256). When present, `reel_check.py` checks it and copies it into the QA report; a pipeline
  that may only cut approved episodes passes `--require-provenance` to `make_reel.py`.
- `sentences[]` (optional, one per narration sentence, in order): `id` (for cards), `hold` (seconds kept after the line
  for an exercise beat), `chunks` (`[[word count, "sm" | "md" | "big" + " y" for the accent], ...]`; without it a
  default chunker runs).
- `cards`: `steps[]` {from, to, text}; `chips[]` {until, items[] {sentence, word | offset, nth, text, accent}};
  `message` {from, until, label, text, top}; `end` {lines[[text, class]], top, hold}. `until: "end"` = end-card start.
- `look`: a pack in `looks/` (`bold`, `cool-girl`). `platform`: `reels`, `tiktok` or `shorts` (safe zones in layout.md).
- `"sfx": false`: no sound effects (none are copied into the project or placed on the timeline). The render engine
  always sets it.
- `"one_take": true`: each shot is one take: its sentences join in the pause between them (nothing inside a shot is
  cut out) and the look's cut punch (the `#stage` zoom) is off. The render engine always sets it (generated shots).

```bash
PY SK/scripts/make_reel.py <reel.json> --out <project>                      # all stages
PY SK/scripts/make_reel.py <reel.json> --out <project> --from build --look cool-girl --platform tiktok
```
Stages: ingest (small.en per shot, onset-corrected) -> faces (YuNet face box on every frame, CPU; the 232 KB model
downloads once into `SK/.models`, sha256-pinned) -> edl (one segment per sentence) -> assemble (+ end-card hold)
-> transcribe (medium.en on the cut: caption timing) -> build (index.html, layout.json, build-report.json) -> render
-> check (cut sheet, phone copy, caption sheet, `reel_check.py` on the film itself: caption onset vs its own speech-onset
finder, caption presence, audio and picture sync per segment, speech through cuts, chunk holds, peak level, safe zone,
face clearance, expected text, render warnings; exit 1 on a failed check, 3 when a stage fails). Outputs land in `<project>/renders/<slug>-<look>-<platform>*`. A spec mistake
(chunk count, unknown sentence id, a chip word that is not said) stops the build with the sentence named.
Read `build-report.json` warnings: a caption block that had to move up to fit the platform may cover the chin.
Spec mode does not use the cutout (no behind-head words yet); shots need headroom (head top below y400) first.
Tests: `PY SK/tests/test_spec_mode.py`.

## Render engine (an edit packet in, a film and its QA report out)

For a production system that hands over an approved episode, not a reel spec:
```bash
PY SK/scripts/render_packet.py <packet.json> --shots <dir> --out <dir> [--look bold] [--platform reels]
```
- Packet: `{shots: [{narration}], provenance: {episodeId, productionId, ..., creativeReview, approval, shots:
  [{operationId, sha256, narration, url, ...}]}}`. `--shots` holds the staged shot files; each is matched by its sha256
  against `provenance.shots[i].sha256`. `url` is never fetched.
- It writes `<out>/packet.reel.json` (shots in packet order, provenance copied in, `"sfx": false`, `"one_take": true`, no `sentences` or
  `caption_top`, a shot's `clean_from` where `openings.py` finds an unclean opening), `<out>/openings.json` (every
  shot's opening note; `reel_check.py` copies it into `qa-report.json` under `openings` and prints a line for each shot
  whose `clean_from` was not found or not applied) and `<out>/expected-text.txt`, then runs
  `make_reel.py --require-provenance --expect-text` into `<out>`.
- Exit codes (render_packet.py and make_reel.py):
  - `0` every check passes;
  - `1` a QA check failed (`reel_check.py`): `<out>/qa-report.json` and the printed `QA FAIL` line name the check and
    its shot, segment or element;
  - `2` refused before any stage runs: a shot missing, ambiguous, with another sha256, or matching its sha256 but not
    decodable; narration differing from the approved one; no passed review or approval; a packet that is not JSON; a
    usage error;
  - `3` a pipeline stage failed or something crashed: the stage's own exit code is printed on its `== <stage>: exit N`
    line, with a Python traceback, or the render's output when a render fails (a signal is 3 too). No retry.
- Film: `<out>/renders/<slug>-<look>-<platform>.mp4`, 1080x1920, with its `-qa.json`, sheets and page copy beside it.

Planned mode (a packet with `graphics`, SPEC v1.3): `render_packet.py` hands it to `render_planned.py`. `--shots` holds the
takes and pictures, each matched by sha256. Every graphic's cue (line id + word index range) is checked against its line and
resolved on the render transcript; a missing or ambiguous cue, or a hold past its take, refuses (exit 2) naming the graphic id.
The film is built from the pinned kit (`kits/<kit>/`, HyperFrames 0.9.23, GSAP 3.15.0, 24 fps) and `<out>/evidence/` gets the
resolved timeline, the timing table (each graphic's measured first/last frame against its rule), the frame-per-word sheet and
the cue-frame sheet.

Checks measured on the render: the `render` stage renders the page twice, the film and then an overlays-only copy
(`work/overlays.html`: no video or audio, captions tagged one colour, text shadows another) as an RGBA PNG sequence in
`work/overlays/` (`overlay_mask.py`). Its alpha is the overlay mask: per frame, the boxes of every overlay pixel (ink),
the captions, and the text shadows. From it, on the film's own frames and audio:
- caption onset: the first frame showing each sentence's caption against the speech onset found on the render's
  audio; a failure names every sentence over the limit, worst first;
- caption presence: every caption element in `layout.json` shows on some frame within its planned span; one never
  shown fails, named with its sentence;
- safe zone: ink inside the platform's safe zone on every settled frame (an element's planned span minus its fades);
- face clearance: YuNet on every 3rd rendered frame (`FACE_STEP`), settled caption ink 20px under the face found on
  that frame and never above y940. The planner (`faces.py`) samples every source frame and adds 3px
  (`RENDER_HEADROOM`) because the rendered faces sit up to 3px lower; the checker samples the render.
- Information, never a failure: a text shadow (halo) past the line, and ink past it only during a fade (spill).
`layout.json` only names which element a rendered box belongs to and when it is planned.

Environment: `VIDEO_EDIT_HYPERFRAMES` names an installed hyperframes binary (the render image sets it; otherwise a
hyperframes on PATH at the pinned version, else `npx --yes hyperframes@0.8.34`); `VIDEO_EDIT_GSAP` names a local
GSAP 3.14.2 (sha256-checked; otherwise the CDN). Planned mode: `VIDEO_EDIT_HYPERFRAMES_PLANNED` names a hyperframes 0.9.23
binary (otherwise `npx --yes hyperframes@0.9.23`, which needs network) and `VIDEO_EDIT_GSAP_PLANNED` a local GSAP 3.15.0.

Tests: `PY -m unittest discover -s SK/tests` (synthetic only, no network). `test_render_packet.py` (packet entry point
and exit codes), `test_render_planned.py` (planned mode: cues, refusals, timing table), `test_render_checks.py` (overlay mask, onset, presence, safe zone, face clearance, openings in the
report), `test_openings.py` (unclean openings), `test_offline_image.py` (the image's offline wiring), `test_spec_mode.py`
and `test_platform.py`. Two suites also run against private labelled footage when pointed at it, and skip otherwise:
`VIDEO_EDIT_ONSET_FIXTURES` (a folder of `cN.wav` + `labels.json` with speech-onset labels) and
`VIDEO_EDIT_OPENING_FIXTURES` (a folder of shots + `labels.json` with `clean_from` labels).

In a container: `docker/Dockerfile` builds the offline image (pinned models, YuNet, GSAP, hyperframes, Chrome; no sound
effects), whose entrypoint is `render_packet.py`. Run it with `--network none`, the packet and shots mounted
read-only. Usage, the manifest and the memory note are in `docker/README.md`. HyperFrames renders with 1 worker
(its low-memory profile) when the container has 8 GiB of memory or less; `PRODUCER_LOW_MEMORY_MODE=false` turns that off.

## Hard rules
1. Everything inside the Reels safe zone: top 220, bottom 450 (nothing below y 1470), sides 35, right 100 from y 1155.
2. Never cover the face with text or cards. Behind-head words show at least ~70% of each letter.
3. Captions say what was actually said: fix Whisper's mishearings of names and keywords ("Claude" often comes out
   as "cloud"/"claws", "skill" as "scale"). No em dashes in on-screen text.
4. Sound effects subtle (template `SFX_GAIN = 0.75` of the base volumes), never louder than the voice.
5. Credit other creators on screen (@handle) whenever their reels appear. Real numbers on the user's own profile;
   anything else (commenters, chat history) is a generic placeholder, never a real person, never fabricated proof.
6. Prove cuts from the final render, not from snapshots.
7. Never delete the user's files. Originals are read, never modified.

## Workflow

### 1. Intake
- Clips: the files the user names, else the newest video files in `clipsDir`. Create `<projectsDir>/<short-slug>/`.
  (`~` in config.json is the user's home folder, `C:/Users/<name>` on Windows. The skill's scripts expand it.)
- Reference links (IG reels, Pinterest pins) for a style: download them and look at frames before designing anything
  (see overlays.md for how to fetch them).

### 2. Ingest
```bash
PY SK/scripts/ingest.py <project> <clips or folder> [--newest 5]
```
Look at `work/sheets.jpg` (setups and angles) and read `transcript.txt` (per-clip lines, words with times, speech map).

### 3. Pick takes -> `<project>/edl.json`
- Rebuild the script from the takes: people repeat a line until it lands. Use the LAST complete clean take unless
  the user says otherwise ("the better section starts at 0:47" means one continuous take from there).
- In/out points from the speech map: start ~0.05-0.07s before the first word, end at the silence start after the
  last word. Kill dead air between sections; confirm short pauses with silencedetect before trimming (Whisper can
  invent a leading word at a cut).
- Two segments in a row from the same angle: punch one in (zoom 1.3-1.9 from the 4K original, cx/cy on the chest).
- Unsure words (keyword, CTA trigger): re-transcribe that snippet with medium.en and a prompt containing the names.
- Format: `[{"id": "hook", "clip": "c1", "in": 12.66, "out": 15.72, "zoom": 1.0, "cx": 0.5, "cy": 0.5, "line": "..."}]`
- Post the EDL to the user as a short table (line, clip, look per section) and keep going; do not wait.

### 4. Assemble, transcribe, cut out
```bash
PY SK/scripts/assemble.py <project>                 # aroll.mp4 clean 30fps CFR + segments.json with exact frames
PY SK/scripts/transcribe_cut.py <project> "<names and keywords in the script>"
PY SK/scripts/cutout.py <project>                   # run in background; asserts cutout frames == aroll frames
PY SK/scripts/headpos.py <project>                  # after the cutout: head/body position per segment
```
One cutout at a time (about 1.5 GB of RAM). It prints a progress line every 30 seconds.

### 5. Inserts in parallel (as soon as the EDL is locked)
For each animated insert the script calls for (an app on screen, the profile follow on the CTA, a file scroll),
spawn a sub-agent in the background with the brief template in `references/overlays.md`: fixed size, fixed
duration, beats locked to the word times, transparent webm out. Keep building the main edit while they work.

### 6. Build
Copy `SK/assets/template/` into the project (build.py, hyperframes.json, package.json, assets/fonts, assets/sfx),
then adapt build.py. It is the finished build of a real reel: keep the machinery, rewrite the content (its
docstring lists which is which).
- Which look where: `defaultLook` for the whole reel; a line that names a look switches its section.
- Orbit and profile grid use the user's own reels (overlays.md: Apify post scraper with their handle, or files they
  drop into `assets/reels/`).
- Iterate from the project folder: `HF lint` (0 errors), then `PY build.py --safe`, then
  `HF snapshot --at <10-20 moments> --no-end --describe false`. Read the contact sheets, fix layout, readability
  and safe zone. (`HF` picks the right Node by itself, so there is no `nvm use`.)
  A build.py copied from an older version of this skill ignores `--safe`: if the red guide is missing from the
  snapshots, build that project the old way, `SAFE=1 "PY" build.py` (such projects only exist on Mac and Linux).

### 7. Render and QA
```bash
PY build.py           # without --safe
HF render -o renders/<slug>-vN.mp4 --quality high      # ~1.5-2 min for a 25s reel on Apple Silicon
PY SK/scripts/check_cuts.py <project> renders/<slug>-vN.mp4 --phone
```
Look at `work/cut_check.jpg`: every after-cut frame shows the new shot cleanly. Check audio peaks (`volumedetect`,
max around -2 to -5 dB) and the silence at section joins.

### 8. Deliver
Send or point to the phone copy (720p) and the full-quality render. Report in plain terms what changed and anything
that needs the user's call. Keep every version in `renders/`; back up `assets/` + `segments.json` to `work/vN/` before
re-assembling.

## Defaults that came out of real feedback (do not re-litigate)
- Change the grade and captions to create a look; do not replace the background with an AI-generated room.
- No film grain. Dark is fine, but keep the subject lit and skin from going red.
- Lead-in captions centred in front of the speaker read better than lines pinned to the left edge.
- Words behind the head are the signature move, but only when readable.

---
Built by [@tenfoldmarc](https://instagram.com/tenfoldmarc). Follow for daily AI automation builds, real systems, not theory.
