#!/usr/bin/env python3
"""Planned mode (SPEC v1.3 modules 6-8, contracts B, C, E): a planned edit packet in, a film built from the graphic kit
out, with its resolved timeline, timing table, frame-per-word sheet and cue-frame sheet.

Usage:  PY render_packet.py <planned packet.json> --shots DIR --out DIR      (render_packet.py dispatches here when the
        packet has `graphics`). DIR holds every take and picture the packet names; each is found by its sha256.
Steps: refuse a packet pinned to another build, an unsupported treatment, or a cue whose words are not the words of its
line (or a repeated word without its index) -> transcribe the takes as cut (medium.en) -> resolve every cue on that render
transcript (refuse a cue word missing from it, or a hold past its line's take) -> compose index.html from the kit (takes
at the kit's placements, captions unchanged, one piece per graphic) -> render (HyperFrames + GSAP at the pin, local GSAP,
no CDN) -> render each graphic alone as RGBA frames and measure the frames it is on screen -> timing table vs the rules
-> sheets. Writes <out>/renders/<slug>-planned.mp4, <out>/qa-report.json and <out>/evidence/*.
Exit: 0 every check passes, 1 a timing check failed, 2 refused (the graphic id is in the message), 3 a stage failed.
HyperFrames: VIDEO_EDIT_HYPERFRAMES_PLANNED (a hyperframes binary at the pinned version), else npx hyperframes@<pin>.
"""
import glob
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402
from edl_from_words import align, norm  # noqa: E402

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = {'hyperframes': '0.9.23', 'gsap': '3.15.0', 'kit': 'film3-r3', 'fps': 24}
GSAP_SHA = '92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb'   # gsap@3.15.0 dist/gsap.min.js, as hyperframes 0.9.23 ships it
MIN_HOLD = {'photo_panel': 2.0, 'top_band_broll': 2.0, 'circle': 1.5}            # main picture 2.0 s, overlays 1.5 s
MIN_ENTRANCE_GAP = 0.5
ALPHA, MIN_PIXELS = 32, 16      # a measured frame shows a graphic when 16+ pixels have alpha over 32 (overlay_mask's rule)


class Refused(Exception):
    pass


# ------------------------------------------------------------------ cues (contract B)
def cue_span(line_text, graphic):
    """(from, to) word indices of the graphic's cue in its line. Words, when given, must be those words of the line
    (case and punctuation ignored); without an index they must occur exactly once."""
    gid, cue = graphic['id'], graphic['cue']
    toks = [norm(t) for t in line_text.split()]
    lo, hi = cue.get('from'), cue.get('to')
    want = [norm(t) for t in str(cue.get('words', '')).split() if norm(t)]
    if lo is not None or hi is not None:
        if lo is None or hi is None or not 0 <= lo <= hi < len(toks):
            raise Refused(f"{gid}: cue words {lo}-{hi} are not in line {cue['lineId']} ({len(toks)} words)")
        if want and toks[lo:hi + 1] != want:
            raise Refused(f"{gid}: cue words '{cue['words']}' are not words {lo}-{hi} of line {cue['lineId']} "
                          f"('{' '.join(line_text.split()[lo:hi + 1])}')")
        return lo, hi
    if not want:
        raise Refused(f"{gid}: cue has neither words nor a word index")
    hits = [i for i in range(len(toks) - len(want) + 1) if toks[i:i + len(want)] == want]
    if not hits:
        raise Refused(f"{gid}: cue words '{cue['words']}' are not in line {cue['lineId']}")
    if len(hits) > 1:
        raise Refused(f"{gid}: cue words '{cue['words']}' occur {len(hits)} times in line {cue['lineId']} "
                      f"(words {', '.join(map(str, hits))}); the cue must give its word index")
    return hits[0], hits[0] + len(want) - 1


def check_pin(pk):
    eng = (pk.get('versions') or {}).get('engine') or {}
    off = [f'{k} {eng.get(k)} (this engine: {v})' for k, v in PIN.items() if eng.get(k) != v]
    if off:
        raise Refused('the packet is pinned to another render build: ' + '; '.join(off))


