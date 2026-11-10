#!/usr/bin/env python3
"""The caller's edit packet in, finished reel out: writes a reel spec and an expected-text file, then runs make_reel.py.

Usage:  PY render_packet.py <packet.json> --shots DIR --out DIR [--look bold] [--platform reels] [--from STAGE] [--to STAGE]
A planned packet (one with `graphics`, SPEC v1.3) goes to render_planned.py instead; the rest of this note is spec mode.
The packet: {shots: [{narration}], provenance: {episodeId, productionId, ..., shots:
[{operationId, sha256, narration, url, ...}]}}. Each shot is the file in --shots whose sha256 is provenance.shots[i].sha256;
url is never fetched. A missing, ambiguous or mismatched shot, one that matches but cannot be decoded, an unreadable
packet or one reel_check would refuse exits 2 naming it.
Writes <out>/packet.reel.json (shots in packet order, provenance copied in, no sentences or caption_top, sfx off, one_take on, a
shot's clean_from where openings.py finds an unclean opening), <out>/openings.json (every shot's opening note) and
<out>/expected-text.txt (every caption chunk, one per line), then runs make_reel.py --require-provenance --expect-text
and exits with its exit code: 0 every check passes, 1 a QA check failed, 2 refused, 3 a stage failed or crashed (a
signal or any other code is 3 too).
"""
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402
from build_reel import default_chunks  # noqa: E402
from edl_from_words import sentences  # noqa: E402
from openings import clean_from  # noqa: E402
from reel_check import provenance_problems  # noqa: E402

SCRIPTS = os.path.dirname(os.path.abspath(__file__))


class Refused(Exception):
    pass


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def same_words(a, b):
    return ' '.join(str(a).split()) == ' '.join(str(b).split())    # reel_check's comparison, so the two gates agree


def hashes(d):
    """{sha256: [path, ...]} for the regular files directly in d, read once"""
    out = {}
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            h = hashlib.sha256()
            with open(p, 'rb') as fh:
                for block in iter(lambda: fh.read(1 << 20), b''):
                    h.update(block)
            out.setdefault(h.hexdigest(), []).append(p)
    return out


def match_shots(pk, shots_dir):
    """[absolute file] per packet shot, by sha256; Refused names the first shot that has no single match"""
    prov = pk.get('provenance') or {}
    made, cut = prov.get('shots') or [], pk.get('shots') or []
    if len(made) != len(cut):
        raise Refused(f'{len(made)} produced shots, the packet cuts {len(cut)}')
    if not os.path.isdir(shots_dir):
        raise Refused(f'no shots dir {shots_dir}')
    found, files = hashes(shots_dir), []
    for k, (m, s) in enumerate(zip(made, cut), 1):
        who = f"shot {k} (operationId {m.get('operationId')})"
        if not same_words(s.get('narration', ''), m.get('narration', '')):
            raise Refused(f'{who}: narration differs from the approved narration')
        hits = found.get(m.get('sha256'), [])
        if not hits:
            raise Refused(f"{who}: no file in {shots_dir} has sha256 {m.get('sha256')}")
        if len(hits) > 1:
            raise Refused(f"{who}: {len(hits)} files have its sha256: {', '.join(os.path.basename(p) for p in hits)}")
        files.append(os.path.abspath(hits[0]))
    return files


def expected_text(narrations):
    """every caption chunk build_reel puts on screen, in order: default chunks per sentence (the spec has no sentences)"""
    out = []
    for narr in narrations:
        for toks in sentences(narr):
            i = 0
            for n, _ in default_chunks(len(toks)):
                out.append(' '.join(toks[i:i + n]))
                i += n
    return out


def convert(packet_path, shots_dir, out, look, platform):
    """-> (spec path, expected-text path), both in out"""
    try:
        with open(packet_path, encoding='utf-8') as fh:
            pk = json.load(fh)
    except (OSError, ValueError) as e:
        raise Refused(f'cannot read the packet: {e}')
    files = match_shots(pk, shots_dir)
    prov = pk['provenance']
    slug = 'ep-' + (re.sub(r'[^a-z0-9]', '', str(prov.get('episodeId', '')).lower())[:8] or 'unknown')
    spec = {'slug': slug, 'look': look, 'platform': platform, 'sfx': False, 'one_take': True,
            'shots': [{'file': f, 'narration': s['narration']} for f, s in zip(files, pk['shots'])], 'provenance': prov}
    problems = provenance_problems(spec, require=True)
    if problems:
        raise Refused('provenance: ' + '; '.join(problems))
    os.makedirs(out, exist_ok=True)
    notes = []
    for k, (shot, m) in enumerate(zip(spec['shots'], prov['shots']), 1):
        try:
            at, note = clean_from(shot['file'])
        except (subprocess.CalledProcessError, ValueError, IndexError, ZeroDivisionError) as e:
            raise Refused(f"shot {k} (operationId {m.get('operationId')}): {os.path.basename(shot['file'])} matches its "
                          f'sha256 but cannot be decoded ({type(e).__name__}: {e})')
        if at is not None:
            shot['clean_from'] = at
        notes.append({'shot': k, 'clean_from': at, 'note': note})
    with open(os.path.join(out, 'openings.json'), 'w', encoding='utf-8') as fh:
        json.dump(notes, fh, indent=1)
    spec_path, text_path = os.path.join(out, 'packet.reel.json'), os.path.join(out, 'expected-text.txt')
    with open(spec_path, 'w', encoding='utf-8') as fh:
        json.dump(spec, fh, indent=1)
    with open(text_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(''.join(ln + '\n' for ln in expected_text(s['narration'] for s in spec['shots'])))
    return spec_path, text_path


def planned(packet_path):
    """True when the packet carries a plan (graphics); anything unreadable stays with the spec-mode path, which refuses it"""
    try:
        with open(packet_path, encoding='utf-8') as fh:
            return 'graphics' in json.load(fh)
    except (OSError, ValueError, TypeError):
        return False


def main():
    skillenv.utf8_stdio()
    if len(sys.argv) < 2 or '--shots' not in sys.argv or '--out' not in sys.argv:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    out = os.path.abspath(skillenv.path_arg(arg('--out')))
    if planned(skillenv.path_arg(sys.argv[1])):          # a planned packet (SPEC v1.3): the kit renders it
        import render_planned
        sys.exit(render_planned.main(skillenv.path_arg(sys.argv[1]), skillenv.path_arg(arg('--shots')), out))
    try:
        spec_path, text_path = convert(skillenv.path_arg(sys.argv[1]), skillenv.path_arg(arg('--shots')), out,
                                       arg('--look', 'bold'), arg('--platform', 'reels'))
    except Refused as e:
        print(f'packet refused: {e}', file=sys.stderr)
        sys.exit(2)
    cmd = [sys.executable, os.path.join(SCRIPTS, 'make_reel.py'), spec_path, '--out', out,
           '--require-provenance', '--expect-text', text_path]
    for flag in ('--from', '--to'):
        if arg(flag):
            cmd += [flag, arg(flag)]
    code = subprocess.call(cmd)
    sys.exit(code if code in (0, 1, 2, 3) else 3)      # a signal or an unknown code is a stage failure


if __name__ == '__main__':
    skillenv.main_or_3(main)
