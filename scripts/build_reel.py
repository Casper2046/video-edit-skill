#!/usr/bin/env python3
"""Spec mode: build <project>/index.html (HyperFrames) from the reel spec, a look pack and a platform safe zone.

Usage:  PY build_reel.py <project_dir> [--look NAME] [--platform NAME] [--safe]
Reads reel.json, segments.json (assemble.py), words_cN.json (ingest.py), words.json (transcribe_cut.py) and
assets/aroll.mp4. Writes index.html, layout.json (every caption and card box with its time span) and build-report.json.

Captions say the narration's words; their times come from medium.en on the cut, aligned back to the narration, and
fall back to the shot's own words (docs: timing from the cut, spelling from the narration). Font sizes are fitted with
the real font files, and every box is checked against the platform's safe zone: a box outside it is an error.
--safe draws the unsafe area in red (snapshots only, never render with it).
"""
import html
import json
import math
import os
import subprocess
import sys

from PIL import ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402
from edl_from_words import align, norm  # noqa: E402

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(SK, 'assets', 'template', 'assets', 'fonts')
W, H, FPS = 1080, 1920, 30
LEAD = .05      # a caption chunk appears this long before its first word
END_GAP = .25   # the end card takes over this long after the last word
# outer margins + an extra right margin (width, from y) for the action-button column. Sources: references/layout.md
PLATFORMS = {
    'reels': {'top': 220, 'bottom': 450, 'left': 35, 'right': 35, 'strip': (100, 1155)},
    'tiktok': {'top': 240, 'bottom': 660, 'left': 120, 'right': 120, 'strip': (300, 840)},
    'shorts': {'top': 288, 'bottom': 672, 'left': 48, 'right': 192, 'strip': None},
}
LABEL = {'size': 34, 'lh': 41, 'ls': .24}    # step labels (tracked caps)
CHIP = {'size': 30, 'lh': 64, 'ls': .16, 'pad': 48, 'gap': 14}
MSG = {'left': 110, 'right': 970, 'h': 120, 'size': 36, 'pad': 60}
WHOOSH = ('whoosh-short', .57, .18)
GSAP_URL = 'https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js'
GSAP_LOCAL = 'assets/vendor/gsap.min.js'   # make_reel.py puts a hash-checked copy here so renders need no network
SFX_GAIN = 0.75


class SpecError(ValueError):
    pass


def load_look(name):
    p = os.path.join(SK, 'looks', f'{name}.json')
    if not os.path.exists(p):
        raise SpecError(f'no look pack "{name}" (have: {", ".join(sorted(f[:-5] for f in os.listdir(os.path.dirname(p))))})')
    with open(p, encoding='utf-8') as fh:
        return json.load(fh)


def right_edge(z, y0, y1):
    strip = z['strip']
    return W - max(z['right'], strip[0] if strip and y1 > strip[1] else 0)


def inside(z, box):
    x0, y0, x1, y1 = box
    return x0 >= z['left'] - .5 and y0 >= z['top'] and x1 <= right_edge(z, y0, y1) + .5 and y1 <= H - z['bottom']


_font_cache = {}


def _font(st, size):
    key = (st['family'], st['weight'], st['style'], size)
    if key not in _font_cache:
        f = f"{st['family'].replace(' ', '')}-{st['weight']}-{st['style']}.woff2"
        _font_cache[key] = ImageFont.truetype(os.path.join(FONTS, f), size)
    return _font_cache[key]


def text_w(text, st, size):
    """rendered width in px, measured from the font file (+5% margin; tracking added only when positive)"""
    return (_font(st, size).getlength(text) + max(st.get('ls', 0), 0) * size * len(text)) * 1.05


def ink(text, st, size, css_lh=.9):
    """(top, bottom) of the drawn glyphs relative to the line's top, for a CSS line-height of css_lh: descenders
    ('g', 'y', 'p') hang below the line box, so the safe-zone check uses the ink, not the line height"""
    f = _font(st, size)
    asc, desc = f.getmetrics()
    _, y0, _, y1 = f.getbbox(text)
    half = (css_lh * size - asc - desc) / 2
    return half + y0, half + y1


def style(look, cls):
    parts = cls.split()
    if parts[0] not in look['classes']:
        raise SpecError(f'caption class "{parts[0]}" not in the look pack (have {sorted(look["classes"])})')
    st = dict(look['classes'][parts[0]])
    if 'y' in parts:
        st.update({k: v for k, v in look['accent'].items() if k != 'color'})
    return st


