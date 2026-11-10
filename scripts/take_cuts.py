#!/usr/bin/env python3
"""Planned mode (ticket 11): a take's cut, measured once from its own audio and words, so the plan checks and the render use
the same cut (the packet carries it as take meta.cut; render_planned.py lays the takes end to end from it).

Usage:  PY take_cuts.py [--words WORDS.json] [--lines LINES.json] [--out FILE] <take.mp4> [<take.mp4> ...]
WORDS.json maps a take's path to its stored words ([{word, start, end}], take seconds); a take without them is transcribed
here (medium.en). LINES.json maps a take's path to its line's script text: the cut is bounded by the script's words, so an
ad-lib before them ("Mm," on face23be l1) is left out. --out FILE writes the list to FILE as well (a new file; the render
worker's measure job reads it there). Prints a JSON list, one entry per take: sha256, frames, fps, words, speech [start, end], cleanFrom,
fromFrame, toFrame, rule, or {path, refused} for a take with no speech. Exit 0, or 2 when any take was refused.

The rule (r3's hand cut, remake-film3-20261108/verify/plan2.py, made automatic):
- speech = from the first to the last loud moment: 20 ms RMS windows every 10 ms, loud above -38 dBFS; a loud window
  starting in the file's last 0.05 s is an end-of-file blip and ignored; no loud window at all refuses the take;
- in  = max(first loud moment, first script word's start) - 0.10 s, floored to a frame, not before frame 0;
- out = max(last loud moment, last script word's end) + 0.12 s, ceiled to a frame, and not past the take's last frame.
An unclean opening (openings.py clean_from) is recorded, not cut: it can only move the in point past the 0.10 s lead into
the first word (spec mode's opening rule keeps the words then too; face23be l6: unclean to 0.79 s, "Pick" at 0.01 s).
A sound after the last script word still holds the cut open (only the start is bounded by the script).
On film 3's seven takes with their stored small.en words this gives r3's cuts frame for frame (tests/test_general_layout.py);
-40 dB and an end window of 0.03-0.08 s give the same frames.
"""
import hashlib
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402

RULE = 'take_cuts v2: speech -38 dBFS (20 ms RMS / 10 ms), in = max(speech, first script word) - 0.10 s, out = max(speech, last script word) + 0.12 s'
RATE, WIN, HOP, LOUD_DB, END_BLIP = 16000, 0.02, 0.01, -38.0, 0.05
LEAD, TAIL = 0.10, 0.12


class NoSpeech(Exception):
    pass


def speech_span(path):
    """(first loud moment, last loud moment) in seconds of a file's audio"""
    import numpy as np
    raw = subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-i', path, '-vn', '-ac', '1', '-ar', str(RATE), '-f', 'f32le', '-'],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.float32).astype(np.float64)
    n, h = int(RATE * WIN), int(RATE * HOP)
    if len(a) < n:
        raise NoSpeech(f'{os.path.basename(path)}: no speech (audio shorter than {WIN} s)')
    c = np.concatenate([[0.0], np.cumsum(a * a)])
    starts = np.arange(0, len(a) - n + 1, h)
    db = 20 * np.log10(np.sqrt((c[starts + n] - c[starts]) / n) + 1e-9)
    t = starts / RATE
    loud = np.nonzero((db > LOUD_DB) & (t < len(a) / RATE - END_BLIP))[0]
    if not len(loud):
        raise NoSpeech(f'{os.path.basename(path)}: no speech (nothing above {LOUD_DB:.0f} dBFS)')
    return round(float(t[loud[0]]), 3), round(float(t[loud[-1]] + WIN), 3)


def script_words(words, text):
    """the take's words from its first to its last word of the script text (all of them without a text, or when none match)"""
    if not text:
        return words
    from edl_from_words import align
    heard = [i for i in align(text.split(), [{'text': w['word'], 'start': w['start'], 'end': w['end']} for w in words]) if i is not None]
    return words[min(heard):max(heard) + 1] if heard else words


def cut_frames(speech, words, nframes, fps):
    """(first frame, frame after the last) of a take's cut; words: its script words"""
    on = max([speech[0]] + [w['start'] for w in words[:1]])
    last = max([speech[1]] + [w['end'] for w in words[-1:]])
    a = max(0, math.floor(round((on - LEAD) * fps, 6)))
    b = min(nframes, math.ceil(round((last + TAIL) * fps, 6)))
    return a, b


def frame_count(path):
    out = subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries',
                          'stream=nb_read_packets,r_frame_rate', '-of', 'json', path], check=True, **skillenv.TEXT).stdout
    st = json.loads(out)['streams'][0]
    num, den = st['r_frame_rate'].split('/')
    return int(st['nb_read_packets']), round(int(num) / int(den), 6)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def transcribe(path, work):
    import render_planned
    wav = os.path.join(work, os.path.basename(path) + '.wav')
    subprocess.run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', '-i', path, '-vn', '-ac', '1', '-ar', '16000', wav], check=True)
    return [{'word': w['text'], 'start': w['start'], 'end': w['end']} for w in render_planned.heard_words(wav)]


def measure(path, words=None, work=None, text=None):
    """a take's cut and what it was measured from (text: its line's script)"""
    import openings
    nframes, fps = frame_count(path)
    if words is None:
        words = transcribe(path, work or os.path.dirname(os.path.abspath(path)))
    speech = speech_span(path)
    clean, note = openings.clean_from(path)
    a, b = cut_frames(speech, script_words(words, text), nframes, fps)
    return {'path': path, 'sha256': sha256(path), 'frames': nframes, 'fps': fps, 'words': words, 'script': text, 'speech': list(speech),
            'cleanFrom': clean, 'cleanNote': note, 'fromFrame': a, 'toFrame': b, 'rule': RULE}


def main(argv):
    paths, maps = list(argv), {}
    dest = None
    if '--out' in paths:
        i = paths.index('--out')
        dest = paths[i + 1]
        del paths[i:i + 2]
    for flag in ('--words', '--lines'):
        if flag in paths:
            i = paths.index(flag)
            with open(paths[i + 1], encoding='utf-8') as fh:
                maps[flag] = json.load(fh)
            del paths[i:i + 2]
    words, lines = maps.get('--words', {}), maps.get('--lines', {})
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    out, refused = [], False
    for p in paths:
        try:
            out.append(measure(p, words.get(p), text=lines.get(p)))
        except NoSpeech as e:
            out.append({'path': p, 'refused': str(e)})
            refused = True
    print(json.dumps(out, indent=1))
    if dest:
        with open(dest, 'x', encoding='utf-8') as fh:
            json.dump(out, fh)
    return 2 if refused else 0


if __name__ == '__main__':
    skillenv.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
