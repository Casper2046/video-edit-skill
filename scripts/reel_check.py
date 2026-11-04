#!/usr/bin/env python3
"""Spec mode QA: measure a built reel against the audio and the spec. Exit 1 when any check fails.

Usage:  PY reel_check.py <project_dir> [--render FILE] [--max-onset-err 0.15] [--expect-cuts 83,183,...]
                         [--expect-text FILE] [--expect-duration SECONDS] [--sheet]
Checks:
  caption onset  first caption chunk of each sentence (read from index.html) vs the speech onset measured on the source
                 shot (speech_onset: pre-emphasised energy against the local floor, searched -0.25..+1.0s around the
                 sentence's first word), mapped into cut time. Its own code, not build_reel's speech_start.
  cuts           cut frames from segments.json vs --expect-cuts (+-1 frame)
  text           every line of --expect-text (one phrase per line) appears in the reel's on-screen text
  safe zone      every box in layout.json inside the platform's safe zone
  duration       --render's duration vs --expect-duration (+-0.2s)
  provenance     when reel.json carries one (from a production system): a passed creative review and an approved production, and
                 shot by shot the approved narration and the produced file's sha256, for the spec's file and the file
                 ingest cut (sources.json) (provenance_problems); copied into qa-report. --require-provenance fails a
                 spec without one (for pipelines that may only cut approved episodes)
  face clearance when faces.json exists (faces.py): on every sampled frame inside a caption's segment the caption box
                 starts FACE_MARGIN (20px) under the face bottom, or at the chest band (y940) if that is lower; a sampled
                 frame there with no face found cannot be checked and fails
  picture sync   --render: each segment's picture against its source shot by pixels (picture_sync): the matched source
                 frames must put the picture within PIC_LIMIT (under half a frame) of its sound; unmeasurable fails
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
from build_reel import FPS, H, PLATFORMS, PUNCH_S, W, inside  # noqa: E402
from faces import CHEST_TOP, FACE_MARGIN  # noqa: E402

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
    preceded by 120ms at breath level or below (10ms of clicks allowed). A sound already under way at the window
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
            if sum(d > breath for d in db[max(nb, j - 24):j]) <= 2:
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


def video_fps(f):
    num, den = (subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=r_frame_rate',
                                '-of', 'csv=p=0', f], **skillenv.TEXT).stdout.strip().split(',')[0].split('/') + ['1'])[:2]
    return float(num) / float(den)


def band_frames(f, rows, pre=''):
    """every frame as 360x640 grey (cover-cropped to 1080x1920 like assemble.py, after the `pre` crop), rows [0, rows) only"""
    import numpy as np
    raw = subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-i', f, '-vf',
                          f'{pre}scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,scale=360:640:flags=area,format=gray',
                          '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 640, 360)[:, :rows].astype(float)


def picture_sync(render, segs, src, band):
    """{sentence id: {lo, hi, matched, n, reason}}: where each segment's picture sits against the moment its sound is at.
    Render frame k of a segment should show the source frame on screen at in + k/30; ffmpeg puts each source frame j on
    its nearest 30fps slot, so frame j is right for picture times [j/fps - 1/60, (j+1)/fps - 1/60]. Each render frame
    (above y `band`, clear of captions and cards; not the cut punch) is matched by pixels to the source frames around
    it, on the PIC_MOVING pixels that move most in the segment's source range (a talking head's static background makes
    neighbouring frames look alike: whole-band matching measured 4 of 17 segments in a test run), each pixel set normalised to
    zero mean and unit variance so the look's grade does not count. Every clear match narrows the segment's offset
    [lo, hi] (s, + = picture ahead). reason None = within PIC_LIMIT; otherwise why not, including too few clear matches
    (frames that cannot be told apart fail, closed)."""
    import numpy as np
    from assemble import crop, size
    rows, half = max(8, int(band / 3) - 2), .5 / 30
    rv, cache, out = band_frames(render, rows), {}, {}
    for s in segs:
        f, z, cx, cy = src[s['clip']], s.get('zoom', 1), s.get('cx', .5), s.get('cy', .5)
        if (f, z, cx, cy) not in cache:
            cache[f, z, cx, cy] = (band_frames(f, rows, crop(z, cx, cy, *size(f))), video_fps(f))
        sv, fps = cache[f, z, cx, cy]
        moving = np.abs(np.diff(sv[int(s['src_in'] * fps):int(s['src_out'] * fps) + 2], axis=0)).mean(0)
        mask = moving >= np.quantile(moving, 1 - PIC_MOVING) if moving.size else np.ones(sv.shape[1:], bool)

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


def caption_errors(segs, words, stem, page):
    """[(sentence id, onset in the film, error s)]: caption reveal - speech onset measured on the film's speech stem
    (assets/aroll.mp4, before the whooshes), near where the sentence's first word should be"""
    rev, out = reveal_times(page), []
    for g, s in enumerate(segs):
        guess = s['frame'] / FPS + words[s['clip']][s['word_idx'][0]]['start'] - s['src_in']
        on = speech_onset(*stem, guess, not_before=s['frame'] / FPS)
        out.append((s['id'], on, rev[g] - on))
    return out


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


