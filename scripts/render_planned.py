#!/usr/bin/env python3
"""Planned mode (SPEC v1.3 modules 6-8, contracts B, C, E): a planned edit packet in, a film built from the graphic kit
out, with its resolved timeline, timing table, frame-per-word sheet and cue-frame sheet.

Usage:  PY render_packet.py <planned packet.json> --shots DIR --out DIR      (render_packet.py dispatches here when the
        packet has `graphics`). DIR holds every take and picture the packet names; each is found by its sha256.
Steps: refuse a packet pinned to another build, a graphic or persistent element the kit has no piece for (or on another
layout), a malformed one (naming its id), or a cue whose words are not the words of its line (every cue carries its words;
a repeated word needs its index), a take with no measured cut (take meta.cut, take_cuts.py) or a cut past its take's last
frame -> lay the takes end to end, each playing its cut -> build the captions from each take's words placed through its
cut and the line's caption groups -> transcribe the takes as cut (medium.en) -> resolve every cue on that render
transcript (refuse a cue word missing from it, or a hold past its line's take); a graphic enters on the first frame at or
after its cue word's onset and not before its caption shows that word -> compose index.html from the kit (takes in
wrappers the top band slides, the built captions, one piece per graphic, the step counter as one element with timed
updates) -> render (HyperFrames + GSAP at the pin, local GSAP, no CDN) -> render each graphic alone as RGBA
frames and measure the frames it is on screen (and, for the counter, the frame each update changes it) -> timing table
vs the rules -> gates by class (contract D, GATES): render the base (the film without graphics or captions, presenter
moves kept) and find the face on it; media, graphic text and annotations keep clear of that face and are visible on the
film itself at their cue frames; the captions rendered alone stay inside the Reels safe
zone; every script word is heard, in order, on the film's own audio -> sheets. Writes <out>/renders/<slug>-planned.mp4,
<out>/qa-report.json (with stage times) and <out>/evidence/* (gates.json: each check's measurements). A failure names
the graphic id (or captions, or words) and the check.
Exit: 0 every check passes, 1 a timing check failed, 2 refused (the graphic id is in the message), 3 a stage failed.
HyperFrames: VIDEO_EDIT_HYPERFRAMES_PLANNED (a hyperframes binary at the pinned version), else npx hyperframes@<pin>.
"""
import glob
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402
from edl_from_words import align, norm  # noqa: E402

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = {'hyperframes': '0.9.23', 'gsap': '3.15.0', 'kit': 'planned-r4', 'fps': 24}
# gsap@3.15.0 dist/gsap.min.js, as hyperframes 0.9.23 ships it
GSAP_SHA = '92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb'
# settled rules (SPEC module 2): main picture 2.0 s, overlays 1.5 s (a counter state is an overlay, as r3's verifier
# measured it), text 1.0 s + 0.3 s a word, no two entrances within 0.5 s
MIN_HOLD = {'photo_panel': 2.0, 'top_band_broll': 2.0, 'circle': 1.5, 'step_counter': 1.5}
TEXT_BASE, TEXT_PER_WORD = 1.0, 0.3
MIN_ENTRANCE_GAP = 0.5
ALPHA, MIN_PIXELS = 32, 16      # a measured frame shows a graphic when 16+ pixels have alpha over 32 (overlay_mask's rule)
UPDATE_WINDOW = 6               # frames either side of a counter update's cue frame searched for the change it makes
EPS = 0.0005                    # hosts and main-timeline events start this much before their frame's time, so a time-0 set
                                # (or one at an exact frame time rounded up to 6 decimals) applies on that frame
FONTS = ('Poppins-800-latin.woff2', 'Caveat-700-latin.woff2')
# contract D: gate policies by class, measured on renders. Captions keep the existing safe zone (build_reel's Reels zone,
# on the captions' rendered ink); graphic text, annotations and (lead 10 Nov) media panels and bands get face clearance
# and a visible-on-the-film check at their cue frames. The top band's exemption from the Reels top margin is named here.
GATES_NOTE = ("safe zone for captions only: graphics' and media's positions are recorded, not enforced (r3, approved by "
              'Andy, puts its circle under the Reels bottom margin and its photo panel past the right margin)')
GATES = {
    'captions': {'class': 'caption', 'checks': ['safe zone'], 'zone': 'reels'},
    'photo_panel': {'class': 'media panel', 'checks': ['face clearance', 'visible on the film']},
    'top_band_broll': {'class': 'media band', 'checks': ['face clearance', 'visible on the film'],
                       'exemptions': ["the Reels top margin (y < 220): the band sits at the top of the frame, as r3's shelf "
                                      'band does at top:0']},
    'circle': {'class': 'annotation', 'checks': ['face clearance', 'visible on the film']},
    'step_counter': {'class': 'graphic text', 'checks': ['face clearance', 'visible on the film']},
}
FACE_STEP = 3                   # face clearance samples every 3rd frame, as spec mode's reel_check
VIS_DIFF = 40                   # a graphic pixel counts when it changes the picture under it by more than this (0-255)
VIS_SHARE = .75                 # visible on the film: this share of those pixels show the graphic, not the picture under it


class Refused(Exception):
    pass


def load_kit(name=PIN['kit']):
    with open(os.path.join(SK, 'kits', name, 'kit.json'), encoding='utf-8') as fh:
        return json.load(fh)


def caption_words(page):
    """[(word, the second it appears)] of a captions page (one span per script word in order)"""
    words = re.findall(r'<span class="cw[^"]*" id="cw(\d+)">([^<]*)</span>', page)
    at = {int(k): float(t) for k, t in re.findall(r'tl\.fromTo\("#cw(\d+)",.*?\}, ([\d.]+)\);', page)}
    return [(html.unescape(w), at[int(k)]) for k, w in words]


def caption_frames(page, fps):
    """[(word, the first frame its caption shows)]"""
    return [(w, first_frame(t, fps)) for w, t in caption_words(page)]


def caption_at(caps, frame):
    """the caption word shown on a frame: the latest whose first frame is at or before it"""
    shown = [w for w, f in caps if f <= frame]
    return shown[-1] if shown else None


def first_frame(t, fps):
    """the first frame at or after a moment"""
    return math.ceil(t * fps - 1e-6)


# ------------------------------------------------------------------ takes and captions (ticket 11)
def layout(pk, fps):
    """({line id: its take's place in the film}, the film's frames): the takes end to end, each playing its measured cut
    (take meta.cut: fromFrame, toFrame at fps, from take_cuts.py; the plan checks timed the plan on the same cut)"""
    place, at = {}, 0
    for ln in pk['lines']:
        c = ((ln.get('take') or {}).get('meta') or {}).get('cut')
        if not isinstance(c, dict) or not all(isinstance(c.get(k), int) and not isinstance(c.get(k), bool) for k in ('fromFrame', 'toFrame')):
            raise Refused(f"line {ln.get('id')}: its take {(ln.get('take') or {}).get('id')} has no measured cut (take meta.cut from "
                          'take_cuts.py)')
        if c.get('fps') != fps:
            raise Refused(f"line {ln['id']}: its cut is at {c.get('fps')} fps, the film at {fps}")
        a, b = c['fromFrame'], c['toFrame']
        if not 0 <= a < b:
            raise Refused(f"line {ln['id']}: its cut {a}-{b} is empty")
        place[ln['id']] = {'start': at / fps, 'mediaStart': a / fps, 'duration': (b - a) / fps, 'startFrame': at, 'fromFrame': a,
                           'toFrame': b, 'frames': b - a, 'speech': c.get('speech')}
        at += b - a
    return place, at


def check_cuts(pk, frames):
    """refuses a cut past its take's last frame. frames: {take sha256: its frame count}"""
    for ln in pk['lines']:
        n, b = frames[ln['take']['sha256']], ln['take']['meta']['cut']['toFrame']
        if b > n:
            raise Refused(f"line {ln['id']}: its cut ends on frame {b}, past its take's {n} frames")


PUNCT = tuple(',.?!;:')


def default_groups(text):
    """caption groups for a line without its own: a break after punctuation or every three words; the word that ends a
    phrase (its punctuation) is the group's accent, where most of r3's accents fall ("full?", "phone.", "room,", "page.")"""
    groups, cur = [], []
    for t in text.split():
        cur.append(t + ('*' if t.endswith(PUNCT) else ''))
        if t.endswith(PUNCT) or len(cur) == 3:
            groups.append(' '.join(cur))
            cur = []
    return ' | '.join(groups + ([' '.join(cur)] if cur else []))


def caption_groups(pk, place, fps):
    """[[(word, accent, start, end)]] in film seconds: each line's caption groups ("|" splits, "*" marks the accent;
    default_groups without them), its words timed by its take's words placed through its cut (r3's gencap.py: the first
    word at the take's measured speech start); words the take's words do not have are spread evenly from the end of the
    word heard before them (or the speech start) to the start of the word heard after them (or the cut's end less 0.12 s).
    Refuses a line whose groups are not its words or whose take has no words"""
    out = []
    for ln in pk['lines']:
        spec = ln.get('captions') or default_groups(ln['text'])
        if not isinstance(spec, str):
            raise Refused(f"line {ln['id']}: its caption groups are not a string")
        groups = [g.split() for g in spec.split('|')]
        flat = [t.rstrip('*') for g in groups for t in g]
        if [norm(t) for t in flat] != [norm(t) for t in ln['text'].split()] or not all(groups):
            raise Refused(f"line {ln['id']}: its caption groups '{spec}' are not its words")
        p, words = place[ln['id']], ln['take'].get('words') or []
        if not words:
            raise Refused(f"line {ln['id']}: its take {ln['take'].get('id')} has no words (take_cuts.py measures them)")
        heard = align(flat, [{'text': w['word'], 'start': w['start'], 'end': w['end']} for w in words])
        speech = (p.get('speech') or [None])[0]
        m0, ms = p['startFrame'] / fps, p['fromFrame'] / fps
        local, prev, gap = [], speech if speech is not None else ms, []
        for k, i in enumerate(heard + [-1]):           # -1: the cut's end closes a gap at the line's tail
            if i is None:
                gap.append(k)
                continue
            if i == -1:
                nxt = p['toFrame'] / fps - 0.12
            else:
                nxt = words[i]['start']
                if k == 0 and speech is not None:
                    nxt = max(speech, nxt)
            for j, _ in enumerate(gap):
                at = prev + (max(nxt, prev) - prev) * j / len(gap)
                local.append((at, at))
            gap = []
            if i != -1:
                local.append((nxt, words[i]['end']))
                prev = words[i]['end']
        times = [(round(m0 + st - ms, 3), round(m0 + en - ms, 3)) for st, en in local]
        k = 0
        for g in groups:
            out.append([(t.rstrip('*'), t.endswith('*'), *times[k + j]) for j, t in enumerate(g)])
            k += len(g)
    return out