def column(z, y0, y1, maxw):
    """(left, width) of the box text is centred in: the full width centred on the frame when the zone allows it,
    else the zone's own column (TikTok's right-hand buttons push it left)"""
    left, right = z['left'], right_edge(z, y0, y1)
    sym = 2 * min(W / 2 - left, right - W / 2)
    if sym >= .75 * (right - left):
        return W / 2 - sym / 2, sym, min(maxw, sym - 20)
    return left, right - left, min(maxw, right - left - 20)


def line(text, st, top, z, maxw):
    """one centred line: fitted font size, its box and the column it sits in"""
    left, width, fit = column(z, top, top + st['lh'], maxw)
    fs = min(st['size'], math.floor(st['size'] * fit / text_w(text, st, st['size'])))
    w, h = text_w(text, st, fs), round(st['lh'] * fs / st['size'])
    i0, i1 = ink(text, st, fs)
    cx = left + width / 2
    return {'fs': fs, 'h': h, 'left': round(left), 'width': round(width), 'top': round(top),
            'box': [round(cx - w / 2), math.floor(top + min(0, i0)), round(cx + w / 2), math.ceil(top + max(h, i1))]}


def block(items, top0, z, maxw):
    """stacked lines at top0; if the stack crosses the safe bottom, one line at a time; if that still crosses, move
    up until it fits. items: [(text, style)]. Returns (mode, lines, warning)"""
    floor = H - z['bottom']
    for mode in ('stack', 'replace'):
        lines, top = [], top0
        for text, st in items:
            lines.append(line(text, st, top, z, maxw))
            if mode == 'stack':
                top += lines[-1]['h']
        bottom = max(ln['box'][3] for ln in lines)
        if bottom <= floor:
            return mode, lines, None
    shift = bottom - floor
    lines = [line(text, st, top0 - shift, z, maxw) for text, st in items]
    return 'replace', lines, f'moved up {shift}px from y{top0} to fit above y{floor} (check it does not cover the face)'


