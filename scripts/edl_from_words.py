#!/usr/bin/env python3
"""Spec mode: plan <project>/edl.json from the reel spec, one segment per narration sentence.

Usage:  PY edl_from_words.py <project_dir>
Needs reel.json (shots[].narration, optional sentences[].hold/id), sources.json, words_cN.json and silences_cN.json
from ingest.py. The narration is the truth for the words and the sentence breaks; Whisper's words only give times.

Segment in  = first word start - 0.06
Segment out = first silence start at/after the last word + 0.08 + the sentence's hold,
              capped at the next spoken word - 0.08 and at the shot's video length.
With the shots' audio (work/cN.wav, as main() loads it) the cuts go by the sound, not Whisper's word times (Whisper
hangs a pause on the next word and ends words early): between two sentences of a shot the cut sits in the longest
pause that sound follows before the next sentence's last word (out = pause start + 0.12 + hold, in = pause end - 0.10);
a pause too short for both becomes one seamless join at its middle (out == next in, no jump, no fades). A shot's first sentence starts 0.10 before the sound that
ends the pause holding Whisper's first-word start; its last sentence ends 0.12 after the sound does.
One take (spec "one_take": true, render_packet.py): a shot's sentences join seamlessly in the middle of the pause
between them (out == next in), so the whole take plays and only its lead-in and tail are trimmed.
Opening rule: a shot whose spec gives clean_from (seconds: its first clean frame, e.g. after a blurred foreground object)
starts there when that keeps MIN_LEAD before the first word's sound; otherwise the in point stays, and the segment
carries an "opening" note saying why (e.g. a shot unclean to 0.75s whose first word starts at 0.02s).
"""
import array
import difflib
import json
import math
import os
import subprocess
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402

LEAD, TAIL, GAP = .06, .08, .08
SNAP_LEAD, SNAP_TAIL = .10, .12   # cuts placed by the sound keep this much room from it (weak /th/, /h/ starts and tails)
MIN_LEAD = .04                    # an in point moved to a clean frame keeps this much before the first word's sound
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


DIGITAL_SILENCE = -80   # dBFS: frames below are no signal at all (padding, generated silence), not the room
ROOM_GAP = 20           # dB under the loud frames that non-silent frames must sit to count as room tone