CAPTION_MAX_W = 810     # px of glyphs: centred, the ink ends by x 945, the Reels action column's edge from y 1155 (which the
                        # caption band reaches); rendered ink matched this width to the pixel on face23be l3 (983 predicted, 983
                        # measured), and r3's widest group ("another room," 806 px, ink to 943) passed the gate


def caption_width(words, size):
    """a caption group's width in px at a font size, from Poppins 800's own glyph advances: each word with the page's
    -0.01 em letter spacing, a space and 0.4 em of word margins between words"""
    from PIL import ImageFont
    ft = ImageFont.truetype(os.path.join(SK, 'kits', PIN['kit'], 'fonts', 'Poppins-800-latin.woff2'), size)
    return sum(ft.getlength(w) - 0.01 * size * (len(w) - 1) for w in words) + (ft.getlength(' ') + 0.4 * size) * (len(words) - 1)


def caption_size(words):
    """r3's size (104 px, or 820 px over 0.6 em a character), made smaller until the group fits CAPTION_MAX_W"""
    size = min(104, int(820 / (len(' '.join(words)) * 0.6)))
    while size > 1 and caption_width(words, size) > CAPTION_MAX_W:
        size -= 1
    return size


def captions_page(groups, end):
    """the kit's captions page for these groups (r3's gencap.py timing): a group shows 0.02 s before its first word (not
    before 0.12 s), each word pops in at its start, and the group leaves 0.3 s after its last word (at least 0.2 s after
    its start) or as the next group shows; the last stays to the end; each group at caption_size"""
    body, js, wid = [], [], 0
    for gi, grp in enumerate(groups):
        txt = ' '.join(t for t, _, _, _ in grp)
        spans = []
        for t, acc, _, _ in grp:
            spans.append(f'<span class="cw{" acc" if acc else ""}" id="cw{wid}">{html.escape(t, quote=False)}</span>')
            wid += 1
        body.append(f'    <div class="cg" id="cg{gi}" style="font-size:{caption_size(txt.split())}px">' + ' '.join(spans) + '</div>')
    wid = 0
    for gi, grp in enumerate(groups):
        start = max(0.12, grp[0][2] - 0.02)
        if gi + 1 < len(groups):
            stop = min(groups[gi + 1][0][2] - 0.02, max(grp[-1][3], grp[-1][2] + 0.2) + 0.3)
        js.append(f'    tl.set("#cg{gi}", {{ opacity: 1, visibility: "visible" }}, {start:.3f});')
        for _, acc, s, _ in grp:
            at = max(s, start)
            if acc:
                js.append(f'    tl.fromTo("#cw{wid}", {{ opacity: 0, scale: 0.6, filter: "blur(8px)" }}, {{ opacity: 1, scale: 1.06, '
                          f'filter: "blur(0px)", duration: 0.18, ease: "back.out(2)" }}, {at:.3f});')
            else:
                js.append(f'    tl.fromTo("#cw{wid}", {{ opacity: 0, scale: 0.86, filter: "blur(6px)" }}, {{ opacity: 1, scale: 1, '
                          f'filter: "blur(0px)", duration: 0.13, ease: "expo.out" }}, {at:.3f});')
            wid += 1
        if gi + 1 < len(groups):
            js.append(f'    tl.to("#cg{gi}", {{ opacity: 0, duration: 0.08, ease: "power2.in" }}, {stop - 0.08:.3f});')
            js.append(f'    tl.set("#cg{gi}", {{ opacity: 0, visibility: "hidden" }}, {stop:.3f});')
    return fill(kit_file(load_kit(), 'captions.html'), {'__GROUPS__': '\n'.join(body), '__TIMELINE__': '\n'.join(js),
                                                         '__END__': f'{end:.6f}'})


# ------------------------------------------------------------------ cues (contract B)
def cue_span(line_text, cue, owner):
    """(from, to) word indices of a cue in its line. The cue carries the words it copied from the line (case and
    punctuation ignored); with an index range they must be the words there, without one they must occur exactly once."""
    toks = [norm(t) for t in line_text.split()]
    lo, hi = cue.get('from'), cue.get('to')
    want = [norm(t) for t in str(cue.get('words') or '').split() if norm(t)]
    if not want:
        raise Refused(f"{owner}: cue on line {cue.get('lineId')} has no words (every cue carries the words it copied from its line)")
    if lo is not None or hi is not None:
        if not isinstance(lo, int) or not isinstance(hi, int) or not 0 <= lo <= hi < len(toks):
            raise Refused(f"{owner}: cue words {lo}-{hi} are not in line {cue['lineId']} ({len(toks)} words)")
        if toks[lo:hi + 1] != want:
            raise Refused(f"{owner}: cue words '{cue['words']}' are not words {lo}-{hi} of line {cue['lineId']} "
                          f"('{' '.join(line_text.split()[lo:hi + 1])}')")
        return lo, hi
    hits = [i for i in range(len(toks) - len(want) + 1) if toks[i:i + len(want)] == want]
    if not hits:
        raise Refused(f"{owner}: cue words '{cue['words']}' are not in line {cue['lineId']}")
    if len(hits) > 1:
        raise Refused(f"{owner}: cue words '{cue['words']}' occur {len(hits)} times in line {cue['lineId']} "
                      f"(words {', '.join(map(str, hits))}); the cue must give its word index")
    return hits[0], hits[0] + len(want) - 1


def check_pin(pk):
    eng = (pk.get('versions') or {}).get('engine') or {}
    off = [f'{k} {eng.get(k)} (this engine: {v})' for k, v in PIN.items() if eng.get(k) != v]
    if off:
        raise Refused('the packet is pinned to another render build: ' + '; '.join(off))


def require(obj, owner, **kinds):
    """refuses (naming the owner) when obj is not a dict holding each key with a value of its kind"""
    if not isinstance(obj, dict):
        raise Refused(f'{owner}: malformed (not an object)')
    for key, kind in kinds.items():
        if not isinstance(obj.get(key), kind) or isinstance(obj.get(key), bool) and kind is not bool:
            raise Refused(f'{owner}: malformed: no {key}')


def unsupported(owner, what, kit):
    names = ', '.join(f"{t} on {s['layout']}" for t, s in kit['treatments'].items())
    return Refused(f"{owner}: {what} is unsupported in kit {kit['kit']} (its pieces: {names})")


def target_box(take, target_id, owner):
    """the box of a take's stored annotation target (in the take's pixels) in the 1080x1920 frame, the take placed cover"""
    for t in (take.get('meta') or {}).get('targets', []):
        if t.get('id') == target_id:
            w, h = take['width'], take['height']
            s = max(1080 / w, 1920 / h)
            ox, oy = (1080 - w * s) / 2, (1920 - h * s) / 2
            x0, y0, x1, y1 = t['box']
            return [x0 * s + ox, y0 * s + oy, x1 * s + ox, y1 * s + oy]
    raise Refused(f"{owner}: target {target_id} is not stored with take {take.get('id')}")


def check_pictures(gr, line, spec, kit):
    gid, treatment, assets = gr['id'], gr['treatment'], gr['assets']
    lo, hi = spec.get('pictures', [0, 0])
    if not lo <= len(assets) <= hi:
        raise unsupported(gid, f'{treatment} with {len(assets)} pictures (its piece takes {lo}-{hi})', kit)
    for a in assets:
        require(a, f'{gid}: picture {a.get("id") if isinstance(a, dict) else a}', id=str, sha256=str, storagePath=str)
    if treatment == 'top_band_broll':
        meta = assets[0].get('meta') or {}
        if not all(isinstance(meta.get(k), (int, float)) for k in ('mediaStart', 'mediaEnd')) or len(meta.get('frame') or []) != 2:
            raise Refused(f"{gid}: b-roll {assets[0]['id']} has no trim (meta.mediaStart, meta.mediaEnd) or frame")
        if 'bandTransition' in meta:
            if meta['bandTransition'] != 'dissolve':
                raise Refused(f"{gid}: unknown band transition")
            height = meta.get('bandHeightPx')
            if type(height) is not int or not 240 <= height <= 555:
                raise Refused(f"{gid}: a stationary dissolve needs a measured band height between 240 and 555 px")
            if any(type(n) not in (int, float) or not math.isfinite(n) or n <= 0 for n in meta['frame']):
                raise Refused(f"{gid}: the dissolve source needs positive frame dimensions")
    if treatment == 'photo_panel':
        for a in assets:
            require(a, f"{gid}: picture {a['id']}", width=(int, float), height=(int, float))
        ids = {a['id']: a for a in assets}
        for wr in gr.get('writing') or []:
            check_writing(gid, wr, ids.get(wr.get('assetId')))
    if treatment == 'circle':
        if not isinstance(gr.get('target'), str):
            raise Refused(f'{gid}: a circle needs a target (a box stored with its take)')
        target_box(line['take'], gr['target'], gid)


def check_writing(gid, wr, a):
    """kit film3-r3.3: handwriting on one of the panel's stills, within the still's own limits (meta.writing): a heading or
    centred line (text, at most maxChars), short lines on its rules (lines, at most lines.max of lines.maxChars each)"""
    m = ((a or {}).get('meta') or {}).get('writing')
    owner = f"{gid}: writing on {wr.get('assetId') if isinstance(wr, dict) else wr}"
    if not isinstance(wr, dict) or not m:
        raise Refused(f"{owner}: not one of its pictures with a writing area (meta.writing)")
    text, lines = wr.get('text'), wr.get('lines')
    if text is not None and not isinstance(text, str) or lines is not None and (
            not isinstance(lines, list) or not all(isinstance(x, str) and x.strip() for x in lines)):
        raise Refused(f"{owner}: malformed (text is a string, lines a list of strings)")
    if not (text or '').strip() and not lines:
        raise Refused(f"{owner}: writes nothing (give text or lines)")
    if text and m.get('maxChars') and len(text) > m['maxChars']:
        raise Refused(f"{owner}: '{text}' is longer than its {m['maxChars']} characters")
    if lines:
        spec = m.get('lines')
        if not spec or not m.get('rules'):
            raise Refused(f"{owner}: it has no ruled lines to write on")
        if len(lines) > spec['max']:
            raise Refused(f"{owner}: {len(lines)} lines; the page takes at most {spec['max']} lines")
        for x in lines:
            if len(x) > spec['maxChars']:
                raise Refused(f"{owner}: line '{x}' is longer than its {spec['maxChars']} characters")


