#!/usr/bin/env python3
"""Spec mode QA: measure a built reel against the audio and the spec. Exit 1 when any check fails, 3 on a crash.

Usage:  PY reel_check.py <project_dir> [--render FILE] [--overlays DIR] [--max-onset-err 0.15] [--expect-cuts 83,183,...]
                         [--expect-text FILE] [--expect-duration SECONDS] [--sheet]
Caption onset, safe zone and face clearance are measured on the render: --render's frames and audio, and the overlay
mask (overlay_mask.py) from the overlays-only render in --overlays (default work/overlays, make_reel.py renders it with
the film; it must have the film's frame count), as is caption presence. Without them these fail as NOT MEASURED. layout.json only names
which element a rendered box belongs to.
Checks:
  caption onset  the first rendered frame from each sentence's cut showing caption pixels vs the speech onset measured on
                 the render's audio (speech_onset: pre-emphasised energy against the local floor, searched -0.25..+1.0s
                 around the sentence's first word). Its own code, not build_reel's speech_start. A failure names
                 every sentence over the limit, worst first
  caption presence every caption element in layout.json shows caption ink on a rendered frame in its planned span;
                 one never shown fails, named with its sentence
  cuts           cut frames from segments.json vs --expect-cuts (+-1 frame)
  text           every line of --expect-text (one phrase per line) appears in the reel's on-screen text
  safe zone      rendered ink (overlay_mask: every overlay pixel but text shadows) inside the platform's safe zone on
                 every settled frame (an element's layout span minus its planned fade-in and fade-out: layout.json gives
                 timing only); the text-shadow halo and ink during fades are recorded as information
  ink timing     no ink on screen outside the planned span of every element it overlaps (one frame either side)
  duration       --render's duration vs --expect-duration (+-0.2s)
  provenance     when reel.json carries one (from a production system): a passed creative review and an approved production, and
                 shot by shot the approved narration and the produced file's sha256, for the spec's file and the file
                 ingest cut (sources.json) (provenance_problems); copied into qa-report. --require-provenance fails a
                 spec without one (for pipelines that may only cut approved episodes)
  face clearance YuNet (faces.py) on every 3rd rendered frame: on each one with settled caption ink, its top pixel
                 sits FACE_MARGIN (20px) under the face found on it, and never above the chest band (y940); such a frame
                 with no face found cannot be checked and fails; a shot with no face on any sampled frame keeps the band.
                 Halo and fades recorded as information, as for the safe zone
  picture sync   --render: each segment's picture against its source shot by pixels (picture_sync): the matched source
                 frames must put the picture within PIC_LIMIT (under half a frame) of its sound; unmeasurable fails
  openings       information: openings.json (render_packet.py), when the project has one, is copied into qa-report
                 under "openings"; a shot whose clean_from was not found or not applied gets one printed line
--sheet writes work/caption_sheet.jpg: a render frame for every sentence, unsafe area tinted red.
Writes qa-report.json.
"""
import array
import html
import json
import math
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402
from build_reel import FPS, H, PLATFORMS, PUNCH_S, W, inside, right_edge  # noqa: E402
from overlay_mask import ALPHA, MIN_AREA, read as read_overlays  # noqa: E402
from faces import CHEST_TOP, FACE_MARGIN  # noqa: E402

FACE_STEP = 3      # face clearance checks every 3rd rendered frame (faces.py plans on every source frame)

def pcm16k(f):
    """a shot's audio as 16-bit mono 16kHz PCM"""
    return subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-i', f, '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', '-'],
                          capture_output=True, check=True).stdout


FR = 200          # onset frames per second (5ms)
MIN_HOLD = .30    # every caption chunk at full opacity at least this long
SYNC_LIMIT = .034 # audio vs picture, one frame
PIC_LIMIT = .016  # picture vs the source moment its sound is at: under half a 30fps frame (16.7ms)
PIC_MIN_MATCHED = 5   # pixel-matched frames before a segment's offset counts (5 in a row cover every 24-in-30 phase)
PIC_SKIP = math.ceil(PUNCH_S * FPS) + 1   # frames after each cut left out: the look's cut punch scales the picture
PIC_CONF = .15    # a frame matches when the next-best source frame is at least 15% further away
PIC_MOVING = .10  # pixels compared: the 10% that move most in the segment's source range
PEAK_MAX = -2.0   # dBFS, the skill's -2..-5 dB guidance