def default_chunks(n):
    """lead-in words small, the last one or two as the yellow punch"""
    if n <= 2:
        return [[n, 'md y']]
    emph = 2 if n >= 6 else 1
    rest = n - emph
    k = math.ceil(rest / 4)
    sizes = [rest // k + (1 if i < rest % k else 0) for i in range(k)]
    return [[s, 'sm'] for s in sizes] + [[emph, 'md y']]


def tween_in(eid, t, frm, to, dur, ease):
    return (f"gsap.set('#{eid}',{{autoAlpha:0}});"
            f"tl.fromTo('#{eid}',{{autoAlpha:0,{frm}}},{{autoAlpha:1,{to},duration:{dur},ease:'{ease}',immediateRender:false}},{t:.3f});")


def tween_out(eid, t, dur, extra=''):
    return f"tl.to('#{eid}',{{autoAlpha:0,{extra}duration:{dur}}},{t - dur:.3f});tl.set('#{eid}',{{autoAlpha:0}},{t:.3f});"


def build(spec, look, platform, segs, words, cut_words, dur, safe=False, sfx=(), gsap=GSAP_URL):
    """-> (html, layout, report). words: per-shot word lists by clip id; cut_words: medium.en on the cut."""
    if platform not in PLATFORMS:
        raise SpecError(f'unknown platform "{platform}" (have {sorted(PLATFORMS)})')
    z, maxw, warn = PLATFORMS[platform], look['max_width'], []
    n = len(segs)
    T = [s['frame'] / FPS - .002 if s['frame'] else 0.0 for s in segs]
    ids = [s['id'] for s in segs]
    over = spec.get('sentences') or [{}] * n

    def seg_end(g):
        return T[g + 1] if g + 1 < n else dur

    def src_to_cut(g, t):
        return segs[g]['frame'] / FPS + (t - segs[g]['src_in'])

    flat = [(g, j) for g in range(n) for j in range(len(segs[g]['tokens']))]
    cut_ix = align([segs[g]['tokens'][j] for g, j in flat], cut_words)
    from_cut = {f: cut_words[i]['start'] for f, i in zip(flat, cut_ix) if i is not None}

    def word_t(g, j):
        t = from_cut.get((g, j))
        if t is not None and T[g] - .02 <= t < seg_end(g):
            return t
        for k in range(j, -1, -1):   # this word, else the closest earlier word the shot transcript has
            wi = segs[g]['word_idx'][k]
            if wi is not None:
                return src_to_cut(g, words[segs[g]['clip']][wi]['start'])
        return T[g]

    last = segs[-1]
    end_t = src_to_cut(n - 1, words[last['clip']][last['word_idx'][-1]]['end']) + END_GAP
    cards = spec.get('cards') or {}
    end = cards.get('end')
    if end and dur - end_t < 1.0:
        raise SpecError(f'end card only gets {dur - end_t:.2f}s after the last word: raise cards.end.hold')

    def sid(name, what):
        if name not in ids:
            raise SpecError(f'{what}: no sentence "{name}" (have {ids})')
        return ids.index(name)

    def until(name, what):
        return end_t if name == 'end' else T[sid(name, what)]

    def case(t):
        return {'lower': t.lower(), 'upper': t.upper()}.get(look.get('case'), t)

    out_html, tw, layout, modes = [], [], [], {}

    # captions: one block per sentence, chunk by chunk on the spoken word
    for g, seg in enumerate(segs):
        toks = seg['tokens']
        chunks = over[g].get('chunks') or default_chunks(len(toks))
        if sum(c for c, _ in chunks) != len(toks):
            raise SpecError(f'sentence {seg["id"]}: chunks cover {sum(c for c, _ in chunks)} words, it has {len(toks)}: {seg["line"]}')
        top0 = spec['shots'][int(seg['clip'][1:]) - 1].get('caption_top', 1000)
        items, starts, i = [], [], 0
        for cnt, cls in chunks:
            items.append((case(' '.join(toks[i:i + cnt])), style(look, cls), cls))
            starts.append(max(T[g], word_t(g, i) - LEAD))
            i += cnt
        mode, lines, w = block([(t, st) for t, st, _ in items], top0, z, maxw)
        modes[seg['id']] = mode
        if w:
            warn.append(f'{seg["id"]}: {w}')
        b = end_t if g == n - 1 else seg_end(g)
        for k, ((text, _, cls), ln, t) in enumerate(zip(items, lines, starts)):
            eid = f'w{g}-{k}'
            out_html.append(f'<div id="{eid}" class="dw {cls}" style="left:{ln["left"]}px;width:{ln["width"]}px;top:{ln["top"]}px;'
                            f'font-size:{ln["fs"]}px">{html.escape(text, quote=False)}</div>')
            tw.append(tween_in(eid, t, "scale:1.22,filter:'blur(22px)'", "scale:1,filter:'blur(0px)'", .2, 'power3.out'))
            gone = b if mode == 'stack' or k == len(items) - 1 else max(t + .2, starts[k + 1])
            if gone < dur - .01:
                tw.append(tween_out(eid, gone, .12 if gone == b else .08, "scale:.96,filter:'blur(16px)'," if gone == b else ''))
            layout.append({'id': eid, 'kind': 'caption', 'box': ln['box'], 't0': round(t, 3), 't1': round(min(gone, dur), 3)})

    # lesson structure: step labels above the head band, from one sentence through another
    lab = {**look['label'], 'style': 'normal', **LABEL}
    for k, s in enumerate(cards.get('steps', [])):
        a, j = T[sid(s['from'], f'step {k + 1}')], sid(s['to'], f'step {k + 1}')
        b = seg_end(j)
        ln = line(s['text'].upper(), lab, z['top'] + 16, z, maxw)
        out_html.append(f'<div id="st{k}" class="step" style="left:{ln["left"]}px;width:{ln["width"]}px;top:{ln["top"]}px;'
                        f'font-size:{ln["fs"]}px">{html.escape(s["text"], quote=False)}</div>')
        tw.append(tween_in(f'st{k}', a, 'y:-10', 'y:0', .3, 'power2.out'))
        if b < dur - .01:
            tw.append(tween_out(f'st{k}', b, .12))
        layout.append({'id': f'st{k}', 'kind': 'step', 'box': ln['box'], 't0': round(a, 3), 't1': round(min(b, dur), 3)})

    # chip rows: each chip pops on its word (or at an offset into its sentence), the row leaves at `until`
    chip = {**look['label'], 'style': 'normal', 'size': CHIP['size'], 'lh': CHIP['lh'], 'ls': CHIP['ls']}
    for r, row in enumerate(cards.get('chips', [])):
        rid, top, pops, labels = f'ch{r}', z['top'] + 80, [], []
        for it in row['items']:
            g = sid(it['sentence'], f'chip "{it.get("text", it.get("word"))}"')
            if 'word' in it:
                hits = [j for j, tok in enumerate(segs[g]['tokens']) if norm(tok) == norm(it['word'])]
                if len(hits) < it.get('nth', 1):
                    raise SpecError(f'chip: "{it["word"]}" is not said in sentence {ids[g]}: {segs[g]["line"]}')
                pops.append(word_t(g, hits[it.get('nth', 1) - 1]))
            else:
                pops.append(T[g] + it['offset'])
            labels.append((it.get('text', it.get('word')), it.get('accent')))
        texts = [(t + (' ' + a if a else '')).upper() for t, a in labels]
        fixed = CHIP['pad'] * len(texts) + CHIP['gap'] * (len(texts) - 1)
        left, width, fit = column(z, top, top + CHIP['lh'], maxw)
        need = sum(text_w(t, chip, CHIP['size']) for t in texts)
        fs = min(CHIP['size'], math.floor(CHIP['size'] * (fit - fixed) / need))
        rw = need * fs / CHIP['size'] + fixed
        h = round(CHIP['lh'] * fs / CHIP['size'])
        cx = left + width / 2
        spans = ''.join(f'<span id="{rid}-{k}" class="chip" style="font-size:{fs}px">{html.escape(t, quote=False)}'
                        + (f' <b>{html.escape(a, quote=False)}</b>' if a else '') + '</span>' for k, (t, a) in enumerate(labels))
        out_html.append(f'<div id="{rid}" class="chips" style="left:{round(left)}px;width:{round(width)}px;top:{top}px">{spans}</div>')
        b = until(row['until'], f'chip row {r + 1}')
        for k, t in enumerate(pops):
            tw.append(tween_in(f'{rid}-{k}', t - .03, 'scale:.6', 'scale:1', .3, 'back.out(2)'))
        tw.append(tween_out(rid, b, .15))
        layout.append({'id': rid, 'kind': 'chips', 'box': [round(cx - rw / 2), top, round(cx + rw / 2), top + h],
                       't0': round(min(pops) - .03, 3), 't1': round(b, 3)})

    # example message card, below the chin
    m = cards.get('message')
    if m:
        top = m.get('top', 790)
        x0, x1 = max(MSG['left'], z['left']), min(MSG['right'], right_edge(z, top, top + MSG['h']))
        st = {'family': 'Inter', 'weight': 600, 'style': 'normal', 'size': MSG['size']}
        fs = min(MSG['size'], math.floor(MSG['size'] * (x1 - x0 - MSG['pad']) / text_w(m['text'], st, MSG['size'])))
        a, b = T[sid(m['from'], 'message')] + .1, until(m['until'], 'message')
        out_html.append(f'<div id="msg" class="msg" style="left:{x0}px;width:{x1 - x0}px;top:{top}px"><div class="ml">'
                        f'{html.escape(m["label"], quote=False)}</div><div class="mt" style="font-size:{fs}px">'
                        f'{html.escape(m["text"], quote=False)}</div></div>')
        tw.append(tween_in('msg', a, 'y:30', 'y:0', .35, 'power3.out'))
        tw.append(tween_out('msg', b, .15))
        layout.append({'id': 'msg', 'kind': 'message', 'box': [x0, top, x1, top + MSG['h']], 't0': round(a, 3), 't1': round(b, 3)})

    # end card: the summary lines after the last word
    if end:
        items = [(case(t), style(look, c)) for t, c in end['lines']]
        mode, lines, w = block(items, end.get('top', 1020), z, maxw)
        if mode != 'stack':
            raise SpecError(f'end card lines do not fit stacked above y{H - z["bottom"]}: lower their size or end.top')
        for k, ((t, c), ln) in enumerate(zip(end['lines'], lines)):
            out_html.append(f'<div id="end{k}" class="dw {c}" style="left:{ln["left"]}px;width:{ln["width"]}px;top:{ln["top"]}px;'
                            f'font-size:{ln["fs"]}px">{html.escape(case(t), quote=False)}</div>')
            tw.append(tween_in(f'end{k}', end_t + k * .25, "scale:1.22,filter:'blur(22px)'", "scale:1,filter:'blur(0px)'", .25, 'power3.out'))
            layout.append({'id': f'end{k}', 'kind': 'end', 'box': ln['box'], 't0': round(end_t + k * .25, 3), 't1': round(dur, 3)})

    bad = [e for e in layout if not inside(z, e['box'])]
    if bad:
        raise SpecError(f'outside the {platform} safe zone: ' + '; '.join(f"{e['id']} {e['box']}" for e in bad))

    # grade, cut punch, a whoosh where the shot changes
    grade = [f"gsap.set('.g',{{filter:'{look['grade']}'}});"]
    if look.get('cut_punch', 1) > 1:
        grade += [f"tl.fromTo('#stage',{{scale:{look['cut_punch']}}},{{scale:1,duration:.32,ease:'power2.out',immediateRender:false}},{t:.3f});"
                  for t in T[1:]]
    audio = []
    if WHOOSH[0] in sfx:
        for k, g in enumerate(g for g in range(1, n) if segs[g]['clip'] != segs[g - 1]['clip']):
            d = min(WHOOSH[1], dur - T[g])
            audio.append(f'<audio id="sfx{k}" src="assets/sfx/{WHOOSH[0]}.mp3" data-start="{T[g]:.3f}" data-duration="{d:.3f}" '
                         f'data-track-index="{10 + k % 2}" data-volume="{WHOOSH[2] * SFX_GAIN:.3f}"></audio>')
    elif n > 1:
        warn.append('no assets/sfx/whoosh-short.mp3: shot changes have no whoosh')

    page = PAGE.format(gsap=gsap, css=css(look), dur=f'{dur:.3f}', audio='\n'.join(audio),
                       layers=''.join(f'<div id="fx{k}" class="fx"></div>' for k in range(len(look['layers']))),
                       body='\n'.join(out_html), guide=guide(z) if safe else '', tl='\n'.join(grade + tw))
    report = {'look': spec.get('_look'), 'platform': platform, 'duration': round(dur, 3), 'end_t': round(end_t, 3),
              'cuts': [round(t, 3) for t in T[1:]], 'caption_modes': modes, 'warnings': warn}
    return page, layout, report


def css(look):
    acc = look['accent']
    out = [f".dw{{position:absolute;z-index:8;color:{look['text']};line-height:.9;text-align:center;text-shadow:{look['shadow']};"
           'transform-origin:50% 60%;white-space:nowrap}']
    for cls, st in look['classes'].items():
        out.append(f".{cls}{{font-family:'{st['family']}';font-weight:{st['weight']};font-style:{st['style']};letter-spacing:{st['ls']}em}}")
    extra = ''.join(f"{'font-family' if k == 'family' else 'font-weight' if k == 'weight' else 'font-style' if k == 'style' else 'letter-spacing'}:"
                    f"{repr(v) if k == 'family' else str(v) + ('em' if k == 'ls' else '')};" for k, v in acc.items() if k != 'color')
    out.append(f".y{{color:{acc['color']};{extra}}}")
    lab = look['label']
    out += [f".step{{position:absolute;z-index:8;text-align:center;color:{lab['color']};font-family:'{lab['family']}';font-weight:{lab['weight']};"
            f"letter-spacing:{LABEL['ls']}em;text-transform:uppercase;text-shadow:0 2px 12px rgba(0,0,0,.6);white-space:nowrap}}",
            f".chips{{position:absolute;z-index:8;display:flex;justify-content:center;gap:{CHIP['gap']}px}}",
            f".chip{{display:inline-block;font-family:'{lab['family']}';font-weight:{lab['weight']};letter-spacing:{CHIP['ls']}em;"
            f"text-transform:uppercase;color:{lab['color']};white-space:nowrap;background:rgba(12,12,14,.62);"
            "border:2px solid rgba(255,255,255,.22);border-radius:999px;padding:12px 22px}",
            f".chip b{{color:{acc['color']};font-weight:{lab['weight']}}}",
            ".msg{position:absolute;z-index:8;background:#fff;border-radius:28px;padding:22px 30px;box-shadow:0 20px 50px rgba(0,0,0,.35)}",
            ".msg .ml{font:600 22px Montserrat;letter-spacing:.18em;text-transform:uppercase;color:#8a8a8a;margin-bottom:8px}",
            ".msg .mt{font-family:Inter;font-weight:600;line-height:1.15;color:#111;white-space:nowrap}"]
    out += [f"#fx{k}{{z-index:7;background:{ly['background']};{'mix-blend-mode:' + ly['blend'] + ';' if ly.get('blend') else ''}}}"
            for k, ly in enumerate(look['layers'])]
    return '\n'.join(out)


def guide(z):
    red = 'background:rgba(255,0,0,.28)'
    boxes = [(0, 0, W, z['top']), (0, H - z['bottom'], W, H), (0, z['top'], z['left'], H - z['bottom']),
             (W - z['right'], z['top'], W, H - z['bottom'])]
    if z['strip']:
        boxes.append((W - z['strip'][0], z['strip'][1], W, H - z['bottom']))
    return ('<div style="position:absolute;inset:0;z-index:99;pointer-events:none">'
            + ''.join(f'<div style="position:absolute;left:{x0}px;top:{y0}px;width:{x1 - x0}px;height:{y1 - y0}px;{red}"></div>'
                      for x0, y0, x1, y1 in boxes) + '</div>')


PAGE = '''<!doctype html>
<html lang="en" data-resolution="portrait">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=1080, height=1920" />
<link rel="stylesheet" href="assets/fonts/fonts.css" />
<script src="{gsap}"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:1080px;height:1920px;overflow:hidden;background:#000}}
#root{{position:relative;width:1080px;height:1920px;overflow:hidden;background:#000}}
#stage{{position:absolute;inset:0;transform-origin:50% 45%}}
.full{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}
.fx{{position:absolute;inset:0;pointer-events:none}}
{css}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{dur}" data-width="1080" data-height="1920">
  <audio id="bga" src="assets/aroll.mp4" data-start="0" data-media-start="0" data-duration="{dur}" data-track-index="2" data-volume="1"></audio>
{audio}
  <div id="stage">
    <video id="bgv" class="full g" src="assets/aroll.mp4" muted playsinline data-start="0" data-media-start="0" data-duration="{dur}" data-track-index="0"></video>
  </div>
  {layers}
{body}
{guide}
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
{tl}
tl.set({{}}, {{}}, {dur});
window.__timelines["main"] = tl;
</script>
</body>
</html>
'''


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main():
    skillenv.utf8_stdio()
    proj = skillenv.path_arg(sys.argv[1])

    def load(name):
        with open(os.path.join(proj, name), encoding='utf-8') as fh:
            return json.load(fh)

    spec, segs = load('reel.json'), load('segments.json')
    look_name, platform = arg('--look') or spec.get('look', 'bold'), arg('--platform') or spec.get('platform', 'reels')
    look = load_look(look_name)
    spec['_look'] = look_name
    words = {c: load(f'words_{c}.json') for c in {s['clip'] for s in segs}}
    cut_words = load('words.json') if os.path.exists(os.path.join(proj, 'words.json')) else []
    dur = float(subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration',
                                '-of', 'csv=p=0', os.path.join(proj, 'assets', 'aroll.mp4')], **skillenv.TEXT).stdout.strip().split(',')[0])
    sfx_dir = os.path.join(proj, 'assets', 'sfx')
    sfx = {f[:-4] for f in os.listdir(sfx_dir) if f.endswith('.mp3')} if os.path.isdir(sfx_dir) else set()
    gsap = GSAP_LOCAL if os.path.exists(os.path.join(proj, GSAP_LOCAL)) else GSAP_URL
    page, layout, report = build(spec, look, platform, segs, words, cut_words, dur, safe='--safe' in sys.argv, sfx=sfx, gsap=gsap)
    if not cut_words:
        report['warnings'].append('no words.json (transcribe_cut.py): caption times come from the per-shot words only')
    with open(os.path.join(proj, 'index.html'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(page)
    with open(os.path.join(proj, 'layout.json'), 'w', encoding='utf-8') as fh:
        json.dump(layout, fh, indent=1)
    with open(os.path.join(proj, 'build-report.json'), 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=1)
    print(f"wrote index.html {dur:.3f}s  look {look_name}  platform {platform}  cuts {len(report['cuts'])}  "
          f"boxes {len(layout)} inside the safe zone  safe={'--safe' in sys.argv}")
    for w in report['warnings']:
        print('WARNING', w)


if __name__ == '__main__':
    try:
        main()
    except SpecError as e:
        sys.exit(f'reel spec error: {e}')