def check_graphic(gr, lines, kit):
    require(gr, 'a graphic', id=str)
    gid = gr['id']
    require(gr, gid, treatment=str, layout=str, cue=dict, holdSec=(int, float), assets=list)
    spec = kit['treatments'].get(gr['treatment'])
    if not spec or spec.get('persistent') or gr['layout'] != spec['layout']:
        raise unsupported(gid, f"treatment {gr['treatment']} on layout {gr['layout']}", kit)
    if gr['treatment'] not in GATES:
        raise unsupported(gid, f"treatment {gr['treatment']} with no gate policy (contract D)", kit)
    line = lines.get(gr['cue'].get('lineId'))
    if not line:
        raise Refused(f"{gid}: cue line {gr['cue'].get('lineId')} is not in the packet")
    cue_span(line['text'], gr['cue'], gid)
    check_pictures(gr, line, spec, kit)


def check_persistent(p, lines, kit):
    """the step counter: one element, its updates "1/N" first, then each "k/N" in order or "pulse" (the next empty slot)"""
    require(p, 'a persistent element', id=str)
    pid = p['id']
    require(p, pid, treatment=str, layout=str, updates=list, untilLineEnd=str)
    spec = kit['treatments'].get(p['treatment'])
    if not spec or not spec.get('persistent') or p['layout'] != spec['layout']:
        raise unsupported(pid, f"persistent {p['treatment']} on layout {p['layout']}", kit)
    if p['treatment'] not in GATES:
        raise unsupported(pid, f"persistent {p['treatment']} with no gate policy (contract D)", kit)
    if p['untilLineEnd'] not in lines:
        raise Refused(f"{pid}: untilLineEnd {p['untilLineEnd']} is not a line of the packet")
    steps, count = kit['stepCounter']['steps'], 0
    if not p['updates']:
        raise Refused(f'{pid}: no updates')
    for i, u in enumerate(p['updates']):
        require(u, f'{pid} update {i + 1}', cue=dict, text=str)
        owner = f"{pid} update {i + 1} ({u['text']})"
        line = lines.get(u['cue'].get('lineId'))
        if not line:
            raise Refused(f"{owner}: cue line {u['cue'].get('lineId')} is not in the packet")
        cue_span(line['text'], u['cue'], owner)
        if u['text'] == f'{count + 1}/{steps}':
            count += 1
        elif u['text'] != 'pulse' or count in (0, steps):
            want = f'{count + 1}/{steps}' + ('' if count in (0, steps) else ' or pulse')
            raise unsupported(pid, f"update '{u['text']}' (the counter shows {want} next)", kit)


def check_packet(pk, kit):
    """contract C: every graphic and persistent element is a kit piece on its layout, with its cue words on its line"""
    lines = {}
    for i, ln in enumerate(pk.get('lines') or []):
        require(ln, f'line {i + 1}', id=str, text=str, take=dict)
        lines[ln['id']] = ln
    for gr in pk.get('graphics') or []:
        check_graphic(gr, lines, kit)
    for p in pk.get('persistent') or []:
        check_persistent(p, lines, kit)


def text_words(gr):
    """every handwritten word on the panel (heading/centred text and lines); the scribble has no words"""
    return sum(len((w.get('text') or '').split()) + sum(len(x.split()) for x in w.get('lines') or []) for w in gr.get('writing') or [])


def resolve(pk, transcript, place, fps, captions=None):
    """every graphic and persistent element placed on the render transcript: the first cue word's onset, the frame it
    enters on (the first frame at or after the onset and, with the captions, not before the frame its caption shows
    that word) and its frames (its hold, or to its line's cut); refuses a cue word the transcript does not have, a hold
    past its line's take, a b-roll trim shorter than its hold, or captions that are not the packet's words"""
    tokens, first = [], {}
    for ln in pk['lines']:
        first[ln['id']] = len(tokens)
        tokens += ln['text'].split()
    if captions is not None and [norm(w) for w, _ in captions] != [norm(t) for t in tokens]:
        raise Refused(f'the captions are not the words of this packet ({len(captions)} caption words, {len(tokens)} script words)')
    heard = align(tokens, transcript)
    lines = {ln['id']: ln for ln in pk['lines']}

    def onset(cue, owner):
        lid = cue['lineId']
        lo, hi = cue_span(lines[lid]['text'], cue, owner)
        k = heard[first[lid] + lo]
        if k is None:
            raise Refused(f"{owner}: cue word '{lines[lid]['text'].split()[lo]}' (line {lid} word {lo}) is not on the render "
                          f"transcript")
        on, i = transcript[k]['start'], first[lid] + lo
        out = {'lineId': lid, 'from': lo, 'to': hi, 'words': ' '.join(lines[lid]['text'].split()[lo:hi + 1]), 'onset': on,
               'heardAs': transcript[k]['text'], 'frame': first_frame(on, fps)}
        if captions is not None:
            out.update(captionIndex=i, captionFrame=captions[i][1], frame=max(out['frame'], captions[i][1]))
        return out

    def cut(lid):
        return round((place[lid]['start'] + place[lid]['duration']) * fps)

    out = []
    for gr in pk['graphics']:
        c = onset(gr['cue'], gr['id'])
        start, end = c.pop('frame'), cut(c['lineId'])
        frames = end - start if gr.get('holdUntilLineEnd') else round(gr['holdSec'] * fps)
        if start + frames > end:
            raise Refused(f"{gr['id']}: hold {gr['holdSec']} s from {start / fps:.3f} s runs past line {c['lineId']}'s take "
                          f"(ends {end / fps:.3f} s)")
        for a in gr['assets'] if gr['treatment'] == 'top_band_broll' else []:
            m = a['meta']
            if m['mediaStart'] + frames / fps > m['mediaEnd'] + 1e-6:
                raise Refused(f"{gr['id']}: b-roll {a['id']} runs out: {frames / fps:.3f} s from {m['mediaStart']} s is past its "
                              f"trim out {m['mediaEnd']} s")
        out.append({'id': gr['id'], 'treatment': gr['treatment'], **c, 'startFrame': start, 'frames': frames,
                    'holdSec': gr['holdSec'], 'untilCut': bool(gr.get('holdUntilLineEnd')), 'cutFrame': end,
                    'textWords': text_words(gr)})
    for p in pk.get('persistent') or []:
        ups = [{'text': u['text'], **onset(u['cue'], f"{p['id']} update {i + 1} ({u['text']})")} for i, u in enumerate(p['updates'])]
        end = cut(p['untilLineEnd'])
        for a, b in zip(ups, ups[1:]):
            if b['frame'] <= a['frame']:
                raise Refused(f"{p['id']}: update {b['text']} (frame {b['frame']}) is not after update {a['text']} (frame {a['frame']})")
        if end <= ups[-1]['frame']:
            raise Refused(f"{p['id']}: its last update (frame {ups[-1]['frame']}) is not before the end of {p['untilLineEnd']}")
        c = {k: v for k, v in ups[0].items() if k not in ('text', 'frame')}
        out.append({'id': p['id'], 'treatment': p['treatment'], **c, 'startFrame': ups[0]['frame'], 'frames': end - ups[0]['frame'],
                    'holdSec': (end - ups[0]['frame']) / fps, 'untilCut': True, 'cutFrame': end, 'textWords': 1, 'updates': ups})
    return out


# ------------------------------------------------------------------ timing table (contract E, settled rules)
def text_min(n):
    return TEXT_BASE + TEXT_PER_WORD * n if n else None


def hold_rules(owner, label, hold, min_hold, words):
    """failures of a measured hold against the overlay/main-picture rule and the text rule"""
    out = []
    if min_hold is not None and hold + 1e-9 < min_hold:
        out.append(f'{owner}: on screen {hold:.3f} s; the rule for {label} is >= {min_hold:.1f} s')
    if words and hold + 1e-9 < text_min(words):
        out.append(f'{owner}: its text ({words} words) reads {hold:.3f} s; the rule is >= {text_min(words):.1f} s')
    return out


def update_rows(r, first, last, regions, fps, captions=None):
    """one row per counter update vs its cue frame: a state ("k/N") from the frame its number changes (the element's
    first frame for the first) until the next state; a pulse while its slot glows, until the next update"""
    rows, failures, seen, slot, count = [], [], [], [], 0
    ups = r['updates']
    for u in ups:
        count += u['text'] != 'pulse'
        slot.append(count + 1 if u['text'] == 'pulse' else None)
    for i, u in enumerate(ups):
        src = regions.get(f'slot{slot[i]}' if slot[i] else 'number', [])
        hit = [f for f in src if u['frame'] - UPDATE_WINDOW <= f <= u['frame'] + UPDATE_WINDOW]
        seen.append(first if i == 0 else hit[0] if hit else None)
    for i, (u, on) in enumerate(zip(ups, seen)):
        row = {'id': r['id'], 'update': u['text'], 'treatment': r['treatment'],
               'cue': {k: u.get(k) for k in ('lineId', 'from', 'to', 'words')},
               'cueOnsetSec': u['onset'], 'cueOnsetFrame': u['frame'], 'minHoldSec': MIN_HOLD['step_counter']}
        owner = f"{r['id']} {u['text']}"
        if on is None:
            failures.append(f"{owner}: no change within {UPDATE_WINDOW} frames of its cue frame {u['frame']}")
            rows.append({**row, 'pass': False})
            continue
        nxt = [s for v, s in zip(ups[i + 1:], seen[i + 1:]) if s is not None and (u['text'] == 'pulse' or v['text'] != 'pulse')]
        if u['text'] == 'pulse':
            stop = nxt[0] if nxt else last + 1
            end = max([f for f in regions.get(f'slot{slot[i]}', []) if on <= f < stop] or [on])
        else:
            end = (nxt[0] - 1) if nxt else last
        hold = (end - on + 1) / fps
        row.update({'measuredFirstFrame': on, 'measuredLastFrame': end, 'measuredHoldSec': round(hold, 6),
                    'onsetDeltaFrames': on - u['frame']})
        bad = hold_rules(owner, 'a counter ' + ('pulse' if u['text'] == 'pulse' else 'state'), hold, MIN_HOLD['step_counter'],
                         0 if u['text'] == 'pulse' else 1)
        if on != u['frame']:
            bad.append(f"{owner}: changes on frame {on}, the onset of '{u['words'].split()[0]}' is frame {u['frame']} "
                       f"({on - u['frame']:+d} frames)")
        bad += caption_rule(owner, u, on, captions, row)
        failures += bad
        rows.append({**row, 'pass': not bad})
    return rows, failures