def speech_onset(pcm, sr, first_word, not_before=None):
    """Speech onset of a sentence's first word in a shot's 16-bit mono PCM, searched first_word -0.25..+1.0s (Whisper
    hangs a pause on the next word). Pre-emphasised 5ms frame energy against the local floor (10th percentile, +-1s):
    the first 15ms at speech level (floor +25dB), walked back to where its rise left breath level (floor +15dB), and
    preceded by 120ms under speech level (10ms of clicks allowed): a breath or room tone over the breath line before
    the word is not a word (episode 1ecda41a: requiring breath level skipped two first words). A sound already under way at the window
    start belongs to the previous word. Checked against 17 independent labels on real generated voices (private; tests/test_spec_mode.py
    runs them when VIDEO_EDIT_ONSET_FIXTURES is set) and on synthetic audio.
    not_before: a cut; nothing before it belongs to this sentence (it counts as quiet)."""
    x = array.array('h', pcm)
    e = [x[0]] + [x[i] - .95 * x[i - 1] for i in range(1, len(x))]
    hop = sr // FR
    db = [10 * math.log10(sum(v * v for v in e[i:i + hop]) / hop / 32768 ** 2 + 1e-12) for i in range(0, len(e) - hop + 1, hop)]
    local = sorted(db[max(0, int((first_word - 1) * FR)):int((first_word + 1) * FR)]) or [-90.0]
    speech, breath = local[len(local) // 10] + 25, local[len(local) // 10] + 15
    nb = int(not_before * FR) if not_before is not None else 0
    i, end = max(nb, int((first_word - .25) * FR)), min(len(db) - 3, int((first_word + 1) * FR))
    if i > nb:
        while i < end and db[i] > breath:
            i += 1
    while i < end:
        if all(d > speech for d in db[i:i + 3]):
            j = i
            while j > nb and db[j - 1] > breath:
                j -= 1
            if sum(d > speech for d in db[max(nb, j - 24):j]) <= 2:
                return j / FR
            while i < end and db[i] > breath:
                i += 1
        i += 1
    return first_word


def sha256_of(path):
    import hashlib
    try:
        with open(path, 'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, TypeError):
        return None


def provenance_problems(spec, sources=None, require=False):
    """[] when the spec proves what it cuts: the spec's provenance (episode, production, approval, a passed creative review)
    and, shot by shot, the approved narration and the produced file's sha256, for the spec's file and (sources: the
    project's sources.json) the file ingest actually cut. Anything else is a reason to refuse. A spec with no provenance
    is only refused when `require` is set."""
    prov = spec.get('provenance')
    if not prov:
        return ['no provenance: the spec does not say which approved episode it cuts'] if require else []
    out = []
    for key in ('episodeId', 'productionId', 'packetSha256', 'scriptSha256'):
        if not prov.get(key):
            out.append(f'provenance has no {key}')
    status = (prov.get('creativeReview') or {}).get('status')
    if status != 'passed':
        out.append(f'creative review is {status}, not passed')
    if not (prov.get('approval') or {}).get('approvedAt'):
        out.append('provenance has no production approval')
    made, cut = prov.get('shots') or [], spec.get('shots') or []
    if len(made) != len(cut):
        out.append(f'{len(made)} produced shots, the spec cuts {len(cut)}')
    for k, (m, s) in enumerate(zip(made, cut), 1):
        if ' '.join(str(s.get('narration', '')).split()) != ' '.join(str(m.get('narration', '')).split()):
            out.append(f'shot {k} narration differs from the approved script')
        if sha256_of(s.get('file')) != m.get('sha256'):
            out.append(f'shot {k} file is not the produced shot (sha256 differs)')
        if sources is not None and sha256_of(sources.get(f'c{k}')) != m.get('sha256'):
            out.append(f'shot {k} cut file (sources.json c{k}) is not the produced shot')
    return out


def chunk_holds(page, end):
    """{chunk id: seconds at full opacity}: from the end of its fade-in to the start of its fade-out (else the reel end)"""
    hides = dict(re.findall(r"tl\.to\('#(w\d+-\d+)',\{autoAlpha:0[^}]*\},([\d.]+)\);", page))
    shows = re.findall(r"tl\.fromTo\('#(w\d+-\d+)',\{[^}]*\},\{[^}]*?duration:([\d.]+)[^}]*\},([\d.]+)\);", page)
    return {c: float(hides.get(c, end)) - float(t) - float(d) for c, d, t in shows}


def short_holds(page, end):
    """{chunk id: seconds at full opacity, to the ms} for every chunk held under MIN_HOLD: the page times chunks in ms, so
    the hold is compared in ms (a planned 0.300s hold reads 0.2999999999999999 in floats)"""
    holds = {c: round(v, 3) for c, v in chunk_holds(page, end).items()}
    return {c: v for c, v in holds.items() if v < MIN_HOLD}


def level_db(x, a, b, sr):
    s = x[max(0, int(a * sr)):max(0, int(b * sr))]
    return 10 * math.log10(sum(v * v for v in s) / max(1, len(s)) / 32768 ** 2 + 1e-12)


def cuts_in_speech(pcm, sr, cuts):
    """cut times where speech runs through the cut: the 30ms just outside the join fades (before -0.06..-0.03,
    after +0.025..+0.055) is louder than the local floor (10th percentile of 10ms frames, +-1.5s) +12dB.
    cuts: [(time, check the side before, check the side after)]; a side where nothing was cut away is not checked."""
    x, bad = array.array('h', pcm), []
    for t, before, after in cuts:
        frames = sorted(level_db(x, u / 100, u / 100 + .01, sr) for u in range(max(0, int((t - 1.5) * 100)), int((t + 1.5) * 100)))
        top = max(frames[len(frames) // 10] + 12, -50)
        if (before and level_db(x, t - .06, t - .03, sr) > top) or (after and level_db(x, t + .025, t + .055, sr) > top):
            bad.append(t)
    return bad


def jump_cuts(segs, lengths):
    """[(cut time, check before, check after)] in the film, leaving out seamless joins (the next segment continues the same
    take where this one ended); a segment ending at its shot's end / starting at its first frame cut nothing away there"""
    return [(b['frame'] / FPS, a['src_out'] < lengths[a['clip']] - .005, b['src_in'] >= .005) for a, b in zip(segs, segs[1:])
            if not (a['clip'] == b['clip'] and abs(a['src_out'] - b['src_in']) < .002)]


def xcorr_offset(probe, ref, sr):
    """seconds into ref where probe matches best (normalised cross-correlation)"""
    import numpy as np
    p = (probe - probe.mean()) / (probe.std() + 1e-9)
    c = np.correlate(ref - ref.mean(), p, 'valid')
    win = np.convolve((ref - ref.mean()) ** 2, np.ones(len(p)), 'valid') ** .5 + 1e-9
    return int(np.argmax(c / win)) / sr


def band_frames(f, rows, pre=''):
    """every frame as 360x640 grey (cover-cropped to 1080x1920 like assemble.py, after the `pre` crop), rows [0, rows) only"""
    import numpy as np
    raw = subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-i', f, '-vf',
                          f'{pre}scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,scale=360:640:flags=area,format=gray',
                          '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 640, 360)[:, :rows].astype(float)


def picture_region(layout):
    """(band, covered): the picture is compared above the first caption, with the cards drawn over it left out"""
    band = min([e['box'][1] for e in layout if e['kind'] == 'caption'] or [H])
    return band, [e['box'] for e in layout if e['kind'] != 'caption' and e['box'][1] < band]


def picture_sync(render, segs, src, band, covered=()):
    """{sentence id: {lo, hi, matched, n, reason}}: where each segment's picture sits against the moment its sound is at.
    Render frame k of a segment should show the source frame on screen at in + k/30; ffmpeg puts each source frame j on
    its nearest 30fps slot, so frame j is right for picture times [j/fps - 1/60, (j+1)/fps - 1/60]. Each render frame
    (above y `band`, clear of captions; `covered` boxes left out; not the cut punch) is matched by pixels to the source frames around
    it, on the PIC_MOVING pixels that move most in the segment's source range (a talking head's static background makes
    neighbouring frames look alike: whole-band matching measured 4 of 17 segments in a test run), each pixel set normalised to
    zero mean and unit variance so the look's grade does not count. Every clear match narrows the segment's offset
    [lo, hi] (s, + = picture ahead). reason None = within PIC_LIMIT; otherwise why not, including too few clear matches
    (frames that cannot be told apart fail, closed)."""
    import numpy as np
    from assemble import crop, size
    rows, half = max(8, int(band / 3) - 2), .5 / 30
    rv, cache, out = band_frames(render, rows), {}, {}
    free = np.ones((rows, 360), bool)
    for x0, y0, x1, y1 in covered:                                 # boxes in the 1080x1920 frame, frames at 1/3 scale
        free[max(0, int(y0 / 3) - 1):max(0, int(y1 / 3) + 2), max(0, int(x0 / 3) - 1):int(x1 / 3) + 2] = False
    for s in segs:
        f, z, cx, cy = src[s['clip']], s.get('zoom', 1), s.get('cx', .5), s.get('cy', .5)
        if (f, z, cx, cy) not in cache:
            cache[f, z, cx, cy] = (band_frames(f, rows, crop(z, cx, cy, *size(f))), skillenv.video_fps(f))
        sv, fps = cache[f, z, cx, cy]
        moving = np.abs(np.diff(sv[int(s['src_in'] * fps):int(s['src_out'] * fps) + 2], axis=0)).mean(0)
        mask = (moving >= np.quantile(moving[free], 1 - PIC_MOVING)) & free if moving.size else free

        def norm(a):
            x = a[mask]
            return (x - x.mean()) / (x.std() + 1e-6)
        lo, hi, matched, n = -1.0, 1.0, 0, 0
        for k in range(PIC_SKIP, s['frames']):
            tau = s['src_in'] + k / 30
            je = int(math.floor(tau * fps + 1e-6))
            if s['frame'] + k >= len(rv) or je + 1 >= len(sv):     # past the render, or the shot's held last frame
                break
            n += 1
            r = norm(rv[s['frame'] + k])
            d = {j: float(((r - norm(sv[j])) ** 2).mean()) for j in range(max(0, je - 4), min(len(sv), je + 5))}
            best = min(d, key=d.get)
            near = min(v for j, v in d.items() if j != best)
            if near <= 0 or (near - d[best]) / near < PIC_CONF:
                continue
            matched += 1
            lo, hi = max(lo, best / fps - half - tau), min(hi, (best + 1) / fps - half - tau)
        if matched < PIC_MIN_MATCHED:
            reason = f'unmeasurable: {matched} of {n} frames matched one source frame clearly'
        elif lo > hi:
            reason = 'picture inconsistent: no one offset explains the matched frames'
        elif -PIC_LIMIT < lo and hi < PIC_LIMIT:
            reason = None
        elif hi - lo < PIC_LIMIT:
            reason = f'picture off {(lo + hi) / 2 * 1000:+.0f}ms (between {lo * 1000:+.0f} and {hi * 1000:+.0f}ms)'
        else:
            reason = f'unmeasurable: the matches only place the picture between {lo * 1000:+.0f} and {hi * 1000:+.0f}ms'
        out[s['id']] = {'lo': round(lo, 4), 'hi': round(hi, 4), 'matched': matched, 'n': n, 'reason': reason}
    return out


def reveal_times(page):
    return {int(g): float(t) for g, t in re.findall(r"tl\.fromTo\('#w(\d+)-0',.*?,([\d.]+)\);", page)}


def screen_text(page):
    body = page.split('<body>', 1)[-1].split('<script>', 1)[0]
    return norm_text(html.unescape(re.sub(r'<[^>]+>', ' ', body)))


def norm_text(t):
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in t.lower().replace("'", '').replace('’', '')).split())


def caption_shown(frames, segs):
    """{segment index: time of the first rendered frame from its cut to the next showing caption pixels (overlay_mask),
    None if none does}: the sentence's first caption chunk (the one before is gone at the cut)"""
    out = {}
    for g, s in enumerate(segs):
        end = min(segs[g + 1]['frame'] if g + 1 < len(segs) else len(frames), len(frames))
        out[g] = next((frames[i]['t'] for i in range(s['frame'], end) if frames[i]['caption']), None)
    return out


def caption_errors(segs, words, audio, shown):
    """[(sentence id, onset in the film, error s or None)]: when the caption is shown (caption_shown) - the speech onset
    measured on the film's audio near where the sentence's first word should be; None when it is never shown"""
    out = []
    for g, s in enumerate(segs):
        guess = s['frame'] / FPS + words[s['clip']][s['word_idx'][0]]['start'] - s['src_in']
        on = speech_onset(*audio, guess, not_before=s['frame'] / FPS)
        out.append((s['id'], on, None if shown[g] is None else shown[g] - on))
    return out


def onset_failure(errs, lim):
    """the caption-onset failure naming every sentence over the limit, worst first; None when none is"""
    over = sorted(((sid, e) for sid, _, e in errs if e is not None and abs(e) > lim), key=lambda x: -abs(x[1]))
    if not over:
        return None
    return f'caption onset worst {abs(over[0][1]):.3f}s > {lim}s: ' + ', '.join(f'{sid} {e:+.3f}s' for sid, e in over)


def caption_presence(frames, layout, segs):
    """{caption element id: its sentence id} for every caption in layout.json that no rendered frame in its planned
    span (t0..t1) shows: no caption ink (overlay_mask) overlapping its box"""
    out = {}
    for e in layout:
        if e['kind'] == 'caption' and not any(e['t0'] <= f['t'] <= e['t1'] and any(overlap(e['box'], b) for b in f['caption'])
                                              for f in frames):
            out[e['id']] = segs[int(e['id'][1:].split('-')[0])]['id']
    return out


def openings_report(proj, segs):
    """(openings.json's notes (render_packet.py) or None, one line per shot whose clean_from was not found or not
    applied: the shot's first segment then carries edl_from_words' "opening" note)"""
    path = os.path.join(proj, 'openings.json')
    if not os.path.exists(path):
        return None, []
    with open(path, encoding='utf-8') as fh:
        notes = json.load(fh)
    lines = []
    for n in notes:
        kept = next((s for s in segs if s['clip'] == f"c{n['shot']}" and 'opening' in s), None)
        if n['clean_from'] is None:
            lines.append(f"opening shot {n['shot']}: no clean_from found ({n['note']})")
        elif kept:
            lines.append(f"opening shot {n['shot']}: clean_from {n['clean_from']}s not applied (segment {kept['id']}: {kept['opening']})")
    return notes, lines


def av_sync(render, segs, src):
    """[(sentence id, audio lag s)] measured on the render against each segment's source shot by cross-correlation against
    the source at the segment's planned in point; lag > 0 = the sound runs ahead of its planned place"""
    import numpy as np
    ra = np.frombuffer(pcm16k(render), np.int16).astype(float)
    sa = {c: np.frombuffer(pcm16k(f), np.int16).astype(float) for c, f in src.items()}
    out = []
    for s in segs:
        e = min(3, s['frames'] // 4)
        t, length = (s['frame'] + e) / FPS, min(1.0, (s['frames'] - 2 * e) / FPS)
        ref0 = max(0.0, s['src_in'] + e / FPS - .3)
        off = xcorr_offset(ra[int(t * 16000):int((t + length) * 16000)], sa[s['clip']][int(ref0 * 16000):int((ref0 + length + .6) * 16000)], 16000)
        out.append((s['id'], ref0 + off - (s['src_in'] + e / FPS)))
    return out


def cut_mismatch(segs, expected):
    got = [s['frame'] for s in segs[1:]]
    if len(got) != len(expected):
        return f'{len(got)} cuts, expected {len(expected)}: {got}'
    bad = [(g, e) for g, e in zip(got, expected) if abs(g - e) > 1]
    return f'cut frames off by more than 1: {bad}' if bad else None


def film_faces(render, detect=None):
    """{film frame: face bottom or None} on every FACE_STEP-th rendered frame: YuNet (faces.py) on the frame scaled by 1/3
    into its 640x640 input, the face bottom (faces.face_bottom) back in 1080x1920 pixels. detect: a stand-in for tests"""
    import numpy as np
    import faces as F
    if detect is None:
        import onnxruntime as ort
        session = ort.InferenceSession(F.ensure_model(), providers=['CPUExecutionProvider'])
        detect = lambda img: F.detect(session, img)            # noqa: E731
    raw = subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-i', render, '-vf', f"select='not(mod(n\\,{FACE_STEP}))',scale=360:640",
                          '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], capture_output=True, check=True).stdout
    out = {}
    for k, img in enumerate(np.frombuffer(raw, np.uint8).reshape(-1, 640, 360, 3)):
        pad = np.zeros((F.SIDE, F.SIDE, 3), np.uint8)
        pad[:, :360] = img
        hit = detect(pad)
        out[k * FACE_STEP] = None if hit is None else round(F.face_bottom([v * 3 for v in hit[1]], [[x * 3, y * 3] for x, y in hit[2]]))
    return out


FADE_IN = {'caption': .2, 'end': .25, 'step': .3, 'chips': .3, 'message': .35}   # build_reel's planned entrances (s)
FADE_OUT = .15     # its longest planned exit (.08-.15s)
NO_SPILL = {'halo_px': 0, 'spill_px': 0, 'spill_frames': 0}


def overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def element_at(box, t, layout):
    """id of the layout element a rendered box belongs to (naming only): of those planned on screen at t (+-0.25s), the
    one it overlaps most, else the nearest; None when nothing is planned then"""
    def gap(b):
        return max(b[0] - box[2], box[0] - b[2], 0) + max(b[1] - box[3], box[1] - b[3], 0)
    on = [e for e in layout if e['t0'] - .25 <= t <= e['t1'] + .25]
    return min(on, key=lambda e: (-overlap(e['box'], box), gap(e['box'])))['id'] if on else None


def settled(box, t, layout):
    """True when every element planned near t (+-0.25s) that a rendered box overlaps is past its planned fade-in and
    before its fade-out. layout.json gives the timing only; a box overlapping none is never settled (unexplained_ink
    catches it)"""
    on = [e for e in layout if e['t0'] - .25 <= t <= e['t1'] + .25 and overlap(e['box'], box)]
    return bool(on) and all(e['t0'] + FADE_IN.get(e['kind'], .2) <= t <= e['t1'] - FADE_OUT for e in on)


def zone_margin(z, box):
    """px between a box and the nearest edge of the zone (negative: outside)"""
    x0, y0, x1, y1 = box
    return min(x0 - z['left'], y0 - z['top'], right_edge(z, y0, y1) - x1, H - z['bottom'] - y1)


def overlay_frames(render, d):
    """(per-frame boxes of the overlay render in d (overlay_mask), None), or (None, why the render-measured checks
    cannot run): it must exist and have exactly the film's frame count"""
    if not render:
        return None, 'no --render'
    if not os.path.isdir(d):
        return None, f'no overlay frames at {d}'
    n = int(subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries',
                            'stream=nb_read_packets', '-of', 'csv=p=0', render], **skillenv.TEXT).stdout.strip().split(',')[0])
    frames = read_overlays(d)
    return (frames, None) if len(frames) == n else (None, f'{len(frames)} overlay frames for a {n}-frame film')


def spill(info, name, px):
    r = info.setdefault(name, dict(NO_SPILL))
    r['spill_px'], r['spill_frames'] = max(r['spill_px'], px), r['spill_frames'] + 1


def safe_zone(frames, zone, layout):
    """({element id, or 'unplanned': (first time, box)} for rendered ink outside the zone on a settled frame,
    {element id: info}). Information, never a failure: how far a settled element's text shadow reaches past the zone
    (halo_px), and its ink outside the zone during fades (spill_px, spill_frames)"""
    fails, info = {}, {e['id']: dict(NO_SPILL) for e in layout}
    for f in frames:
        for b in f['ink']:
            if inside(zone, b):
                continue
            name = element_at(b, f['t'], layout) or 'unplanned'
            if settled(b, f['t'], layout):
                fails.setdefault(name, (f['t'], b))
            else:
                spill(info, name, -zone_margin(zone, b))
        for b in f['halo']:
            if not inside(zone, b) and settled(b, f['t'], layout):
                r = info.setdefault(element_at(b, f['t'], layout) or 'unplanned', dict(NO_SPILL))
                r['halo_px'] = max(r['halo_px'], -zone_margin(zone, b))
    return fails, info


def unexplained_ink(frames, layout):
    """{element id, or 'unplanned': (first time, box)}: ink on a rendered frame where no element it overlaps is planned
    on screen (t0..t1, one frame either side), so wrong timing cannot hide a box from the settled-frame checks"""
    out = {}
    for f in frames:
        for b in f['ink']:
            hit = [e for e in layout if overlap(e['box'], b)]
            if not any(e['t0'] - 1 / FPS <= f['t'] <= e['t1'] + 1 / FPS for e in hit):
                out.setdefault(max(hit, key=lambda e: overlap(e['box'], b))['id'] if hit else 'unplanned', (f['t'], b))
    return out


def face_clearance(frames, segs, faces, layout):
    """([(caption id, time, caption top, required top or why it cannot be checked)], {sentence id: closest caption top -
    face bottom}, {caption id: info}): on every sampled frame (faces: film_faces) whose caption ink (overlay_mask) is
    settled, the top caption ink pixel sits FACE_MARGIN under the face bottom found on that frame and never above the
    chest band. Such a frame with no face found cannot be checked and fails; a shot with no face on any sampled frame
    keeps the chest band. One failure per caption, its first. Information, never a failure: how far a settled caption's
    text shadow reaches past that line (halo_px), and its ink past it during fades (spill_px, spill_frames)."""
    caps = [e for e in layout if e['kind'] == 'caption']
    seg_at = {i: g for g, s in enumerate(segs) for i in range(s['frame'], s['frame'] + s['frames'])}
    has_face = {segs[seg_at[i]]['clip'] for i, fb in faces.items() if fb is not None and i in seg_at}
    bad, margin, info = {}, {}, {e['id']: dict(NO_SPILL) for e in caps}
    for i, fb in sorted(faces.items()):
        if i >= len(frames) or i not in seg_at or not frames[i]['caption']:
            continue
        s, t = segs[seg_at[i]], frames[i]['t']
        need = CHEST_TOP if s['clip'] not in has_face else None if fb is None else max(CHEST_TOP, fb + FACE_MARGIN)
        steady = [b for b in frames[i]['caption'] if settled(b, t, caps)]
        moving = [b for b in frames[i]['caption'] if b not in steady]
        if moving and need is not None:
            box = min(moving, key=lambda b: b[1])
            if box[1] < need:
                spill(info, element_at(box, t, caps) or s['id'], need - box[1])
        if not steady:
            continue
        box = min(steady, key=lambda b: b[1])
        name = element_at(box, t, caps) or s['id']
        if need is None:
            bad.setdefault(name, (name, t, box[1], 'no face found on this frame'))
            continue
        if fb is not None:
            margin[s['id']] = min(margin.get(s['id'], box[1] - fb), box[1] - fb)
        if box[1] < need:
            bad.setdefault(name, (name, t, box[1], need))
        halo = [h[1] for h in frames[i]['caption_halo'] if any(overlap(h, b) for b in steady)]
        if halo and min(halo) < need:
            r = info.setdefault(name, dict(NO_SPILL))
            r['halo_px'] = max(r['halo_px'], need - min(halo))
    return list(bad.values()), margin, info


def missing_text(page, phrases):
    have = screen_text(page)
    return [p for p in phrases if norm_text(p) and norm_text(p) not in have]


def sheet(proj, render, segs, page, zone):
    rev, s = reveal_times(page), .2
    out, pngs = os.path.join(proj, 'work', 'caption_sheet.jpg'), []
    boxes = [(0, 0, W, zone['top']), (0, H - zone['bottom'], W, zone['bottom']), (0, 0, zone['left'], H), (W - zone['right'], 0, zone['right'], H)]
    if zone['strip']:
        boxes.append((W - zone['strip'][0], zone['strip'][1], zone['strip'][0], H - zone['strip'][1]))
    tint = ','.join(f'drawbox=x={x * s:.0f}:y={y * s:.0f}:w={w * s:.0f}:h={h * s:.0f}:color=red@0.3:t=fill' for x, y, w, h in boxes)
    os.makedirs(os.path.join(proj, 'work', 'cs'), exist_ok=True)
    for g in range(len(segs)):
        p = os.path.join(proj, 'work', 'cs', f's{g}.png')
        t = min(rev[g] + .9, (segs[g + 1]['frame'] / FPS - .1) if g + 1 < len(segs) else rev[g] + .9)
        subprocess.run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', '-ss', f'{t:.3f}', '-i', render, '-frames:v', '1',
                        '-vf', f'scale={W * s:.0f}:{H * s:.0f},{tint}', p], check=True)
        pngs.append(p)
    subprocess.run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', *sum((['-i', p] for p in pngs), []),
                    '-filter_complex', f'hstack={len(pngs)}' if len(pngs) > 1 else 'null', out], check=True)
    return out


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    skillenv.utf8_stdio()
    proj = skillenv.path_arg(sys.argv[1])

    def load(name):
        with open(os.path.join(proj, name), encoding='utf-8') as fh:
            return json.load(fh)

    segs, src, layout, built = load('segments.json'), load('sources.json'), load('layout.json'), load('build-report.json')
    with open(os.path.join(proj, 'index.html'), encoding='utf-8') as fh:
        page = fh.read()
    words = {c: load(f'words_{c}.json') for c in src}
    zone = PLATFORMS[built['platform']]
    report, fails = {'look': built['look'], 'platform': built['platform']}, []

    spec = load('reel.json')
    problems = provenance_problems(spec, src, require='--require-provenance' in sys.argv)
    report['provenance'] = spec.get('provenance')          # receipts carry what was approved, not only what was measured
    print('provenance: ' + ('REFUSED ' + '; '.join(problems) if problems else
                            'ok, episode ' + str(spec['provenance'].get('episodeId')) if spec.get('provenance') else 'none (not required)'))
    if problems:
        fails.append('provenance: ' + '; '.join(problems))

    report['openings'], lines = openings_report(proj, segs)
    for ln in lines:
        print(ln)

    render = arg('--render')
    frames, why = overlay_frames(render, arg('--overlays', os.path.join(proj, 'work', 'overlays')))
    report['overlay_mask'] = {'frames': len(frames or []), 'alpha_over': ALPHA, 'min_area': MIN_AREA, 'problem': why}
    print(f'overlay mask: {len(frames)} frames, alpha > {ALPHA}, regions of {MIN_AREA}px or more' if frames else f'overlay mask: NOT MEASURED ({why})')
    if frames:
        with open(os.path.join(proj, 'work', 'overlay-boxes.json'), 'w', encoding='utf-8') as fh:
            json.dump(frames, fh)

    lim = float(arg('--max-onset-err', .15))
    stem = (pcm16k(os.path.join(proj, 'assets', 'aroll.mp4')), 16000)
    if frames:
        shown = caption_shown(frames, segs)
        errs = caption_errors(segs, words, (pcm16k(render), 16000), shown)
        print('sentence   onset(film) shown   err')
        for g, (sid, on, e) in enumerate(errs):
            print(f'{sid:10s} {on:9.3f}  ' + (f'{shown[g]:6.3f}  {e:+.3f}' if e is not None else 'NEVER SHOWN'))
        timed = [abs(e) for _, _, e in errs if e is not None]
        never = [sid for sid, _, e in errs if e is None]
        worst = max(timed or [0])
        print(f'caption onset (rendered frames vs the render\'s audio): mean |err| {sum(timed) / max(1, len(timed)):.3f}s  worst {worst:.3f}s  '
              f'n={len(timed)}  (limit {lim})' + (f'  NEVER SHOWN {never}' if never else ''))
        report['caption_onset'] = {'errors': {sid: None if e is None else round(e, 3) for sid, _, e in errs},
                                   'shown': {segs[g]['id']: None if t is None else round(t, 3) for g, t in shown.items()},
                                   'speech': {sid: round(on, 3) for sid, on, _ in errs}, 'worst': round(worst, 3), 'limit': lim}
        late = onset_failure(errs, lim)
        if late:
            fails.append(late)
        if never:
            fails.append(f'caption never shown in its segment: {never}')
        missing = caption_presence(frames, layout, segs)
        report['caption_presence'] = {'captions': sum(e['kind'] == 'caption' for e in layout), 'never_shown': missing}
        print(f"caption presence: {report['caption_presence']['captions'] - len(missing)}/{report['caption_presence']['captions']} "
              'captions shown on the render in their planned span' + (f'  NEVER SHOWN {missing}' if missing else ''))
        if missing:
            fails.append(f'captions never shown on the render (element: sentence): {missing}')
    else:
        report['caption_onset'] = report['caption_presence'] = {'not_measured': why}
        fails += [f'caption onset NOT MEASURED: {why}', f'caption presence NOT MEASURED: {why}']

    dur = len(stem[0]) / 2 / 16000
    short = short_holds(page, dur)
    report['caption_hold'] = {'min': MIN_HOLD, 'short': short}
    print(f'caption hold: {len(short)} chunks under {MIN_HOLD}s at full opacity' + (f'  {short}' if short else ''))
    if short:
        fails.append(f'caption chunks shown under {MIN_HOLD}s: {sorted(short)}')

    lengths = {c: max(float(v) for v in subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-show_entries', 'stream=duration',
                                                          '-of', 'csv=p=0', f], **skillenv.TEXT).stdout.split()
                      if v.replace('.', '', 1).isdigit()) for c, f in src.items()}       # picture or sound, whichever is longer
    cuts = jump_cuts(segs, lengths)
    bad = cuts_in_speech(stem[0], 16000, cuts)
    report['cuts_in_speech'] = [round(t, 3) for t in bad]
    print(f'cuts in speech: {len(bad)}/{len(cuts)}' + (f'  at {report["cuts_in_speech"]}' if bad else ''))
    if bad:
        fails.append(f'cuts inside speech at {report["cuts_in_speech"]}')

    report['cuts'] = [s['frame'] for s in segs[1:]]
    print(f"cuts {len(report['cuts'])} at frames {report['cuts']}")
    if arg('--expect-cuts'):
        bad = cut_mismatch(segs, [int(x) for x in arg('--expect-cuts').split(',')])
        if bad:
            fails.append(bad)

    if frames:
        outside, info = safe_zone(frames, zone, layout)
        steady = [b for f in frames for b in f['ink'] if settled(b, f['t'], layout)]
        closest = min([zone_margin(zone, b) for b in steady] or [None])
        report['safe_zone'] = {'frames': len(frames), 'ink_boxes': sum(len(f['ink']) for f in frames), 'settled_ink_boxes': len(steady),
                               'closest_px': closest, 'outside': {k: {'t': round(t, 3), 'box': b} for k, (t, b) in outside.items()},
                               'info': info}
        halo = {k: v['halo_px'] for k, v in info.items() if v['halo_px']}
        moved = {k: (v['spill_px'], v['spill_frames']) for k, v in info.items() if v['spill_frames']}
        print(f'safe zone ({built["platform"]}, rendered ink on settled frames): {len(steady)} boxes, {len(outside)} elements outside, '
              f'closest {closest}px from the zone edge' + (f'  OUTSIDE {report["safe_zone"]["outside"]}' if outside else ''))
        print(f'  info, not failures: text-shadow halo past the zone {halo or "none"}; ink past it during fades (px, frames) {moved or "none"}')
        if outside:
            fails.append(f'outside the safe zone: {report["safe_zone"]["outside"]}')
        stray = unexplained_ink(frames, layout)
        report['ink_timing'] = {'unexplained': {k: {'t': round(t, 3), 'box': b} for k, (t, b) in stray.items()}}
        print(f'ink timing: {len(stray)} elements on screen outside their planned span (+-1 frame)' + (f'  {report["ink_timing"]["unexplained"]}' if stray else ''))
        if stray:
            fails.append(f'ink on screen outside its planned span: {report["ink_timing"]["unexplained"]}')
        faces = film_faces(render)
        bad, margin, info = face_clearance(frames, segs, faces, layout)
        seen = [i for i in faces if i < len(frames) and frames[i]['caption']]
        report['face_clearance'] = {'sampled': len(faces), 'faces_found': sum(v is not None for v in faces.values()),
                                    'sampled_with_captions': len(seen), 'closest': margin, 'too_close': bad, 'info': info}
        halo = {k: v['halo_px'] for k, v in info.items() if v['halo_px']}
        moved = {k: (v['spill_px'], v['spill_frames']) for k, v in info.items() if v['spill_frames']}
        print(f'face clearance (YuNet on rendered frames, settled caption ink): {len(seen)} sampled frames show captions, {len(bad)} captions fail '
              f'(top {FACE_MARGIN}px under the face found on the frame, never above y{CHEST_TOP}); closest {margin}' + (f'  TOO CLOSE {bad}' if bad else ''))
        print(f'  info, not failures: text-shadow halo past that line {halo or "none"}; ink past it during fades (px, frames) {moved or "none"}')
        if bad:
            fails.append(f'captions over the face: {bad}')
    else:
        report['safe_zone'] = report['face_clearance'] = {'not_measured': why}
        fails += [f'safe zone NOT MEASURED: {why}', f'face clearance NOT MEASURED: {why}']

    if arg('--expect-text'):
        with open(arg('--expect-text'), encoding='utf-8') as fh:
            phrases = [ln.strip() for ln in fh if ln.strip()]
        miss = missing_text(page, phrases)
        report['text'] = {'expected': len(phrases), 'missing': miss}
        print(f'text: {len(phrases) - len(miss)}/{len(phrases)} expected phrases on screen' + (f'  MISSING {miss}' if miss else ''))
        if miss:
            fails.append(f'missing text: {miss}')

    if render:
        d = float(subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', render],
                                 **skillenv.TEXT).stdout.strip())
        report['duration'] = round(d, 3)
        print(f'render {d:.3f}s')
        if arg('--expect-duration') and abs(d - float(arg('--expect-duration'))) > .2:
            fails.append(f'duration {d:.3f}s, expected {arg("--expect-duration")}s +-0.2')
        sync = av_sync(render, segs, src)
        late = [(sid, round(lag, 3)) for sid, lag in sync if abs(lag) > SYNC_LIMIT]
        report['av_sync'] = {'lag': {sid: round(lag, 3) for sid, lag in sync}, 'limit': SYNC_LIMIT, 'off': late}
        print(f'audio sync: worst lag {max(abs(l) for _, l in sync):.3f}s' + (f'  OFF {late}' if late else ''))
        if late:
            fails.append(f'audio out of place: {late}')
        pic = picture_sync(render, segs, src, *picture_region(layout))
        off = {sid: r['reason'] for sid, r in pic.items() if r['reason']}
        report['picture_sync'] = {'limit': PIC_LIMIT, 'segments': pic, 'failed': off}
        worst = max([max(abs(r['lo']), abs(r['hi'])) for r in pic.values()] or [0])
        print(f'picture sync: {len(pic) - len(off)}/{len(pic)} segments within {PIC_LIMIT * 1000:.0f}ms, worst bound {worst * 1000:.0f}ms'
              + (f'  FAILED {off}' if off else ''))
        if off:
            fails.append(f'picture not on its sound: {off}')
        peak = float(re.search(r'max_volume: (-?[\d.]+) dB', subprocess.run(
            [skillenv.tool('ffmpeg'), '-hide_banner', '-i', render, '-af', 'volumedetect', '-f', 'null', '-'], **skillenv.TEXT).stderr).group(1))
        report['peak_db'] = peak
        print(f'peak {peak:.1f} dB (limit {PEAK_MAX})')
        if peak > PEAK_MAX:
            fails.append(f'peak {peak:.1f} dB > {PEAK_MAX} dB')
        if '--sheet' in sys.argv:
            print('caption sheet', sheet(proj, render, segs, page, zone))

    warned = [w for w in built['warnings'] if '[WARN]' in w]
    report['render_warnings'] = warned
    if warned:
        fails.append(f'{len(warned)} render/compile warning(s): {warned[0][:160]}')

    report['warnings'], report['failures'] = built['warnings'], fails
    with open(os.path.join(proj, 'qa-report.json'), 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=1)
    for w in built['warnings']:
        print('WARNING', w)
    print('QA PASS' if not fails else 'QA FAIL: ' + ' | '.join(fails))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    skillenv.main_or_3(main)