def check_cues(pk, kit=None):
    """every graphic's line exists, its treatment is a kit piece, its cue is words of its line; persistent elements are
    not in this kit yet (contract C: anything else is refused as unsupported)"""
    lines = {ln['id']: ln for ln in pk.get('lines', [])}
    presets = (kit or {}).get('presets')
    for gr in pk.get('graphics', []):
        line = lines.get(gr['cue'].get('lineId'))
        if not line:
            raise Refused(f"{gr['id']}: cue line {gr['cue'].get('lineId')} is not in the packet")
        if presets is not None and (gr.get('treatment') not in presets or gr.get('layout') != gr.get('treatment')):
            raise Refused(f"{gr['id']}: treatment {gr.get('treatment')} / layout {gr.get('layout')} is not in kit {kit['kit']}")
        cue_span(line['text'], gr)
    for p in pk.get('persistent', []):
        raise Refused(f"{p['id']}: persistent elements are not in kit {PIN['kit']} yet")


def resolve(pk, transcript, place, fps):
    """every graphic placed on the render transcript: the first cue word's onset, the frame it enters on (the frame that
    holds the onset) and its frames; refuses a cue word the transcript does not have, or a hold past its line's take"""
    tokens, first = [], {}
    for ln in pk['lines']:
        first[ln['id']] = len(tokens)
        tokens += ln['text'].split()
    heard = align(tokens, transcript)
    lines = {ln['id']: ln for ln in pk['lines']}
    out = []
    for gr in pk['graphics']:
        lid = gr['cue']['lineId']
        lo, hi = cue_span(lines[lid]['text'], gr)
        k = heard[first[lid] + lo]
        word = lines[lid]['text'].split()[lo]
        if k is None:
            raise Refused(f"{gr['id']}: cue word '{word}' (line {lid} word {lo}) is not on the render transcript")
        onset = transcript[k]['start']
        start, frames = math.floor(onset * fps + 1e-6), round(gr['holdSec'] * fps)
        take_end = place[lid]['start'] + place[lid]['duration']
        if (start + frames) / fps > take_end + 1e-6:
            raise Refused(f"{gr['id']}: hold {gr['holdSec']} s from {start / fps:.3f} s runs past line {lid}'s take "
                          f"(ends {take_end:.3f} s)")
        out.append({'id': gr['id'], 'treatment': gr['treatment'], 'lineId': lid, 'from': lo, 'to': hi,
                    'words': ' '.join(lines[lid]['text'].split()[lo:hi + 1]), 'onset': onset, 'heardAs': transcript[k]['text'],
                    'startFrame': start, 'frames': frames, 'holdSec': gr['holdSec']})
    return out


# ------------------------------------------------------------------ timing table (contract E, settled rules)
def timing_table(resolved, measured, fps):
    """rows per graphic from the frames it was measured on screen, and the rule failures"""
    rows, failures = [], []
    for r in resolved:
        fr = sorted(measured.get(r['id'], []))
        cue_frame = math.floor(r['onset'] * fps + 1e-6)
        row = {'id': r['id'], 'treatment': r['treatment'], 'cue': {'lineId': r['lineId'], 'from': r['from'], 'to': r['to'],
               'words': r['words']}, 'cueOnsetSec': r['onset'], 'cueOnsetFrame': cue_frame,
               'plannedStartFrame': r['startFrame'], 'plannedFrames': r['frames'], 'minHoldSec': MIN_HOLD.get(r['treatment'])}
        if not fr:
            failures.append(f"{r['id']}: not on screen in any measured frame")
            rows.append({**row, 'pass': False})
            continue
        first, last = fr[0], fr[-1]
        hold = (last - first + 1) / fps
        row.update({'measuredFirstFrame': first, 'measuredLastFrame': last, 'measuredFirstSec': round(first / fps, 6),
                    'measuredEndSec': round((last + 1) / fps, 6), 'measuredHoldSec': round(hold, 6),
                    'measuredFrames': len(fr), 'contiguous': len(fr) == last - first + 1,
                    'onsetDeltaFrames': first - cue_frame})
        ok = True
        if row['minHoldSec'] is not None and hold + 1e-9 < row['minHoldSec']:
            failures.append(f"{r['id']}: on screen {hold:.3f} s; the rule for {r['treatment']} is >= {row['minHoldSec']:.1f} s")
            ok = False
        if first != cue_frame:
            failures.append(f"{r['id']}: first on screen at frame {first}, the onset of '{r['words'].split()[0]}' is frame "
                            f"{cue_frame} ({first - cue_frame:+d} frames)")
            ok = False
        if not row['contiguous']:
            failures.append(f"{r['id']}: leaves the screen and comes back between frames {first} and {last}")
            ok = False
        rows.append({**row, 'pass': ok})
    seen = sorted((r['measuredFirstFrame'], r['id']) for r in rows if 'measuredFirstFrame' in r)
    for (fa, a), (fb, b) in zip(seen, seen[1:]):
        if (fb - fa) / fps < MIN_ENTRANCE_GAP - 1e-9:
            failures.append(f'{a} and {b} enter {(fb - fa) / fps:.2f} s apart; the rule is no two entrances within '
                            f'{MIN_ENTRANCE_GAP} s')
            for row in rows:
                if row['id'] in (a, b):
                    row['pass'] = False
    return rows, failures