def entry_frame(r, fps, captions):
    """the frame a graphic must first show on: the first frame at or after its cue word's onset, and with the kit's
    captions not before its caption shows that word"""
    f = first_frame(r['onset'], fps)
    return max(f, captions[r['captionIndex']][1]) if captions is not None and 'captionIndex' in r else f


def caption_rule(owner, r, first, captions, row):
    """lead 10 Nov: never on a frame whose caption still shows the previous word"""
    if captions is None or 'captionIndex' not in r:
        return []
    want, shown = captions[r['captionIndex']][0], caption_at(captions, first)
    row['captionAtFirstFrame'] = shown
    if norm(shown or '') != norm(want):
        return [f"{owner}: on its first frame {first} the caption shows '{shown}', not its cue word '{want}'"]
    return []


def timing_table(resolved, measured, fps, captions=None):
    """rows per graphic (and per counter update) from the frames it was measured on screen, and the rule failures.
    Hold = first visible frame to last visible frame; the fully-in-place frame (the first frame from which its outline
    stops moving) is recorded as information only."""
    rows, failures, entrances = [], [], []
    for r in resolved:
        ms = measured.get(r['id']) or {}
        fr, moving = sorted(ms.get('shown', [])), set(ms.get('moving', []))
        cue_frame = entry_frame(r, fps, captions)
        row = {'id': r['id'], 'treatment': r['treatment'], 'cue': {'lineId': r['lineId'], 'from': r['from'], 'to': r['to'],
               'words': r['words']}, 'cueOnsetSec': r['onset'], 'cueOnsetFrame': cue_frame, 'plannedStartFrame': r['startFrame'],
               'plannedFrames': r['frames'], 'untilCut': r.get('untilCut', False), 'minHoldSec': MIN_HOLD.get(r['treatment']),
               'minTextSec': text_min(r.get('textWords', 0))}
        if not fr:
            failures.append(f"{r['id']}: not on screen in any measured frame")
            rows.append({**row, 'pass': False})
            continue
        first, last = fr[0], fr[-1]
        hold = (last - first + 1) / fps
        fully = next((f for f in fr if f + 1 not in moving), last)
        row.update({'measuredFirstFrame': first, 'measuredLastFrame': last, 'measuredFirstSec': round(first / fps, 6),
                    'measuredEndSec': round((last + 1) / fps, 6), 'measuredHoldSec': round(hold, 6),
                    'measuredFrames': len(fr), 'contiguous': len(fr) == last - first + 1, 'onsetDeltaFrames': first - cue_frame,
                    'fullyInFrame': fully, 'fullyInSec': round(fully / fps, 6), 'fullyInAfterFirstSec': round((fully - first) / fps, 6)})
        bad = hold_rules(r['id'], r['treatment'], hold, row['minHoldSec'], 0 if r.get('updates') else r.get('textWords', 0))
        if first != cue_frame:
            bad.append(f"{r['id']}: first on screen at frame {first}, the onset of '{r['words'].split()[0]}' is frame "
                       f"{cue_frame} ({first - cue_frame:+d} frames)")
        bad += caption_rule(r['id'], r, first, captions, row)
        if not row['contiguous']:
            bad.append(f"{r['id']}: leaves the screen and comes back between frames {first} and {last}")
        failures += bad
        rows.append({**row, 'pass': not bad})
        entrances.append((first, r['id'], rows[-1]))
        if r.get('updates'):
            urows, ufail = update_rows(r, first, last, {k: sorted(v) for k, v in (ms.get('regions') or {}).items()}, fps, captions)
            rows += urows
            failures += ufail
            entrances += [(u['measuredFirstFrame'], f"{u['id']} {u['update']}", u) for u in urows[1:] if 'measuredFirstFrame' in u]
    entrances.sort(key=lambda e: e[0])
    for (fa, a, ra), (fb, b, rb) in zip(entrances, entrances[1:]):
        if (fb - fa) / fps < MIN_ENTRANCE_GAP - 1e-9:
            failures.append(f'{a} and {b} enter {(fb - fa) / fps:.2f} s apart; the rule is no two entrances within '
                            f'{MIN_ENTRANCE_GAP} s')
            ra['pass'] = rb['pass'] = False
    return rows, failures


# ------------------------------------------------------------------ rendered checks by class (contract D)
def face_clearance(r, ms, faces, line_at):
    """(info, failures) for one graphic: on each sampled frame (faces: {frame: face box or None}, found on the base
    render) where it is shown and settled (its outline not moving), fewer than MIN_PIXELS of its pixels (ms['face']:
    {frame: px inside that box}) sit inside the face box. Such a frame with no face found fails as unchecked; a line
    with no face on any sampled frame has none to clear. Pixels over the face while it moves are information."""
    shown, moving, px = set(ms.get('shown', [])), set(ms.get('moving', [])), ms.get('face') or {}
    has_face = {line_at.get(i) for i, b in faces.items() if b is not None}
    over, unchecked, moved, checked = [], [], [], 0
    for i in sorted(faces):
        if i not in shown or line_at.get(i) not in has_face:
            continue
        if i in moving:
            if faces[i] is not None and (px.get(i) or 0) >= MIN_PIXELS:
                moved.append(px[i])
            continue
        if faces[i] is None:
            unchecked.append(i)
            continue
        checked += 1
        if (px.get(i) or 0) >= MIN_PIXELS:
            over.append(i)
    info = {'checkedFrames': checked, 'overFaceFrames': over, 'maxPx': max([px[i] for i in over] or [0]),
            'uncheckedFrames': unchecked, 'movingOverFace': {'frames': len(moved), 'maxPx': max(moved or [0])}}
    failures = []
    if over:
        f = over[0]
        failures.append(f"{r['id']}: face clearance: covers the face on {len(over)} of {checked} settled sampled frames "
                        f"(first frame {f}: {px[f]} px of it inside the face box {[round(v) for v in faces[f]]})")
    if unchecked:
        failures.append(f"{r['id']}: face clearance: no face found on frame {unchecked[0]} of line {line_at.get(unchecked[0])} "
                        f"({len(unchecked)} frames) to check it against, while the line shows a face elsewhere")
    return info, failures


def visible_pixels(alone, film, base):
    """(pixels where the graphic changes the picture, of those the ones the film shows): alone = the graphic's RGBA frame
    rendered alone, base = the same frame of the film without graphics or captions, film = the film's frame. Expected =
    the graphic over the base; a pixel shows the graphic when the film is closer to that than to the base."""
    import numpy as np
    a = alone[:, :, 3:4].astype(np.float32) / 255
    g, b, f = (x[:, :, :3].astype(np.float32) for x in (alone, base, film))
    want = g * a + b * (1 - a)
    count = (alone[:, :, 3] > ALPHA) & (np.abs(want - b).max(axis=2) > VIS_DIFF)
    shows = np.abs(f - want).mean(axis=2) < np.abs(f - b).mean(axis=2)
    return int(count.sum()), int((count & shows).sum())


def visible_rows(gid, checks):
    """(rows, failures): each check frame ({frame, what, px, shown}) passes when the graphic changes MIN_PIXELS+ pixels
    there and VIS_SHARE of them show on the film"""
    rows, failures = [], []
    for c in checks:
        share = c['shown'] / c['px'] if c['px'] else 0.0
        row = {**c, 'share': round(share, 4), 'pass': c['px'] >= MIN_PIXELS and share >= VIS_SHARE}
        rows.append(row)
        if c['px'] < MIN_PIXELS:
            failures.append(f"{gid}: visible on the film: at its cue frame {c['frame']} ({c['what']}) it changes {c['px']} px of "
                            f'the picture, too few to check')
        elif not row['pass']:
            failures.append(f"{gid}: visible on the film: at its cue frame {c['frame']} ({c['what']}) only {share:.0%} of its "
                            f"{c['px']} px show on the film (the rule is >= {VIS_SHARE:.0%}): hidden or covered")
    return rows, failures


def cue_frames(rows, gid):
    """[(frame, what)]: the cue frames of a graphic's timing rows (the graphic's, then each counter update's), once each"""
    out = []
    for row in rows:
        if row['id'] != gid or row['cueOnsetFrame'] in [f for f, _ in out]:
            continue
        out.append((row['cueOnsetFrame'], (f"{row['update']} " if row.get('update') else '') + f"'{row['cue']['words']}'"))
    return out


def margin_side(zone, box):
    """the first Reels margin a box crosses (top, bottom, left, right), None when inside"""
    from build_reel import H, right_edge
    x0, y0, x1, y1 = box
    for side, bad in (('top', y0 < zone['top']), ('bottom', y1 > H - zone['bottom']), ('left', x0 < zone['left'] - .5),
                      ('right', x1 > right_edge(zone, y0, y1) + .5)):
        if bad:
            return side
    return None


def caption_safe_zone(frames, caps, zone=None):
    """(info, failures): every box of the captions' rendered ink ({frame: [box]}, from the captions rendered alone) inside
    the existing Reels safe zone (build_reel PLATFORMS), on every frame; one failure naming each caption word with ink outside"""
    from build_reel import PLATFORMS
    zone = zone or PLATFORMS[GATES['captions']['zone']]
    bad = {}
    for i in sorted(frames):
        for b in frames[i]:
            side = margin_side(zone, b)
            if side:
                k = max([j for j, (_, f) in enumerate(caps) if f <= i] or [0])
                bad.setdefault(k, (i, b, side))
    info = {'framesWithInk': sum(1 for v in frames.values() if v), 'zone': zone,
            'outside': [{'word': caps[k][0], 'frame': i, 'box': b, 'margin': s} for k, (i, b, s) in sorted(bad.items())]}
    if not bad:
        return info, []
    k = min(bad)
    i, b, side = bad[k]
    return info, [f"captions: safe zone: {len(bad)} caption words have ink outside the Reels safe zone, first '{caps[k][0]}' "
                  f'on frame {i}: ink {b} past the {side} margin']