def room_floor(db):
    """5th-percentile floor of dBFS frames: digital silence is left out when the shot also has room tone (else room tone
    would count as sound); a shot whose only quiet is digital silence keeps it as the floor"""
    ranked, room = sorted(db), sorted(v for v in db if v > DIGITAL_SILENCE)
    if room and room[len(room) // 20] <= ranked[-1 - len(ranked) // 20] - ROOM_GAP:
        return room[len(room) // 20]
    return ranked[len(ranked) // 20]


def levels(pcm, sr):
    """20ms RMS every 10ms in dBFS, and the speech line: the shot's room floor + 10dB"""
    x, n = array.array('h', pcm), sr // 50
    db = [20 * math.log10(math.sqrt(sum(v * v for v in x[k:k + n]) / n) / 32768 + 1e-9) for k in range(0, max(1, len(x) - n), sr // 100)]
    return db, room_floor(db) + 10


BLIP, BLIP_DB = .05, 6   # a run over the speech line shorter than this and under line + BLIP_DB is noise, not sound
SOFT = .16               # a soft edge or decay lasts at most this long (measured on two episodes: up to 0.15s;
                         # longer walks, 0.18-0.30s, ran through noise between sentences)


def quiet_frames(db, line):
    """True for a frame below the speech line or inside a blip: a run over the line shorter than BLIP and under
    line + BLIP_DB with quiet frames on both sides (room noise or a click between sentences; a measured shot had
    10-30ms peaks up to 4.2dB over the line, its quietest real syllable ran 60ms)"""
    q, i = [v < line for v in db], 0
    while i < len(q):
        j = i
        while j < len(q) and not q[j]:
            j += 1
        if 0 < i < j < len(q) and j - i < round(BLIP * 100) and max(db[i:j]) < line + BLIP_DB:
            q[i:j] = [True] * (j - i)
        i = j + 1
    return q


def pauses(db, line, a, b, least=.03):
    """[(start, end)] runs of quiet frames (below the speech line, blips included) of at least `least` seconds in a..b"""
    out, run, q = [], None, quiet_frames(db, line)
    for i in range(max(0, int(a * 100)), min(len(db), int(b * 100))):
        if q[i]:
            run = i if run is None else run
        else:
            if run is not None and i - run >= least * 100:
                out.append((run / 100, i / 100))
            run = None
    if run is not None and min(len(db), int(b * 100)) - run >= least * 100:
        out.append((run / 100, min(len(db), int(b * 100)) / 100))
    return out


def soft_edges(db, line, p0, p1):
    """a pause's real edges: a soft /s/, /h/ or /th/ is quieter than the speech line but still above the room
    (floor + 4dB), so the sound ends later and starts earlier than the line says. A walk longer than SOFT is noise
    between sentences, not a consonant: that edge stays at the line"""
    quiet = line - 6
    s0, e0 = round(p0 * 100), round(p1 * 100)
    s, e = s0, e0
    while s < e0 and db[s] > quiet:
        s += 1
    while e > s0 and db[e - 1] > quiet:
        e -= 1
    s, e = (s if s - s0 <= round(SOFT * 100) else s0), (e if e0 - e <= round(SOFT * 100) else e0)
    return (s / 100, e / 100) if e - s >= 3 else (p0, p1)


def sound_bounds(db, line, start, end, cap):
    """(in, out) of a sentence alone in its shot side: in = 0.10 before the sound that ends the pause holding Whisper's
    first-word start (Whisper often dates it inside the pause), else the pause just before it; out = 0.12 after the sound
    ends (the first >=0.08s pause from `end` - 0.15). A pause inside the sentence never moves its in point."""
    near = pauses(db, line, max(0, start - .3), start + 1.0, .1)
    holding = [p for p in near if p[0] <= start < p[1]] or [p for p in near if start - .3 <= p[1] <= start]
    lead_in = soft_edges(db, line, *holding[-1])[1] if holding else start
    after = pauses(db, line, max(lead_in, end - .15), cap, .08)
    tail = soft_edges(db, line, *after[0])[0] if after else cap
    return max(0.0, lead_in - SNAP_LEAD), min(cap, tail + SNAP_TAIL), lead_in


def plan(spec, words, silences, durations, audio=None):
    """spec: reel spec dict. words/silences/durations: keyed by clip id c1..cN (shot order). audio: {clip: (pcm, rate)}.
    Returns the EDL list; each entry also carries the sentence's narration tokens and their word indexes."""
    over = spec.get('sentences') or []
    edl = []
    for k, shot in enumerate(spec['shots']):
        cid = f'c{k + 1}'
        ws, sents = words[cid], sentences(shot['narration'])
        idx = align([t for s in sents for t in s], ws)
        cap = math.floor(durations[cid] * 100) / 100
        first, a = len(edl), 0
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
                        'zoom': 1.0, 'cx': .5, 'cy': .5, 'line': ' '.join(toks), 'tokens': toks, 'word_idx': ix,
                        '_start': start, '_end': end, '_hold': o.get('hold', 0), '_onset': start})
        if audio and cid in audio:
            db, line = levels(*audio[cid])
            cap = max(cap, math.floor(len(audio[cid][0]) / 2 / audio[cid][1] * 1000) / 1000)   # to the end of the shot's sound
            shot_edl = edl[first:]
            for e in shot_edl:
                e['in'], e['out'], e['_onset'] = sound_bounds(db, line, e['_start'], e['_end'], cap)
                e['out'] = min(cap, e['out'] + e['_hold'])
            for e, nxt in zip(shot_edl, shot_edl[1:]):
                upto = max(nxt['_end'], e['_end'] + .2)            # the next sentence is still sounding before its last word ends
                gaps = [g for g in pauses(db, line, e['_end'] - .15, upto) if g[1] < upto - .005] or [(e['_end'], e['_end'])]
                p0, p1 = soft_edges(db, line, *max(gaps, key=lambda g: g[1] - g[0]))
                if p1 - p0 >= SNAP_LEAD + SNAP_TAIL:
                    e['out'], nxt['in'] = min(p0 + SNAP_TAIL + e['_hold'], p1 - SNAP_LEAD), p1 - SNAP_LEAD
                else:
                    e['out'] = nxt['in'] = (p0 + p1) / 2
            for e in shot_edl:
                e['in'], e['out'] = round(e['in'], 3), round(e['out'], 3)
        if spec.get('one_take'):     # a generated shot is one take: its sentences join in their pause, nothing is cut out
            for e, nxt in zip(edl[first:], edl[first + 1:]):
                e['out'] = nxt['in'] = round((e['out'] + nxt['in']) / 2, 3)
        clean, e = shot.get('clean_from'), edl[first]
        if clean is not None and clean > e['in']:
            if clean <= e['_onset'] - MIN_LEAD:
                e['in'] = round(clean, 3)
            else:
                e['opening'] = f"unclean until {clean:.2f}s kept: the first word starts at {e['_onset']:.2f}s"
    for e in edl:
        for key in ('_start', '_end', '_hold', '_onset'):
            e.pop(key)
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
    audio = {}
    for c in src:
        wav = os.path.join(proj, 'work', f'{c}.wav')
        if os.path.exists(wav):
            with wave.open(wav) as fh:
                audio[c] = (fh.readframes(fh.getnframes()), fh.getframerate())
    edl = plan(spec, words, silences, {c: video_duration(f) for c, f in src.items()}, audio=audio)
    with open(os.path.join(proj, 'edl.json'), 'w', encoding='utf-8') as fh:
        json.dump(edl, fh, indent=1)
    for e in edl:
        print(f"{e['id']:8s} {e['clip']} {e['in']:6.2f}-{e['out']:6.2f} ({e['out'] - e['in']:4.2f}s)  {e['line']}"
              + (f"   OPENING {e['opening']}" if 'opening' in e else ''))
    print(f"total {sum(e['out'] - e['in'] for e in edl):.2f}s  {len(edl)} sentences")


if __name__ == '__main__':
    main()