# ------------------------------------------------------------------ media
def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def match_files(pk, shots_dir):
    """{sha256: path} for every take and picture the packet names, each matched by its bytes"""
    found = {}
    for f in sorted(os.listdir(shots_dir)) if os.path.isdir(shots_dir) else []:
        p = os.path.join(shots_dir, f)
        if os.path.isfile(p):
            found.setdefault(sha256(p), []).append(p)
    want = [(ln['id'], ln['take']) for ln in pk['lines']] + [(g['id'], a) for g in pk['graphics'] for a in g['assets']]
    out = {}
    for who, a in want:
        hits = found.get(a['sha256'], [])
        if len(hits) != 1:
            raise Refused(f"{who}: {len(hits)} files in {shots_dir} have the sha256 of {a['id']} ({a['sha256']})")
        out[a['sha256']] = os.path.abspath(hits[0])
    return out


def run(cmd, **kw):
    p = subprocess.run(cmd, **skillenv.TEXT, **kw)
    if p.returncode:
        print(p.stdout[-3000:] + p.stderr[-3000:], file=sys.stderr)
        raise subprocess.CalledProcessError(p.returncode, cmd[:3])
    return p


def transcribe(pk, files, kit, work):
    """the render transcript: the takes as cut (the kit's placements) in one track, medium.en words in film time"""
    from faster_whisper import WhisperModel
    ins, chains = [], []
    for i, ln in enumerate(pk['lines']):
        t = kit['takes'][ln['id']]
        ins += ['-i', files[ln['take']['sha256']]]
        chains.append(f"[{i}:a]atrim=start={t['mediaStart']}:duration={t['duration']},asetpts=PTS-STARTPTS[a{i}]")
    wav = os.path.join(work, 'cut.wav')
    graph = ';'.join(chains) + ';' + ''.join(f'[a{i}]' for i in range(len(chains))) + f'concat=n={len(chains)}:v=0:a=1[out]'
    run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', *ins, '-filter_complex', graph, '-map', '[out]', '-ac', '1',
         '-ar', '16000', wav])
    m = WhisperModel('medium.en', device='cpu', compute_type='int8')
    segs, _ = m.transcribe(wav, word_timestamps=True, vad_filter=False)
    return [{'text': w.word.strip(), 'start': round(w.start, 3), 'end': round(w.end, 3)} for s in segs for w in s.words]


def hyperframes():
    """(argv prefix, env, package dir or None) for the pinned HyperFrames"""
    env = skillenv.hyperframes_env(None, skillenv.tool('ffmpeg') if os.path.isabs(skillenv.tool('ffmpeg')) else None)
    env.update(HYPERFRAMES_NO_UPDATE_CHECK='1', HYPERFRAMES_NO_AUTO_INSTALL='1')
    named = os.environ.get('VIDEO_EDIT_HYPERFRAMES_PLANNED')
    if named:
        argv, pkg = [named], os.path.dirname(os.path.dirname(os.path.realpath(named)))
    else:
        argv, pkg = skillenv.npx_command(skillenv.find_node()[0]) + ['--yes', f"hyperframes@{PIN['hyperframes']}"], None
    got = subprocess.run(argv + ['--version'], env=env, **skillenv.TEXT).stdout.strip()
    if PIN['hyperframes'] not in got:
        raise RuntimeError(f"hyperframes {got!r} is not the pinned {PIN['hyperframes']}")
    return argv, env, pkg


