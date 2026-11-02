#!/usr/bin/env python3
"""Spec mode QA: measure a built reel against the audio and the spec. Exit 1 when any check fails.

Usage:  PY reel_check.py <project_dir> [--render FILE] [--max-onset-err 0.15] [--expect-cuts 83,183,...]
                         [--expect-text FILE] [--expect-duration SECONDS] [--sheet]
Checks:
  caption onset  first caption chunk of each sentence (read from index.html) vs the speech onset measured on the source
                 shot (silencedetect -40dB d=0.1: the silence end nearest the sentence's first word, within 0.4s, else
                 that word's start), mapped into cut time. Independent of the times the captions were placed with.
  cuts           cut frames from segments.json vs --expect-cuts (+-1 frame)
  text           every line of --expect-text (one phrase per line) appears in the reel's on-screen text
  safe zone      every box in layout.json inside the platform's safe zone
  duration       --render's duration vs --expect-duration (+-0.2s)
--sheet writes work/caption_sheet.jpg: a render frame for every sentence, unsafe area tinted red.
Writes qa-report.json.
"""
import html
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402
from build_reel import FPS, H, PLATFORMS, W, inside  # noqa: E402

NEAR = .4


def silence_ends(f):
    err = subprocess.run([skillenv.tool('ffmpeg'), '-hide_banner', '-i', f, '-af', 'silencedetect=n=-40dB:d=0.1', '-f', 'null', '-'],
                         **skillenv.TEXT).stderr
    return [float(m) for m in re.findall(r'silence_end: ([\d.]+)', err)]


def onset(first_word, ends):
    near = [e for e in ends if abs(e - first_word) <= NEAR]
    return min(near, key=lambda e: abs(e - first_word)) if near else first_word


def reveal_times(page):
    return {int(g): float(t) for g, t in re.findall(r"tl\.fromTo\('#w(\d+)-0',.*?,([\d.]+)\);", page)}


def screen_text(page):
    body = page.split('<body>', 1)[-1].split('<script>', 1)[0]
    return norm_text(html.unescape(re.sub(r'<[^>]+>', ' ', body)))


def norm_text(t):
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in t.lower().replace("'", '').replace('’', '')).split())


def caption_errors(segs, words, ends, page):
    """[(sentence id, onset in source, error s)]: caption reveal - measured onset"""
    rev, out = reveal_times(page), []
    for g, s in enumerate(segs):
        w0 = words[s['clip']][s['word_idx'][0]]['start']
        on = onset(w0, ends[s['clip']])
        out.append((s['id'], on, rev[g] - (s['frame'] / FPS + on - s['src_in'])))
    return out


def cut_mismatch(segs, expected):
    got = [s['frame'] for s in segs[1:]]
    if len(got) != len(expected):
        return f'{len(got)} cuts, expected {len(expected)}: {got}'
    bad = [(g, e) for g, e in zip(got, expected) if abs(g - e) > 1]
    return f'cut frames off by more than 1: {bad}' if bad else None


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

    lim = float(arg('--max-onset-err', .15))
    errs = caption_errors(segs, words, {c: silence_ends(f) for c, f in src.items()}, page)
    print('sentence   onset(src)  caption err')
    for sid, on, e in errs:
        print(f'{sid:10s} {on:9.3f}  {e:+.3f}')
    worst = max(abs(e) for _, _, e in errs)
    print(f'caption onset: mean |err| {sum(abs(e) for _, _, e in errs) / len(errs):.3f}s  worst {worst:.3f}s  n={len(errs)}  (limit {lim})')
    report['caption_onset'] = {'errors': {sid: round(e, 3) for sid, _, e in errs}, 'worst': round(worst, 3), 'limit': lim}
    if worst > lim:
        fails.append(f'caption onset worst {worst:.3f}s > {lim}s')

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
        if '--sheet' in sys.argv:
            print('caption sheet', sheet(proj, render, segs, page, zone))

    report['warnings'], report['failures'] = built['warnings'], fails
    with open(os.path.join(proj, 'qa-report.json'), 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=1)
    for w in built['warnings']:
        print('WARNING', w)
    print('QA PASS' if not fails else 'QA FAIL: ' + ' | '.join(fails))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