def words_on_film(pk, transcript):
    """(info, failures): every script word heard, in order, on the rendered film's audio (its own transcript; the
    captions are not read)"""
    tokens, owner = [], []
    for ln in pk['lines']:
        for j, t in enumerate(ln['text'].split()):
            tokens.append(t)
            owner.append(f"{ln['id']} word {j} '{t}'")
    heard = align(tokens, transcript)
    missing = [owner[k] for k, h in enumerate(heard) if h is None]
    info = {'scriptWords': len(tokens), 'heard': len(tokens) - len(missing), 'notHeard': missing}
    if not missing:
        return info, []
    return info, [f"words: {info['heard']}/{len(tokens)} script words heard on the film's audio (in order); not heard: "
                  + ', '.join(missing)]


# ------------------------------------------------------------------ media
def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def match_files(pk, shots_dir):
    """{sha256: path} for every take and picture the packet names, each matched by its bytes; the same bytes staged
    under two names (two graphics or ids with one picture) are one input"""
    found = {}
    for f in sorted(os.listdir(shots_dir)) if os.path.isdir(shots_dir) else []:
        p = os.path.join(shots_dir, f)
        if os.path.isfile(p):
            found.setdefault(sha256(p), p)
    want = [(ln['id'], ln['take']) for ln in pk['lines']] + [(g['id'], a) for g in pk['graphics'] for a in g['assets']]
    out = {}
    for who, a in want:
        if a['sha256'] not in found:
            raise Refused(f"{who}: no file in {shots_dir} has the sha256 of {a['id']} ({a['sha256']})")
        out[a['sha256']] = os.path.abspath(found[a['sha256']])
    return out


def ext(asset, default='.png'):
    return os.path.splitext(str(asset.get('storagePath', '')))[1].lower() or default


def run(cmd, log=None, **kw):
    p = subprocess.run(cmd, **skillenv.TEXT, **kw)
    if log:
        with open(log, 'w', encoding='utf-8') as fh:
            fh.write(p.stdout + p.stderr)
    if p.returncode:
        print(p.stdout[-3000:] + p.stderr[-3000:], file=sys.stderr)
        raise subprocess.CalledProcessError(p.returncode, cmd[:3])
    return p


_WHISPER = []


def heard_words(wav):
    """medium.en words with their times in a 16 kHz mono wav (the model is loaded once)"""
    if not _WHISPER:
        from faster_whisper import WhisperModel
        _WHISPER.append(WhisperModel('medium.en', device='cpu', compute_type='int8'))
    segs, _ = _WHISPER[0].transcribe(wav, word_timestamps=True, vad_filter=False)
    return [{'text': w.word.strip(), 'start': round(w.start, 3), 'end': round(w.end, 3)} for s in segs for w in s.words]


def film_transcript(film, work):
    """the rendered film's own audio, transcribed (the words check reads this, not the captions)"""
    wav = os.path.join(work, 'film.wav')
    run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', '-i', film, '-vn', '-ac', '1', '-ar', '16000', wav])
    return heard_words(wav)


def transcribe(pk, files, place, work):
    """the render transcript: the takes as cut (their places in the film) in one track, medium.en words in film time"""
    ins, chains = [], []
    for i, ln in enumerate(pk['lines']):
        t = place[ln['id']]
        ins += ['-i', files[ln['take']['sha256']]]
        chains.append(f"[{i}:a]atrim=start={t['mediaStart']}:duration={t['duration']},asetpts=PTS-STARTPTS[a{i}]")
    wav = os.path.join(work, 'cut.wav')
    graph = ';'.join(chains) + ';' + ''.join(f'[a{i}]' for i in range(len(chains))) + f'concat=n={len(chains)}:v=0:a=1[out]'
    run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', *ins, '-filter_complex', graph, '-map', '[out]', '-ac', '1',
         '-ar', '16000', wav])
    return heard_words(wav)


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