def vendor_gsap(pkg, dst):
    """GSAP at the pin, from the copy HyperFrames ships (no CDN): VIDEO_EDIT_GSAP_PLANNED, else <hyperframes>/dist/vendor"""
    src = os.environ.get('VIDEO_EDIT_GSAP_PLANNED') or (pkg and os.path.join(
        pkg, 'dist', 'vendor', 'npm', f"gsap@{PIN['gsap']}", 'dist', 'gsap.min.js'))
    if not src or not os.path.isfile(src) or sha256(src) != GSAP_SHA:
        raise RuntimeError(f"no GSAP {PIN['gsap']} with sha256 {GSAP_SHA} at {src}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


HEAD = '''<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1080, height=1920" />
    <script src="assets/vendor/gsap.min.js"></script>
    <style>
      @font-face { font-family: "Poppins"; font-weight: 800; src: url("assets/fonts/Poppins-800-latin.woff2") format("woff2"); }
      :root { --color-accent: #ffc23d; --color-ink: #1f2d5c; --color-paper: #fbf6ec; --color-marker: #c4161c; }
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { margin: 0; width: 1080px; height: 1920px; overflow: hidden; background: __BG__; }
      #root { position: relative; width: 1080px; height: 1920px; overflow: hidden; }
      .take { position: absolute; left: 0; top: 0; width: 1080px; height: 1920px; object-fit: cover; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="__DUR__" data-fps="__FPS__" data-width="1080" data-height="1920">
'''
TAIL = '''    </div>
    <script>
      (function () {
        var tl = gsap.timeline({ paused: true });
        tl.set({}, {}, __DUR__);
        window.__timelines["main"] = tl;
      })();
    </script>
  </body>
</html>
'''


def host(gid, src, start, dur, track, kind='graphics', z=10):
    return (f'      <div id="{gid}" data-composition-id="{gid}" data-composition-src="{src}" data-start="{start:.6f}" '
            f'data-duration="{dur:.6f}" data-track-index="{track}" data-track-kind="{kind}" data-width="1080" '
            f'data-height="1920" style="position: absolute; inset: 0; z-index: {z}"></div>\n')


def panel_piece(kit, gid, src, size, hold):
    pre = kit['presets']['photo_panel']
    x0, y0, x1, y1 = pre['box']
    w, h, pad = x1 - x0, y1 - y0, pre['pad']
    s = min((w - 2 * pad) / size[0], (h - 2 * pad) / size[1])
    iw, ih = round(size[0] * s), round(size[1] * s)
    with open(os.path.join(SK, 'kits', kit['kit'], pre['piece']), encoding='utf-8') as fh:
        page = fh.read()
    vals = {'__ID__': gid, '__SRC__': src, '__LEFT__': x0, '__TOP__': y0, '__W__': w, '__H__': h,
            '__IMG_L__': (w - iw) // 2, '__IMG_T__': (h - ih) // 2, '__IMG_W__': iw, '__IMG_H__': ih,
            '__ENTER__': pre['enter'], '__EXIT__': pre['exit'], '__EXIT_AT__': f"{hold - pre['exit']:.6f}", '__HOLD__': f'{hold:.6f}'}
    for k, v in vals.items():
        page = page.replace(k, str(v))
    return page


def build(pk, files, resolved, kit, proj, masks):
    """<proj>/index.html (the film) and <masks>/<id>/index.html (each graphic alone, transparent: a project of its own, so
    each project has one root composition) from the kit"""
    fps, dur = kit['fps'], kit['duration']
    kdir = os.path.join(SK, 'kits', kit['kit'])
    for sub in ('assets/takes', 'assets/gen', 'assets/fonts', 'compositions'):
        os.makedirs(os.path.join(proj, sub), exist_ok=True)
    shutil.copyfile(os.path.join(kdir, 'fonts', 'Poppins-800-latin.woff2'), os.path.join(proj, 'assets/fonts/Poppins-800-latin.woff2'))
    shutil.copyfile(os.path.join(kdir, 'captions.html'), os.path.join(proj, 'compositions', 'captions.html'))
    takes = ''
    for ln in pk['lines']:
        t, rel = kit['takes'][ln['id']], f"assets/takes/{ln['id']}.mp4"
        shutil.copyfile(files[ln['take']['sha256']], os.path.join(proj, rel))
        takes += (f'      <video id="take-{ln["id"]}" class="clip take" src="{rel}" data-start="{t["start"]}" '
                  f'data-duration="{t["duration"]}" data-media-start="{t["mediaStart"]}" data-track-index="0" '
                  f'data-has-audio="true" data-volume="1" playsinline></video>\n')
    graphics = {}
    by_id = {g['id']: g for g in pk['graphics']}
    for r in resolved:
        a = by_id[r['id']]['assets'][0]
        ext = os.path.splitext(a['storagePath'])[1] or '.png'
        rel = f"assets/gen/{a['id']}{ext}"
        shutil.copyfile(files[a['sha256']], os.path.join(proj, rel))
        piece = f"compositions/{r['id']}.html"
        with open(os.path.join(proj, piece), 'w', encoding='utf-8') as fh:
            fh.write(panel_piece(kit, r['id'], rel, (a['width'], a['height']), r['frames'] / fps))
        graphics[r['id']] = host(r['id'], piece, r['startFrame'] / fps, r['frames'] / fps, 1)
    head = HEAD.replace('__DUR__', str(dur)).replace('__FPS__', str(fps))
    tail = TAIL.replace('__DUR__', str(dur))
    with open(os.path.join(proj, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(head.replace('__BG__', '#000') + takes + ''.join(graphics.values())
                 + host('captions', 'compositions/captions.html', 0, dur, 5, 'captions', 20) + tail)
    for r in resolved:
        gid, a = r['id'], by_id[r['id']]['assets'][0]
        mp = os.path.join(masks, gid)
        for rel in (f'compositions/{gid}.html', f"assets/gen/{a['id']}{os.path.splitext(a['storagePath'])[1] or '.png'}",
                    'assets/vendor/gsap.min.js', 'assets/fonts/Poppins-800-latin.woff2'):
            os.makedirs(os.path.dirname(os.path.join(mp, rel)), exist_ok=True)
            shutil.copyfile(os.path.join(proj, rel), os.path.join(mp, rel))
        with open(os.path.join(mp, 'index.html'), 'w', encoding='utf-8') as fh:
            fh.write(head.replace('__BG__', 'transparent') + graphics[gid] + tail)


def measure(frames_dir):
    """frame indices (0 = the film's first frame) on which the RGBA frames show a graphic"""
    import numpy as np
    from PIL import Image
    shown = []
    for i, f in enumerate(sorted(glob.glob(os.path.join(frames_dir, '*.png')))):
        a = np.asarray(Image.open(f).convert('RGBA'))[:, :, 3]
        if int((a > ALPHA).sum()) >= MIN_PIXELS:
            shown.append(i)
    return shown


def frames_at(film, idx, out_dir, scale='270:480'):
    """one PNG per film frame index (in idx order), scaled"""
    os.makedirs(out_dir, exist_ok=True)
    uniq = sorted(set(idx))
    sel = '+'.join(f'eq(n\\,{n})' for n in uniq)
    run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', '-i', film, '-vf', f'select={sel},scale={scale}', '-vsync', '0',
         os.path.join(out_dir, 'f%04d.png')])
    got = sorted(glob.glob(os.path.join(out_dir, 'f*.png')))
    if len(got) != len(uniq):
        raise RuntimeError(f'extracted {len(got)} frames of {len(uniq)}')
    return {n: p for n, p in zip(uniq, got)}


def sheet(cells, path, cols=8):
    """cells: [(png path, [label lines])] -> one JPEG contact sheet"""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(os.path.join(SK, 'assets', 'template', 'assets', 'fonts', 'Inter-600-normal.woff2'), 20)
    cw, ch, lh = 270, 480, 26
    lab = lh * max(len(c[1]) for c in cells)
    rows = math.ceil(len(cells) / cols)
    im = Image.new('RGB', (cols * cw, rows * (ch + lab)), (20, 20, 20))
    d = ImageDraw.Draw(im)
    for k, (png, lines) in enumerate(cells):
        x, y = (k % cols) * cw, (k // cols) * (ch + lab)
        im.paste(Image.open(png).convert('RGB'), (x, y))
        for j, t in enumerate(lines):
            d.text((x + 6, y + ch + 2 + j * lh), t, font=font, fill=(255, 255, 255) if j == 0 else (200, 200, 200))
    im.save(path, quality=88)


def main(packet_path, shots_dir, out):
    try:
        with open(packet_path, encoding='utf-8') as fh:
            pk = json.load(fh)
        with open(os.path.join(SK, 'kits', PIN['kit'], 'kit.json'), encoding='utf-8') as fh:
            kit = json.load(fh)
        check_pin(pk)
        check_cues(pk, kit)
        missing = [ln['id'] for ln in pk['lines'] if ln['id'] not in kit['takes']]
        if missing:
            raise Refused(f"lines {', '.join(missing)} have no placement in kit {kit['kit']}")
        files = match_files(pk, shots_dir)
    except Refused as e:
        print(f'packet refused: {e}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as e:
        print(f'packet refused: cannot read the packet ({type(e).__name__}: {e})', file=sys.stderr)
        return 2
    fps = kit['fps']
    proj, work, ev = os.path.join(out, 'project'), os.path.join(out, 'work'), os.path.join(out, 'evidence')
    if os.path.exists(os.path.join(out, 'project')):
        print(f'packet refused: {out} already holds a planned render; give a new --out', file=sys.stderr)
        return 2
    for d in (proj, work, ev, os.path.join(out, 'renders')):
        os.makedirs(d, exist_ok=True)
    print('== transcribe: medium.en on the takes as cut', flush=True)
    transcript = transcribe(pk, files, kit, work)
    try:
        resolved = resolve(pk, transcript, kit['takes'], fps)
    except Refused as e:
        print(f'packet refused: {e}', file=sys.stderr)
        return 2
    hf, env, pkg = hyperframes()
    vendor_gsap(pkg, os.path.join(proj, 'assets', 'vendor', 'gsap.min.js'))
    masks = os.path.join(out, 'masks')
    build(pk, files, resolved, kit, proj, masks)
    print('== lint', flush=True)
    log = run(hf + ['lint'], cwd=proj, env=env)
    with open(os.path.join(work, 'lint.log'), 'w', encoding='utf-8') as fh:
        fh.write(log.stdout + log.stderr)
    slug = 'ep-' + (''.join(c for c in str(pk.get('episodeId', '')).lower() if c.isalnum())[:8] or 'unknown')
    film = os.path.join(out, 'renders', f'{slug}-planned.mp4')
    print('== render: film', flush=True)
    log = run(hf + ['render', '--quality', 'high', '--fps', str(fps), '-o', film], cwd=proj, env=env)
    with open(os.path.join(work, 'render.log'), 'w', encoding='utf-8') as fh:
        fh.write(log.stdout + log.stderr)
    measured = {}
    for r in resolved:
        print(f"== render: {r['id']} alone (RGBA frames)", flush=True)
        frames = os.path.join(work, f"mask-{r['id']}")
        log = run(hf + ['render', '--format', 'png-sequence', '--fps', str(fps), '-o', frames],
                  cwd=os.path.join(masks, r['id']), env=env)
        with open(os.path.join(work, f"mask-{r['id']}.log"), 'w', encoding='utf-8') as fh:
            fh.write(log.stdout + log.stderr)
        measured[r['id']] = measure(frames)
    rows, failures = timing_table(resolved, measured, fps)
    probe = run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-count_frames', '-show_entries',
                 'stream=r_frame_rate,nb_read_frames,width,height:format=duration', '-of', 'json', film]).stdout
    info = json.loads(probe)
    st = info['streams'][0]
    if st['r_frame_rate'] != f'{fps}/1' or (st['width'], st['height']) != (1080, 1920):
        failures.append(f"film is {st['width']}x{st['height']} at {st['r_frame_rate']} fps, not 1080x1920 at {fps}")
    on = {r['id']: set(measured[r['id']]) for r in resolved}

    # frame-per-word sheet: the film frame holding each heard word's onset, with what is on screen
    tokens, owner = [], []
    for ln in pk['lines']:
        for j, t in enumerate(ln['text'].split()):
            tokens.append(t)
            owner.append((ln['id'], j))
    heard = align(tokens, transcript)
    words = []
    for i, w in enumerate(transcript):
        n = math.floor(w['start'] * fps + 1e-6)
        k = heard.index(i) if i in heard else None
        words.append({'i': i, 'heard': w['text'], 'start': w['start'], 'end': w['end'], 'frame': n,
                      'script': None if k is None else {'lineId': owner[k][0], 'word': owner[k][1], 'text': tokens[k]},
                      'onScreen': [g for g in on if n in on[g]]})
    pngs = frames_at(film, [w['frame'] for w in words], os.path.join(work, 'words'))
    sheet([(pngs[w['frame']], [f"{w['heard']}", f"{w['start']:.2f}s f{w['frame']}", ' '.join(w['onScreen']) or '-'])
           for w in words], os.path.join(ev, 'frame-per-word.jpg'))
    with open(os.path.join(ev, 'frame-per-word.json'), 'w', encoding='utf-8') as fh:
        json.dump(words, fh, indent=1)

    # cue-frame sheet: per graphic, the frame before it, its first frame, the cue onset frame, its last frame, the one after
    cells = []
    for row in rows:
        if 'measuredFirstFrame' not in row:
            continue
        marks = [('before', row['measuredFirstFrame'] - 1), ('first', row['measuredFirstFrame']),
                 ('cue onset', row['cueOnsetFrame']), ('last', row['measuredLastFrame']), ('after', row['measuredLastFrame'] + 1)]
        got = frames_at(film, [n for _, n in marks], os.path.join(work, f"cue-{row['id']}"))
        cells += [(got[n], [f"{row['id']} {what}", f'f{n} {n / fps:.3f}s', f"'{row['cue']['words']}'"]) for what, n in marks]
    if cells:
        sheet(cells, os.path.join(ev, 'cue-frames.jpg'), cols=5)

    timeline = {'pin': PIN, 'packetEngine': pk['versions']['engine'], 'kit': kit['kit'], 'fps': fps, 'duration': kit['duration'],
                'takes': [{'lineId': ln['id'], 'takeId': ln['take']['id'], 'sha256': ln['take']['sha256'], **kit['takes'][ln['id']]}
                          for ln in pk['lines']],
                'transcript': {'model': 'faster-whisper medium.en int8', 'words': transcript,
                               'scriptWords': len(tokens), 'scriptWordsHeard': sum(h is not None for h in heard)},
                'graphics': [{**r, 'startSec': r['startFrame'] / fps, 'durationSec': r['frames'] / fps} for r in resolved],
                'captions': {'piece': 'captions.html', 'sha256': sha256(os.path.join(proj, 'compositions', 'captions.html'))},
                'gsapSha256': GSAP_SHA, 'film': {'path': os.path.relpath(film, out), 'sha256': sha256(film), 'probe': info}}
    with open(os.path.join(ev, 'resolved-timeline.json'), 'w', encoding='utf-8') as fh:
        json.dump(timeline, fh, indent=1)
    with open(os.path.join(ev, 'timing-table.json'), 'w', encoding='utf-8') as fh:
        json.dump({'fps': fps, 'rules': {'minHoldSec': MIN_HOLD, 'minEntranceGapSec': MIN_ENTRANCE_GAP,
                                         'measured': f'RGBA frames of each graphic rendered alone; on screen = {MIN_PIXELS}+ px alpha > {ALPHA}'},
                   'rows': rows, 'failures': failures}, fh, indent=1)
    with open(os.path.join(out, 'qa-report.json'), 'w', encoding='utf-8') as fh:
        json.dump({'mode': 'planned', 'failures': failures, 'duration': float(info['format']['duration']),
                   'evidence': sorted(os.listdir(ev))}, fh, indent=1)
    print('QA ' + ('PASS' if not failures else 'FAIL: ' + '; '.join(failures)), flush=True)
    return 0 if not failures else 1


if __name__ == '__main__':
    skillenv.utf8_stdio()
    a = sys.argv
    if len(a) < 2 or '--shots' not in a or '--out' not in a:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(a[1], a[a.index('--shots') + 1], os.path.abspath(a[a.index('--out') + 1])))
