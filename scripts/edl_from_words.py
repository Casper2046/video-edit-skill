#!/usr/bin/env python3
"""Spec mode: plan <project>/edl.json from the reel spec, one segment per narration sentence.

Usage:  PY edl_from_words.py <project_dir>
Needs reel.json (shots[].narration, optional sentences[].hold/id), sources.json, words_cN.json and silences_cN.json
from ingest.py. The narration is the truth for the words and the sentence breaks; Whisper's words only give times.

Segment in  = first word start - 0.06
Segment out = first silence start at/after the last word + 0.08 + the sentence's hold,
              capped at the next spoken word - 0.08 and at the shot's video length.
"""
import difflib
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402

LEAD, TAIL, GAP = .06, .08, .08
CLOSERS = '\'"’”)'


def norm(t):
    return ''.join(c for c in t.lower() if c.isalnum())


def sentences(narration):
    """[[token, ...], ...]: split after a token ending in . ? or ! (closing quotes allowed after it)"""
    out, cur = [], []
    for tok in narration.split():
        cur.append(tok)
        if tok.rstrip(CLOSERS)[-1:] in '.?!':
            out.append(cur); cur = []
    return out + [cur] if cur else out


def align(tokens, words):
    """index into words for every token, or None. Same-length replacements count (colour/color, 'Just'/Just)."""
    a, b = [norm(t) for t in tokens], [norm(w['text']) for w in words]
    out = [None] * len(tokens)
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == 'equal' or (tag == 'replace' and a1 - a0 == b1 - b0):
            for k in range(a1 - a0):
                out[a0 + k] = b0 + k
    return out


def onset_fix(words, silences):
    """Whisper dates a shot's first word at 0.00 even when the audio is silent there: move a word start that falls
    inside the leading silence to that silence's end (the real speech onset)."""
    out = []
    for w in words:
        st = w['start']
        for s0, e0 in silences:
            if st <= .05 and s0 - .02 <= st < e0 - .05 and e0 < w['end']:
                st = round(e0, 3)
        out.append({**w, 'start': st})
    return out


def plan(spec, words, silences, durations):
    """spec: reel spec dict. words/silences/durations: keyed by clip id c1..cN (shot order).
    Returns the EDL list; each entry also carries the sentence's narration tokens and their word indexes."""
    over = spec.get('sentences') or []
    edl = []
    for k, shot in enumerate(spec['shots']):
        cid = f'c{k + 1}'
        ws, sents = words[cid], sentences(shot['narration'])
        idx = align([t for s in sents for t in s], ws)
        cap = math.floor(durations[cid] * 100) / 100
        a = 0
        for toks in sents:
            n = len(edl)
            ix = idx[a:a + len(toks)]
            a += len(toks)
            if ix[0] is None or ix[-1] is None:
                raise ValueError(f'shot {k + 1} sentence "{" ".join(toks)}": first/last word not found in what was said: '
                                 + ' '.join(w['text'] for w in ws))
            o = over[n] if n < len(over) else {}
            start, end = ws[ix[0]]['start'], ws[ix[-1]]['end']
            after = [s0 for s0, _ in silences[cid] if s0 >= end - .15]
            out = (min(after) if after else end) + TAIL + o.get('hold', 0)
            if ix[-1] + 1 < len(ws):
                out = min(out, ws[ix[-1] + 1]['start'] - GAP)
            out = min(out, cap)
            edl.append({'id': o.get('id', f's{n + 1}'), 'clip': cid, 'in': round(max(0, start - LEAD), 3), 'out': round(out, 3),
                        'zoom': 1.0, 'cx': .5, 'cy': .5, 'line': ' '.join(toks), 'tokens': toks, 'word_idx': ix})
    if over and len(over) != len(edl):
        raise ValueError(f'reel spec lists {len(over)} sentences, the narration has {len(edl)}: '
                         + ' | '.join(e['line'] for e in edl))
    ids = [e['id'] for e in edl]
    if len(set(ids)) != len(ids):
        raise ValueError(f'sentence ids repeat: {ids}')
    return edl


def video_duration(f):
    out = subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration',
                          '-of', 'csv=p=0', f], **skillenv.TEXT).stdout
    return float(out.strip().split(',')[0])


def load(proj, name):
    with open(os.path.join(proj, name), encoding='utf-8') as fh:
        return json.load(fh)


def main():
    skillenv.utf8_stdio()
    proj = skillenv.path_arg(sys.argv[1])
    spec, src = load(proj, 'reel.json'), load(proj, 'sources.json')
    words = {c: load(proj, f'words_{c}.json') for c in src}
    silences = {c: load(proj, f'silences_{c}.json') for c in src}
    edl = plan(spec, words, silences, {c: video_duration(f) for c, f in src.items()})
    with open(os.path.join(proj, 'edl.json'), 'w', encoding='utf-8') as fh:
        json.dump(edl, fh, indent=1)
    for e in edl:
        print(f"{e['id']:8s} {e['clip']} {e['in']:6.2f}-{e['out']:6.2f} ({e['out'] - e['in']:4.2f}s)  {e['line']}")
    print(f"total {sum(e['out'] - e['in'] for e in edl):.2f}s  {len(edl)} sentences")


if __name__ == '__main__':
    main()