def face_clearance(layout, segs, faces):
    """[(caption id, box top, required top or why it cannot be checked)]: on every frame faces.py sampled inside the
    caption's segment the caption starts FACE_MARGIN under the face bottom (and never above the chest band); a sampled
    frame there with no face found cannot be checked and fails; a shot with no face anywhere keeps the chest band"""
    bad = []
    for e in layout:
        if e['kind'] != 'caption':
            continue
        s = segs[int(e['id'][1:].split('-')[0])]
        samples = faces.get(s['clip'], {}).get('samples')
        if samples is None:
            bad.append((e['id'], e['box'][1], 'faces.json has no per-frame samples (run faces.py)'))
            continue
        inside = [x for x in samples if s['src_in'] - 1e-6 <= x['t'] <= s['src_out'] + 1e-6]
        need = CHEST_TOP
        if any(x['face_bottom'] is not None for x in samples):
            missed = [x['t'] for x in inside if x['face_bottom'] is None]
            if missed or not inside:
                bad.append((e['id'], e['box'][1], f'no face found at {missed[0]:.2f}s' if missed else 'no sampled frame in the segment'))
                continue
            need = max([CHEST_TOP] + [x['face_bottom'] + FACE_MARGIN for x in inside])
        if e['box'][1] < need:
            bad.append((e['id'], e['box'][1], need))
    return bad


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

    lim = float(arg('--max-onset-err', .15))
    stem = (pcm16k(os.path.join(proj, 'assets', 'aroll.mp4')), 16000)
    errs = caption_errors(segs, words, stem, page)
    print('sentence   onset(film) caption err')
    for sid, on, e in errs:
        print(f'{sid:10s} {on:9.3f}  {e:+.3f}')
    worst = max(abs(e) for _, _, e in errs)
    print(f'caption onset: mean |err| {sum(abs(e) for _, _, e in errs) / len(errs):.3f}s  worst {worst:.3f}s  n={len(errs)}  (limit {lim})')
    report['caption_onset'] = {'errors': {sid: round(e, 3) for sid, _, e in errs}, 'worst': round(worst, 3), 'limit': lim}
    if worst > lim:
        fails.append(f'caption onset worst {worst:.3f}s > {lim}s')

    dur = len(stem[0]) / 2 / 16000
    short = {c: round(v, 3) for c, v in chunk_holds(page, dur).items() if v < MIN_HOLD}
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

    outside = [e['id'] for e in layout if not inside(zone, e['box'])]
    report['safe_zone'] = {'boxes': len(layout), 'outside': outside}
    print(f'safe zone ({built["platform"]}): {len(layout) - len(outside)}/{len(layout)} boxes inside')
    if outside:
        fails.append(f'outside the safe zone: {outside}')

    if os.path.exists(os.path.join(proj, 'faces.json')):
        bad = face_clearance(layout, segs, load('faces.json'))
        report['face_clearance'] = {'captions': sum(e['kind'] == 'caption' for e in layout), 'too_close': bad}
        print(f"face clearance: {report['face_clearance']['captions'] - len(bad)}/{report['face_clearance']['captions']} captions "
              f'at least {FACE_MARGIN}px under the face on every sampled frame, or on the chest band' + (f'  TOO CLOSE {bad}' if bad else ''))
        if bad:
            fails.append(f'captions over the face: {bad}')

    if arg('--expect-text'):
        with open(arg('--expect-text'), encoding='utf-8') as fh:
            phrases = [ln.strip() for ln in fh if ln.strip()]
        miss = missing_text(page, phrases)
        report['text'] = {'expected': len(phrases), 'missing': miss}
        print(f'text: {len(phrases) - len(miss)}/{len(phrases)} expected phrases on screen' + (f'  MISSING {miss}' if miss else ''))
        if miss:
            fails.append(f'missing text: {miss}')

    render = arg('--render')
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
        pic = picture_sync(render, segs, src, min([e['box'][1] for e in layout] or [H]))
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
    main()