# ------------------------------------------------------------------ composition from the kit (contract C)
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
      .stage { position: absolute; left: 0; top: 0; width: 1080px; height: 1920px; z-index: 1; }
      .take { position: absolute; left: 0; top: 0; width: 1080px; height: 1920px; object-fit: cover; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="__DUR__" data-fps="__FPS__"
      data-width="1080" data-height="1920">
'''
TAIL = '''    </div>
    <script>
      (function () {
        var tl = gsap.timeline({ paused: true });
__MAIN__        tl.set({}, {}, __DUR__);
        window.__timelines["main"] = tl;
      })();
    </script>
  </body>
</html>
'''


def fill(template, vals):
    for k, v in vals.items():
        template = template.replace(k, str(v))
    return template


def kit_file(kit, name):
    with open(os.path.join(SK, 'kits', kit['kit'], name), encoding='utf-8') as fh:
        return fh.read()


def num(v):
    return repr(round(v, 6))


def host(gid, src, start_frame, frames, fps, track, kind='graphics', z=10):
    """a host on the frames [start_frame, start_frame + frames), starting EPS before its first frame's time"""
    start = max(0.0, start_frame / fps - EPS)
    return (f'      <div id="{gid}" data-composition-id="{gid}" data-composition-src="{src}" data-start="{start:.6f}" '
            f'data-duration="{frames / fps:.6f}" data-track-index="{track}" data-track-kind="{kind}" data-width="1080" '
            f'data-height="1920" style="position: absolute; inset: 0; z-index: {z}"></div>\n')


def to_fixed1(v):
    """JavaScript's Number.prototype.toFixed(1) (half away from zero on the double's exact value)"""
    from decimal import ROUND_HALF_UP, Decimal
    return str(Decimal(v).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)).replace('-0.0', '0.0')


def js_hash(n, seed, signed=False):
    x = math.sin(n * 127.1 + seed * 311.7) * 43758.5453
    f = x - math.floor(x)
    return f * 2 - 1 if signed else f


def wobble_ellipse(cx, cy, rx, ry, seed, wobble_pct, turns):
    """r3's circle.html wobbleEllipse, computed here so the ring is in the page from the host's first frame (a path a
    sub-composition's script builds shows one frame after the host starts)"""
    n = 14
    pts = []
    i = 0
    while i < n * turns + 3:
        a = (i / n) * math.pi * 2 - 2.6
        wr = 1 + js_hash(i * 13 + 5, seed, True) * (wobble_pct / 100) + i * 0.004
        pts.append((cx + math.cos(a) * rx * wr, cy + math.sin(a) * ry * wr))
        i += 1
    d = 'M' + to_fixed1(pts[0][0]) + ' ' + to_fixed1(pts[0][1])
    for p, q in zip(pts[1:-1], pts[2:]):
        d += ' Q' + to_fixed1(p[0]) + ' ' + to_fixed1(p[1]) + ' ' + to_fixed1((p[0] + q[0]) / 2) + ' ' + to_fixed1((p[1] + q[1]) / 2)
    return d


def scribble(rules, s, start=None):
    """r3's plate.html crammed handwriting, one path per rule from rules.fromRule down (seeded, computed here for the same
    reason as the ring): the rule geometry from the picture's meta in its own pixels, the loops kit constants in display px"""
    left, right, spread, bottom = (round(rules[k] * s, 2) for k in ('left', 'right', 'rightSpread', 'bottom'))
    out = []
    for k in range(rules['fromRule'] if start is None else start, 20):
        base = (rules['firstY'] + rules['pitch'] * k) * s - 2
        if base > bottom:
            break
        x = left + js_hash(k, 2) * 6
        end = right + js_hash(k, 5) * spread
        d, i = 'M' + to_fixed1(x) + ' ' + to_fixed1(base), 0
        while x < end:
            n, j = 3 + math.floor(js_hash(k * 31 + i, 9) * 5), 0
            while j < n and x < end:
                h = 4 + js_hash(k * 97 + i * 7 + j, 4) * 4
                d += ' Q' + to_fixed1(x + 2) + ' ' + to_fixed1(base - h) + ' ' + to_fixed1(x + 4) + ' ' + to_fixed1(base)
                x += 4.5
                j += 1
            x += 5 + js_hash(k + i, 7) * 4
            d += ' M' + to_fixed1(x) + ' ' + to_fixed1(base)
            i += 1
        out.append(d)
    return out


def photo_panel_piece(kit, r, gr, rels, fps):
    pre = kit['presets']['photo_panel']
    x0, y0, x1, y1 = pre['box']
    w, h, pad, gid = x1 - x0, y1 - y0, pre['pad'], r['id']
    assets = gr['assets']
    if len(assets) == 1:            # one picture: fitted by its aspect inside the pad, tilted as slot 1
        a = assets[0]
        k = min((w - 2 * pad) / a['width'], (h - 2 * pad) / a['height'])
        iw, ih = round(a['width'] * k), round(a['height'] * k)
        slots = [{**pre['slots'][0], 'left': (w - iw) // 2, 'top': (h - ih) // 2, 'width': iw, 'height': ih}]
    else:
        slots = [{**sl, 'height': round(sl['width'] * a['height'] / a['width'])} for sl, a in zip(pre['slots'], assets)]
    writing = {wr['assetId']: wr for wr in gr.get('writing') or []}
    layers = ''
    for i, (a, rel, sl) in enumerate(zip(assets, rels, slots)):
        sw, sh, s = sl['width'], sl['height'], sl['width'] / a['width']
        inner = f'        <img src="{rel}" alt="" style="width: {sw}px; height: {sh}px; filter: {sl["shadow"]}" />\n'
        wr = writing.get(a['id'])
        if wr:
            m = a['meta']['writing']
            size, top = round(m['size'] * s, 2), round(m['y'] * s, 2)
            box = (f'left: 0; width: {sw}px; text-align: center' if m.get('align') == 'center'
                   else f"left: {round(m.get('x', 0) * s, 2)}px")
            if wr.get('text'):
                inner += (f'        <div class="{gid}-text" style="{box}; top: {top}px; font-size: {size}px; line-height: {size}px" '
                          f'data-layout-allow-overlap>{html.escape(wr["text"])}</div>\n')
            rules, spec, written = m.get('rules'), m.get('lines') or {}, wr.get('lines') or []
            for i, line in enumerate(written):    # kit r3.3: each line on its rule (baseline), r3's ink, from the rule margin
                k = rules['fromRule'] + i * spec['every']
                ls, lt = round(spec['size'] * s, 2), round((rules['firstY'] + rules['pitch'] * k - spec['size'] * 0.78) * s, 2)
                inner += (f'        <div class="{gid}-text" style="left: {round(rules["left"] * s, 2)}px; top: {lt}px; font-size: {ls}px; '
                          f'line-height: {ls}px" data-layout-allow-overlap>{html.escape(line)}</div>\n')
            if wr.get('scribble') and rules:
                start = rules['fromRule'] + len(written) * spec.get('every', 1) if written else None
                paths = ''.join(f'<path d="{d}" />' for d in scribble(rules, s, start))
                inner += (f'        <svg class="{gid}-ink" viewBox="0 0 {sw} {sh}" style="width: {sw}px; height: {sh}px" '
                          f'data-layout-ignore><g>{paths}</g></svg>\n')
        layers += (f'      <div class="{gid}-slot" style="left: {sl["left"]}px; top: {sl["top"]}px; width: {sw}px; height: {sh}px; '
                   f'transform: rotate({sl["rotate"]}deg)" data-layout-allow-overflow>\n{inner}      </div>\n')
    hold = r['frames'] / fps
    return fill(kit_file(kit, 'photo_panel.html'), {
        '__LAYERS__': layers, '__ID__': gid, '__LEFT__': x0, '__TOP__': y0, '__W__': w, '__H__': h,
        '__ENTER__': pre['enter'], '__EXIT__': pre['exit'], '__EXIT_AT__': f"{hold - pre['exit'] - pre['hiddenBeforeEnd']:.6f}",
        '__HIDE_AT__': f"{hold - pre['hiddenBeforeEnd']:.6f}", '__HOLD__': f'{hold:.6f}'})


def band_exits(r):
    return r['startFrame'] + r['frames'] < r['cutFrame']


def top_band_piece(kit, r, gr, rels, fps):
    pre, a, gid = kit['presets']['top_band'], gr['assets'][0], r['id']
    m = a['meta']
    band = m.get('bandHeightPx', pre['bandHeightPx'])
    fw, fh = m['frame']
    if m.get('bandTransition') == 'dissolve':
        # Keep all the source pixels inside its measured headroom; the take itself never translates.
        scale = min(1080 / fw, band / fh)
        w, h = round(fw * scale, 2), round(fh * scale, 2)
        hold = r['frames'] / fps
        return fill(kit_file(kit, 'top_band_dissolve.html'), {
            '__ID__': gid, '__SRC__': rels[0], '__LEFT__': num((1080 - w) / 2), '__W__': num(w), '__H__': num(h),
            '__HOLD__': num(hold), '__MEDIA_START__': num(m['mediaStart']), '__FRAME__': num(1 / fps),
            '__LAST__': num(hold - 1 / fps), '__LEAVE__': num(hold - 1 / fps - .23)})
    cx0, cy0, cx1, cy1 = m.get('crop') or [0, 0, fw, fh]
    ax, ay = m.get('anchor') or [0.5, 0]
    s = max(1080 / (cx1 - cx0), band / (cy1 - cy0))           # the kept crop covers the band
    left, top = -cx0 * s - ax * ((cx1 - cx0) * s - 1080), -cy0 * s - ay * ((cy1 - cy0) * s - band)
    hold, motion, exit_ = r['frames'] / fps, '', ''
    pan = m.get('pan') or []
    for (k0, k1) in zip(pan, pan[1:]):
        to = f"{{ y: {k1['y']}, duration: {num(k1['at'] - k0['at'])}, ease: \"{k1.get('ease', 'sine.inOut')}\" }}, {num(k0['at'])}"
        motion += (f'        tl.fromTo("#{gid}-vid", {{ y: {k0["y"]} }}, {to});\n' if k0 is pan[0]
                   else f'        tl.to("#{gid}-vid", {to});\n')
    if m.get('zoom'):
        z0, z1 = m['zoom']
        motion += f'        tl.fromTo("#{gid}-box", {{ scale: {z0} }}, {{ scale: {z1}, duration: {hold:.6f}, ease: "sine.out" }}, 0);\n'
    if band_exits(r):
        exit_ = (f'        tl.to("#{gid}-band", {{ y: -{band}, duration: {pre["exit"]}, ease: "power3.inOut" }}, '
                 f'{hold - pre["exitStartsBeforeEnd"]:.6f});\n'
                 f'        tl.set("#{gid}-band", {{ opacity: 0 }}, {hold - pre["hiddenBeforeEnd"]:.6f});\n')
    return fill(kit_file(kit, 'top_band_broll.html'), {
        '__MOTION__': motion, '__EXIT__': exit_, '__ID__': gid, '__SRC__': rels[0], '__BAND_H__': band, '__VID_L__': round(left, 2),
        '__VID_T__': round(top, 2), '__VID_W__': round(fw * s, 2), '__VID_H__': round(fh * s, 2), '__MEDIA_START__': m['mediaStart'],
        '__FROM_Y__': pre['enterFromY'], '__ENTER__': pre['enter'], '__HOLD__': f'{hold:.6f}'})


def circle_piece(kit, r, gr, take, fps):
    x0, y0, x1, y1 = target_box(take, gr['target'], r['id'])
    c = kit['circle']
    d = wobble_ellipse(round((x0 + x1) / 2, 2), round((y0 + y1) / 2, 2), round((x1 - x0) / 2, 2), round((y1 - y0) / 2, 2),
                       c['seed'], c['wobblePct'], c['turns'])
    return fill(kit_file(kit, 'circle.html'), {'__ID__': r['id'], '__D__': d, '__HOLD__': f"{r['frames'] / fps:.6f}"})


def counter_piece(kit, r, fps):
    sc = kit['stepCounter']
    left, top = kit['presets']['overlay']['anchors'][sc['anchor']][:2]
    code, count, ups = '', 1, r['updates']
    for i, u in enumerate(ups[1:], 1):
        t = (u['frame'] - r['startFrame']) / fps
        if u['text'] == 'pulse':       # starts a frame early so the glow already shows on its cue frame; gone by the next update
            nxt = ups[i + 1]['frame'] if i + 1 < len(ups) else r['startFrame'] + r['frames']
            hold = min(sc['pulseSec'], (nxt - u['frame'] + 1) / fps)
            code += f"        pulse({count + 1}, {t - 1 / fps:.6f}, {hold:.6f});\n"
        else:
            count = int(u['text'].split('/')[0])
            code += f'        step({count}, {t:.6f});\n'
    return fill(kit_file(kit, 'step_counter.html'), {'__UPDATES__': code, '__ID__': r['id'], '__LEFT__': left, '__TOP__': top,
                                                   '__END__': f"{r['frames'] / fps:.6f}"})


def presenter_tweens(kit, r, take, fps, band_meta=None):
    """the top band's presenter transform: the take slides down under the band, and back up if the band leaves first"""
    if (band_meta or {}).get('bandTransition') == 'dissolve':
        return ''
    pre = kit['presets']['top_band']
    pr, d = pre['presenter'], (take.get('meta') or {}).get('topBandShiftPx', pre['presenter']['shiftPx'])
    start, w = r['startFrame'] / fps - EPS, f"#w-{r['lineId']}"
    out = (f'        tl.fromTo("{w}", {{ y: 0 }}, {{ y: {d}, duration: {pr["enter"]}, ease: "{pr["ease"]}" }}, {start:.6f});\n')
    if band_exits(r):
        out += (f'        tl.to("{w}", {{ y: 0, duration: {pre["exit"]}, ease: "{pr["exitEase"]}" }}, '
                f'{start + r["frames"] / fps - pre["exitStartsBeforeEnd"]:.6f});\n')
    return out


def build(pk, files, resolved, kit, proj, masks, place, captions):
    """<proj>/index.html (the film) and <masks>/<id>/index.html (each graphic alone, transparent: a project of its own, so
    each project has one root composition) from the kit, the takes at their places and the built captions page"""
    fps = kit['fps']
    dur = round(sum(p['frames'] for p in place.values()) / fps, 6)
    kdir = os.path.join(SK, 'kits', kit['kit'])
    for sub in ('assets/takes', 'assets/gen', 'assets/fonts', 'compositions'):
        os.makedirs(os.path.join(proj, sub), exist_ok=True)
    for font in FONTS:
        shutil.copyfile(os.path.join(kdir, 'fonts', font), os.path.join(proj, 'assets', 'fonts', font))
    with open(os.path.join(proj, 'compositions', 'captions.html'), 'w', encoding='utf-8') as fh:
        fh.write(captions)
    takes, lines = '', {ln['id']: ln for ln in pk['lines']}
    for ln in pk['lines']:
        t, rel = place[ln['id']], f"assets/takes/{ln['id']}.mp4"
        shutil.copyfile(files[ln['take']['sha256']], os.path.join(proj, rel))
        takes += (f'      <div class="stage" id="w-{ln["id"]}">\n'
                  f'        <video id="take-{ln["id"]}" class="clip take" src="{rel}" data-start="{num(t["start"])}" '
                  f'data-duration="{num(t["duration"])}" data-media-start="{num(t["mediaStart"])}" data-track-index="0" '
                  f'data-has-audio="true" data-volume="1" playsinline></video>\n      </div>\n')
    by_id = {g['id']: g for g in list(pk['graphics']) + list(pk.get('persistent') or [])}
    hosts, needs, main = {}, {}, ''
    for r in resolved:
        gr, gid, t = by_id[r['id']], r['id'], r['treatment']
        rels = []
        for a in gr.get('assets') or []:
            rels.append(f"assets/gen/{a['id']}{ext(a)}")
            shutil.copyfile(files[a['sha256']], os.path.join(proj, rels[-1]))
        take = lines[r['lineId']]['take']
        page = {'photo_panel': lambda: photo_panel_piece(kit, r, gr, rels, fps),
                'top_band_broll': lambda: top_band_piece(kit, r, gr, rels, fps),
                'circle': lambda: circle_piece(kit, r, gr, take, fps),
                'step_counter': lambda: counter_piece(kit, r, fps)}[t]()
        with open(os.path.join(proj, 'compositions', f'{gid}.html'), 'w', encoding='utf-8') as fh:
            fh.write(page)
        spec = kit['treatments'][t]
        hosts[gid] = host(gid, f'compositions/{gid}.html', r['startFrame'], r['frames'], fps, spec['track'], z=spec['z'])
        needs[gid] = [f'compositions/{gid}.html', *rels]
        if t == 'top_band_broll':
            main += presenter_tweens(kit, r, take, fps, gr['assets'][0]['meta'])
    head = HEAD.replace('__DUR__', str(dur)).replace('__FPS__', str(fps))
    caption_host = host('captions', 'compositions/captions.html', 0, round(dur * fps), fps, 5, 'captions', 20)
    with open(os.path.join(proj, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(head.replace('__BG__', '#000') + takes + ''.join(hosts.values()) + caption_host
                 + TAIL.replace('__DUR__', str(dur)).replace('__MAIN__', main))
    # the base (contract D): the film without graphics or captions, presenter moves kept, so a face a graphic covers
    # in the film is still found, and the film can be compared with what it shows under each graphic
    base = os.path.join(os.path.dirname(masks), 'base')
    for rel in [f"assets/takes/{ln['id']}.mp4" for ln in pk['lines']] + ['assets/vendor/gsap.min.js']:
        os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
        if os.path.isfile(os.path.join(proj, rel)):
            shutil.copyfile(os.path.join(proj, rel), os.path.join(base, rel))
    with open(os.path.join(base, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(head.replace('__BG__', '#000') + takes + TAIL.replace('__DUR__', str(dur)).replace('__MAIN__', main))
    hosts['captions'], needs['captions'] = caption_host, ['compositions/captions.html']
    for gid, rels in needs.items():
        mp = os.path.join(masks, gid)
        for rel in rels + ['assets/vendor/gsap.min.js'] + [f'assets/fonts/{f}' for f in FONTS]:
            os.makedirs(os.path.dirname(os.path.join(mp, rel)), exist_ok=True)
            if os.path.isfile(os.path.join(proj, rel)):
                shutil.copyfile(os.path.join(proj, rel), os.path.join(mp, rel))
        with open(os.path.join(mp, 'index.html'), 'w', encoding='utf-8') as fh:
            fh.write(head.replace('__BG__', 'transparent') + hosts[gid] + TAIL.replace('__DUR__', str(dur)).replace('__MAIN__', ''))


# ------------------------------------------------------------------ measuring and sheets
def measure(frames_dir, regions=None, faces=None, boxes=False):
    """per RGBA frame (0 = the film's first frame): `shown` (16+ px with alpha over 32) and `moving` (its alpha differs
    from the frame before: the graphic's outline moves); per named region (box, kind): the frames whose pixels in the box
    differ from the frame before ('change') or that show the accent colour ('accent': red - blue and green over minimums);
    `face` {frame: px with alpha over 32 inside that frame's face box, None with no face} for the frames in faces; with
    boxes, `boxes` {frame: [its alpha-over-32 regions of MIN_AREA+ px]} (overlay_mask's regions, text shadows included)"""
    import numpy as np
    from PIL import Image
    from overlay_mask import components
    shown, moving, prev, face, regs = [], [], None, {}, {}
    found = {name: [] for name in regions or {}}
    for i, f in enumerate(sorted(glob.glob(os.path.join(frames_dir, '*.png')))):
        full = np.asarray(Image.open(f).convert('RGBA'))
        alpha = full[:, :, 3].astype(np.int16)
        if i in (faces or {}):
            b = faces[i]
            face[i] = None if b is None else int((alpha[int(b[1]):int(b[3]), int(b[0]):int(b[2])] > ALPHA).sum())
        if boxes:
            regs[i] = components(alpha > ALPHA)
        if int((alpha > ALPHA).sum()) >= MIN_PIXELS:
            shown.append(i)
            if prev is not None and int((np.abs(alpha - prev[0]) > ALPHA).sum()) >= MIN_PIXELS:
                moving.append(i)
        for name, (box, kind, rule) in (regions or {}).items():
            x0, y0, x1, y1 = box
            px = full[y0:y1, x0:x1].astype(np.int16)
            if kind == 'change':
                hit = prev is not None and int((np.abs(px - prev[1][name]).max(axis=2) > ALPHA).sum()) >= MIN_PIXELS
            else:
                hit = int(((px[:, :, 0] - px[:, :, 2] > rule['minRedMinusBlue']) & (px[:, :, 1] > rule['minGreen'])
                           & (px[:, :, 3] > ALPHA)).sum()) >= MIN_PIXELS
            if hit:
                found[name].append(i)
        prev = (alpha, {name: full[b[1]:b[3], b[0]:b[2]].astype(np.int16) for name, (b, _, _) in (regions or {}).items()})
    return {'shown': shown, 'moving': moving, 'regions': found, 'face': face, **({'boxes': regs} if boxes else {})}


def base_faces(base, detect=None):
    """{frame: face box (YuNet, faces.py) grown by FACE_MARGIN, in 1080x1920 pixels, or None} on every FACE_STEP-th frame of
    the base render (the presenter unobstructed). detect: a stand-in for tests"""
    import numpy as np
    import faces as F
    if detect is None:
        import onnxruntime as ort
        session = ort.InferenceSession(F.ensure_model(), providers=['CPUExecutionProvider'])
        detect = lambda img: F.detect(session, img)            # noqa: E731
    raw = subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-i', base, '-vf', f"select='not(mod(n\\,{FACE_STEP}))',scale=360:640",
                          '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], capture_output=True, check=True).stdout
    out, mg = {}, F.FACE_MARGIN
    for k, img in enumerate(np.frombuffer(raw, np.uint8).reshape(-1, 640, 360, 3)):
        pad = np.zeros((F.SIDE, F.SIDE, 3), np.uint8)
        pad[:, :360] = img
        hit = detect(pad)
        out[k * FACE_STEP] = None if hit is None else [max(0, hit[1][0] * 3 - mg), max(0, hit[1][1] * 3 - mg),
                                                       min(1080, hit[1][2] * 3 + mg), min(1920, hit[1][3] * 3 + mg)]
    return out


def line_frames(place):
    """{film frame: the line whose take is on screen}"""
    return {f: lid for lid, t in place.items() for f in range(t['startFrame'], t['startFrame'] + t['frames'])}


def visible_checks(rows, resolved, film, base, work):
    """{graphic id: [{frame, what, px, shown}]} for the graphics whose class has the visible-on-the-film check: at each
    cue frame, the graphic rendered alone against the film's frame and the base's"""
    import numpy as np
    from PIL import Image
    want = {r['id']: cue_frames(rows, r['id']) for r in resolved if 'visible on the film' in GATES[r['treatment']]['checks']}
    idx = sorted({f for v in want.values() for f, _ in v})
    if not idx:
        return {}
    fpng = frames_at(film, idx, os.path.join(work, 'vis-film'), '1080:1920')
    bpng = frames_at(base, idx, os.path.join(work, 'vis-base'), '1080:1920')
    out = {}
    for gid, marks in want.items():
        alone = sorted(glob.glob(os.path.join(work, f'mask-{gid}', '*.png')))
        out[gid] = []
        for f, what in marks:
            px, shown = visible_pixels(*(np.asarray(Image.open(p).convert(m)) for p, m in
                                         ((alone[f], 'RGBA'), (fpng[f], 'RGB'), (bpng[f], 'RGB'))))
            out[gid].append({'frame': f, 'what': what, 'px': px, 'shown': shown})
    return out


def counter_regions(kit):
    """the step counter's measured boxes in the frame: its number, and each slot's glow"""
    sc = kit['stepCounter']
    ox, oy = kit['presets']['overlay']['anchors'][sc['anchor']][:2]
    m = sc['measure']
    move = lambda b: [b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy]
    out = {'number': (move(m['number']), 'change', None)}
    for k, b in enumerate(m['slots'], 1):
        out[f'slot{k}'] = (move(b), 'accent', m['accent'])
    return out


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


def word_sheet(pk, transcript, on, film, fps, work, ev):
    """the first film frame at or after each heard word's onset, with what is on screen; returns (script tokens, alignment)"""
    tokens, owner = [], []
    for ln in pk['lines']:
        for j, t in enumerate(ln['text'].split()):
            tokens.append(t)
            owner.append((ln['id'], j))
    heard = align(tokens, transcript)
    words = []
    for i, w in enumerate(transcript):
        n = first_frame(w['start'], fps)
        k = heard.index(i) if i in heard else None
        words.append({'i': i, 'heard': w['text'], 'start': w['start'], 'end': w['end'], 'frame': n,
                      'script': None if k is None else {'lineId': owner[k][0], 'word': owner[k][1], 'text': tokens[k]},
                      'onScreen': [g for g in on if n in on[g]]})
    pngs = frames_at(film, [w['frame'] for w in words], os.path.join(work, 'words'))
    sheet([(pngs[w['frame']], [f"{w['heard']}", f"{w['start']:.2f}s f{w['frame']}", ' '.join(w['onScreen']) or '-'])
           for w in words], os.path.join(ev, 'frame-per-word.jpg'))
    with open(os.path.join(ev, 'frame-per-word.json'), 'w', encoding='utf-8') as fh:
        json.dump(words, fh, indent=1)
    return tokens, heard


def cue_sheet(rows, film, nframes, fps, work, ev):
    """per graphic (and counter update): the frame before it, its first frame, the cue onset frame, its last frame, after
    (the film's own first and last frames bound before and after)"""
    cells = []
    for row in rows:
        if 'measuredFirstFrame' not in row:
            continue
        name = f"{row['id']} {row['update']}" if row.get('update') else row['id']
        marks = [('before', max(0, row['measuredFirstFrame'] - 1)), ('first', row['measuredFirstFrame']),
                 ('cue onset', row['cueOnsetFrame']), ('last', row['measuredLastFrame']),
                 ('after', min(nframes - 1, row['measuredLastFrame'] + 1))]
        got = frames_at(film, [n for _, n in marks], os.path.join(work, 'cue-' + name.replace(' ', '-').replace('/', 'of')))
        cells += [(got[n], [f'{name} {what}', f'f{n} {n / fps:.3f}s', f"'{row['cue']['words']}'"]) for what, n in marks]
    if cells:
        sheet(cells, os.path.join(ev, 'cue-frames.jpg'), cols=5)


def dump(path, data):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)


# ------------------------------------------------------------------ the run
def load(packet_path, shots_dir):
    """(packet, kit, files) or Refused: every check that needs no media runs first"""
    try:
        with open(packet_path, encoding='utf-8') as fh:
            pk = json.load(fh)
        kit = load_kit()
        check_pin(pk)
        check_packet(pk, kit)
        place, nframes = layout(pk, kit['fps'])
        caption_groups(pk, place, kit['fps'])
        files = match_files(pk, shots_dir)
        import take_cuts
        check_cuts(pk, {ln['take']['sha256']: take_cuts.frame_count(files[ln['take']['sha256']])[0] for ln in pk['lines']})
        return pk, kit, files
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, subprocess.CalledProcessError) as e:
        raise Refused(f'cannot read the packet ({type(e).__name__}: {e})')


def render_alone(gid, masks, work, hf, env, fps):
    """one piece rendered alone as RGBA frames -> their folder"""
    print(f"== render: {gid} alone (RGBA frames)", flush=True)
    frames = os.path.join(work, f"mask-{gid}")
    run(hf + ['render', '--format', 'png-sequence', '--fps', str(fps), '-o', frames], log=os.path.join(work, f"mask-{gid}.log"),
        cwd=os.path.join(masks, gid), env=env)
    return frames


def render_masks(resolved, masks, work, hf, env, kit, faces=None):
    """each graphic rendered alone (RGBA frames) and measured, with its pixels inside each sampled face box"""
    measured, fps = {}, kit['fps']
    for r in resolved:
        frames = render_alone(r['id'], masks, work, hf, env, fps)
        measured[r['id']] = measure(frames, counter_regions(kit) if r['treatment'] == 'step_counter' else None, faces)
    return measured


def main(packet_path, shots_dir, out):
    t0 = time.time()
    try:
        pk, kit, files = load(packet_path, shots_dir)
    except Refused as e:
        print(f'packet refused: {e}', file=sys.stderr)
        return 2
    fps = kit['fps']
    proj, work, ev, masks = (os.path.join(out, d) for d in ('project', 'work', 'evidence', 'masks'))
    if os.path.exists(proj):
        print(f'packet refused: {out} already holds a planned render; give a new --out', file=sys.stderr)
        return 2
    for d in (proj, work, ev, os.path.join(out, 'renders')):
        os.makedirs(d, exist_ok=True)
    times = {}
    place, nframes = layout(pk, fps)
    try:
        page = captions_page(caption_groups(pk, place, fps), nframes / fps)
    except Refused as e:
        print(f'packet refused: {e}', file=sys.stderr)
        return 2
    print('== transcribe: medium.en on the takes as cut', flush=True)
    transcript = transcribe(pk, files, place, work)
    times['transcribeSec'] = round(time.time() - t0, 1)
    try:
        caps = caption_frames(page, fps)
        resolved = resolve(pk, transcript, place, fps, caps)
    except Refused as e:
        print(f'packet refused: {e}', file=sys.stderr)
        return 2
    hf, env, pkg = hyperframes()
    vendor_gsap(pkg, os.path.join(proj, 'assets', 'vendor', 'gsap.min.js'))
    build(pk, files, resolved, kit, proj, masks, place, page)
    print('== lint', flush=True)
    run(hf + ['lint'], log=os.path.join(work, 'lint.log'), cwd=proj, env=env)
    slug = 'ep-' + (''.join(c for c in str(pk.get('episodeId', '')).lower() if c.isalnum())[:8] or 'unknown')
    film = os.path.join(out, 'renders', f'{slug}-planned.mp4')
    print('== render: film', flush=True)
    t1 = time.time()
    run(hf + ['render', '--quality', 'high', '--fps', str(fps), '-o', film], log=os.path.join(work, 'render.log'), cwd=proj, env=env)
    times['filmRenderSec'] = round(time.time() - t1, 1)
    print('== render: base (the film without graphics or captions)', flush=True)
    t1 = time.time()
    base = os.path.join(work, 'base.mp4')
    run(hf + ['render', '--quality', 'high', '--fps', str(fps), '-o', base], log=os.path.join(work, 'base-render.log'),
        cwd=os.path.join(out, 'base'), env=env)
    times['baseRenderSec'] = round(time.time() - t1, 1)
    t1 = time.time()
    faces = base_faces(base)
    times['facesSec'] = round(time.time() - t1, 1)
    t1 = time.time()
    measured = render_masks(resolved, masks, work, hf, env, kit, faces)
    times['maskRenderAndMeasureSec'] = round(time.time() - t1, 1)
    t1 = time.time()
    cap_boxes = measure(render_alone('captions', masks, work, hf, env, fps), boxes=True)['boxes']
    times['captionsRenderAndMeasureSec'] = round(time.time() - t1, 1)
    rows, failures = timing_table(resolved, measured, fps, caps)
    info = json.loads(run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-count_frames', '-show_entries',
                           'stream=r_frame_rate,nb_read_frames,width,height:format=duration', '-of', 'json', film]).stdout)
    st = info['streams'][0]
    if st['r_frame_rate'] != f'{fps}/1' or (st['width'], st['height']) != (1080, 1920):
        failures.append(f"film is {st['width']}x{st['height']} at {st['r_frame_rate']} fps, not 1080x1920 at {fps}")
    print('== transcribe: medium.en on the film\'s own audio', flush=True)
    t1 = time.time()
    heard_film = film_transcript(film, work)
    times['filmTranscribeSec'] = round(time.time() - t1, 1)
    t1 = time.time()
    gates, gate_failures, at = {'faceClearance': {}, 'visibleOnFilm': {}}, [], line_frames(place)
    for r in resolved:
        if 'face clearance' in GATES[r['treatment']]['checks']:
            gates['faceClearance'][r['id']], bad = face_clearance(r, measured[r['id']], faces, at)
            gate_failures += bad
    for gid, checks in visible_checks(rows, resolved, film, base, work).items():
        gates['visibleOnFilm'][gid], bad = visible_rows(gid, checks)
        gate_failures += bad
    gates['captions'], bad = caption_safe_zone(cap_boxes, caps)
    gate_failures += bad
    gates['words'], bad = words_on_film(pk, heard_film)
    gate_failures += bad
    times['gatesSec'] = round(time.time() - t1, 1)
    failures += gate_failures
    dump(os.path.join(ev, 'gates.json'), {
        'policies': GATES, 'note': GATES_NOTE, 'faceStep': FACE_STEP, 'rules': {
            'face clearance': f'on every {FACE_STEP}rd frame where a graphic is shown and settled, fewer than {MIN_PIXELS} of its '
                              'pixels inside the face box (YuNet on the base render, grown by faces.FACE_MARGIN); a frame with no '
                              'face found on a line that has one fails as unchecked; pixels over the face while it moves are information',
            'visible on the film': f'at each cue frame, of the pixels where the graphic rendered alone changes the base render by '
                                   f'more than {VIS_DIFF}, at least {VIS_SHARE:.0%} on the film are closer to the graphic over the '
                                   'base than to the base',
            'safe zone': 'every region of the captions rendered alone (alpha > 32, text shadows included) inside the Reels zone, '
                         'every frame',
            'words': "every script word heard, in order, on the rendered film's audio (medium.en); captions not read"},
        **gates, 'failures': gate_failures})
    tokens, heard = word_sheet(pk, heard_film, {r['id']: set(measured[r['id']]['shown']) for r in resolved}, film, fps, work, ev)
    cue_sheet(rows, film, int(st['nb_read_frames']), fps, work, ev)
    dump(os.path.join(ev, 'resolved-timeline.json'), {
        'pin': PIN, 'packetEngine': pk['versions']['engine'], 'kit': kit['kit'], 'fps': fps, 'duration': nframes / fps,
        'takes': [{'lineId': ln['id'], 'takeId': ln['take']['id'], 'sha256': ln['take']['sha256'], 'cut': ln['take']['meta']['cut'],
                   **{k: place[ln['id']][k] for k in ('start', 'mediaStart', 'duration', 'startFrame', 'frames')}} for ln in pk['lines']],
        'transcript': {'model': 'faster-whisper medium.en int8', 'of': 'the takes as cut (cues resolve on it)',
                       'words': transcript, 'scriptWords': len(tokens),
                       'scriptWordsHeard': sum(h is not None for h in align(tokens, transcript))},
        'filmTranscript': {'model': 'faster-whisper medium.en int8', 'of': "the rendered film's audio (the words check)",
                           'words': heard_film, 'scriptWords': len(tokens), 'scriptWordsHeard': sum(h is not None for h in heard)},
        'graphics': [{**r, 'startSec': r['startFrame'] / fps, 'durationSec': r['frames'] / fps} for r in resolved],
        'captions': {'piece': 'captions.html', 'built': 'from the take words and caption groups',
                     'groups': [ln.get('captions') or default_groups(ln['text']) for ln in pk['lines']],
                     'sha256': sha256(os.path.join(proj, 'compositions', 'captions.html'))},
        'gsapSha256': GSAP_SHA, 'film': {'path': os.path.relpath(film, out), 'sha256': sha256(film), 'probe': info}})
    dump(os.path.join(ev, 'timing-table.json'), {
        'fps': fps, 'rules': {'minHoldSec': MIN_HOLD, 'text': f'>= {TEXT_BASE} s + {TEXT_PER_WORD} s a word',
                              'minEntranceGapSec': MIN_ENTRANCE_GAP, 'hold': 'first visible frame to last visible frame',
                              'entry': 'first frame at or after the cue word onset, not before its caption; '
                                       'the caption on the first frame is the cue word',
                              'fullyIn': 'information only: the first frame from which its outline stops moving',
                              'measured': f'RGBA frames of each graphic rendered alone; on screen = {MIN_PIXELS}+ px alpha > {ALPHA}'},
        'rows': rows, 'failures': failures})
    times['totalSec'] = round(time.time() - t0, 1)
    dump(os.path.join(out, 'qa-report.json'), {'mode': 'planned', 'failures': failures, 'duration': float(info['format']['duration']),
                                               'times': times, 'evidence': sorted(os.listdir(ev))})
    print('QA ' + ('PASS' if not failures else 'FAIL: ' + '; '.join(failures)), flush=True)
    return 0 if not failures else 1


if __name__ == '__main__':
    skillenv.utf8_stdio()
    a = sys.argv
    if len(a) < 2 or '--shots' not in a or '--out' not in a:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(a[1], a[a.index('--shots') + 1], os.path.abspath(a[a.index('--out') + 1])))
