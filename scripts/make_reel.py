#!/usr/bin/env python3
"""Spec mode: reel spec in, finished reel out. Runs every stage as its own command; nothing is edited by hand.

Usage:  PY make_reel.py <reel.json> --out <project_dir> [--from STAGE] [--to STAGE] [--look NAME] [--platform NAME]
                        [--label NAME] [--expect-cuts 83,183,...] [--expect-text FILE] [--expect-duration S]
                        [--require-provenance]
Stages, in order:
  ingest      per-shot words (small.en, onset-corrected) + silence maps   ingest.py --keep-order
  faces       lowest chin per shot -> caption_top where the spec has none  faces.py
  edl         one segment per narration sentence                          edl_from_words.py
  assemble    assets/aroll.mp4 + segments.json, end-card hold             assemble.py --end-hold
  transcribe  medium.en words on the cut (caption timing)                 transcribe_cut.py
  build       index.html + layout.json + build-report.json               build_reel.py
  render      renders/<slug>-<label>.mp4, then the overlays-only RGBA      hf.py lint, hf.py render x2
              frames work/overlays/ (overlay_mask.py)
  check       cut sheet, phone copy, caption sheet, QA report             check_cuts.py, reel_check.py
--from / --to resume or stop (a new look or platform only needs --from build). The label defaults to <look>-<platform>.
Per-variant copies of index.html, layout.json, build-report.json, qa-report.json and the sheets go to renders/.
Exit: 0 every check passes, 1 a QA check failed (reel_check.py), 2 the spec is refused (a shot file missing), 3 a stage
failed (its own exit code is printed on its `== <stage>: exit N` line) or the run crashed.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402
from overlay_mask import overlays_page  # noqa: E402

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SK, 'scripts')
TEMPLATE = os.path.join(SK, 'assets', 'template')
GSAP = ('gsap.min.js', 'c174bfce53a729418d57a8ad8625e7247c793a22fef8e2851e3cfa3de9cd8280',
        ['https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js', 'https://unpkg.com/gsap@3.14.2/dist/gsap.min.js'])
STAGES = ['ingest', 'faces', 'edl', 'assemble', 'transcribe', 'build', 'render', 'check']


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def run(stage, cmd, cwd=None):
    t0 = time.time()
    print(f'== {stage}: {" ".join(os.path.basename(c) if os.sep in c else c for c in cmd[1:4])} ...', flush=True)
    code = subprocess.run(cmd, cwd=cwd).returncode
    print(f'== {stage}: exit {code} in {time.time() - t0:.0f}s', flush=True)
    if code:
        sys.exit(skillenv.CRASH)


def note_render_warnings(proj, output, log='render.log', tag=''):
    """keep a render's output in work/<log> and add its [WARN] lines (prefixed with tag) to build-report.json (reel_check
    fails on them)"""
    os.makedirs(os.path.join(proj, 'work'), exist_ok=True)
    with open(os.path.join(proj, 'work', log), 'w', encoding='utf-8') as fh:
        fh.write(output)
    warned = [tag + ln.strip() for ln in output.splitlines() if '[WARN]' in ln]
    path = os.path.join(proj, 'build-report.json')
    if warned and os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            rep = json.load(fh)
        rep['warnings'] = rep.get('warnings', []) + warned
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(rep, fh, indent=1)


def render_both(film_cmd, overlay_cmd, proj):
    """the film, then its overlays-only twin (overlay_mask.py): one after the other, as run side by side on a multi-worker
    host the overlays render slowed the film's worker calibration into two [WARN]s (2026-11-05). Both outputs are kept
    and searched for warnings; either failing stops the run (exit 3) with its output printed"""
    for cmd, log, tag in ((film_cmd, 'render.log', ''), (overlay_cmd, 'overlays-render.log', 'overlays: ')):
        t0 = time.time()
        print(f'== render: {"overlays" if tag else "film"} ...', flush=True)
        p = subprocess.run(cmd, cwd=proj, **skillenv.TEXT)
        if not tag or p.returncode:
            print(p.stdout + p.stderr, end='', flush=True)
        note_render_warnings(proj, p.stdout + p.stderr, log, tag)
        print(f'== render: {"overlays" if tag else "film"} exit {p.returncode} in {time.time() - t0:.0f}s', flush=True)
        if p.returncode:
            sys.exit(skillenv.CRASH)


def setup(spec_path, proj):
    """copy the reel spec (shot paths made absolute) and the template's support files into the project"""
    with open(spec_path, encoding='utf-8') as fh:
        spec = json.load(fh)
    base = os.path.dirname(os.path.abspath(spec_path))
    for s in spec['shots']:
        s['file'] = os.path.normpath(os.path.join(base, skillenv.path_arg(s['file'])))
        if not os.path.exists(s['file']):
            print(f'shot not found: {s["file"]}', file=sys.stderr)
            sys.exit(2)
    os.makedirs(os.path.join(proj, 'renders'), exist_ok=True)
    with open(os.path.join(proj, 'reel.json'), 'w', encoding='utf-8') as fh:
        json.dump(spec, fh, indent=1)
    for rel in ('hyperframes.json', 'package.json'):
        shutil.copyfile(os.path.join(TEMPLATE, rel), os.path.join(proj, rel))
    for sub, ext in (('fonts', ('.woff2', '.css')), ('sfx', ('.mp3',))):
        if sub == 'sfx' and spec.get('sfx') is False:
            continue                                     # sound effects off (render_packet.py)
        src, dst = os.path.join(TEMPLATE, 'assets', sub), os.path.join(proj, 'assets', sub)
        os.makedirs(dst, exist_ok=True)
        for f in os.listdir(src):
            if f.endswith(ext):
                shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))
    vendor_gsap(os.path.join(proj, 'assets', 'vendor'))
    return spec


def vendor_gsap(dst):
    """a local, hash-checked GSAP: a CDN fetch that fails mid-render (seen 2026-11-02) blocks the whole render.
    VIDEO_EDIT_GSAP names a local copy (the render image has one, so a run needs no network); the CDN is the fallback."""
    name, sha, urls = GSAP
    path = os.path.join(dst, name)
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            if hashlib.sha256(fh.read()).hexdigest() == sha:
                return
    os.makedirs(dst, exist_ok=True)
    local = os.environ.get('VIDEO_EDIT_GSAP')
    if local and os.path.isfile(local):
        with open(local, 'rb') as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() == sha:
            with open(path, 'wb') as fh:
                fh.write(data)
            return
        print(f'gsap at {local} does not match the pinned sha256, not used')
    for url in urls * 2:
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = r.read()
        except OSError as e:
            print(f'gsap download failed ({url}): {e}')
            continue
        if hashlib.sha256(data).hexdigest() == sha:
            with open(path, 'wb') as fh:
                fh.write(data)
            return
        print(f'gsap from {url} does not match the pinned sha256, not used')
    print('could not get GSAP 3.14.2 (needed once per project); check the network and run again', flush=True)
    sys.exit(skillenv.CRASH)


def main():
    skillenv.utf8_stdio()
    if len(sys.argv) < 3 or '--out' not in sys.argv:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    spec_path, proj = skillenv.path_arg(sys.argv[1]), os.path.abspath(skillenv.path_arg(arg('--out')))
    first, last = STAGES.index(arg('--from', 'ingest')), STAGES.index(arg('--to', 'check'))
    spec = setup(spec_path, proj)
    look, platform = arg('--look', spec.get('look', 'bold')), arg('--platform', spec.get('platform', 'reels'))
    label = arg('--label', f'{look}-{platform}')
    stem = os.path.join(proj, 'renders', f"{spec.get('slug', 'reel')}-{label}")
    render = stem + '.mp4'
    py = sys.executable
    hold = (spec.get('cards') or {}).get('end', {}).get('hold', 0)

    for stage in STAGES[first:last + 1]:
        if stage == 'ingest':
            run(stage, [py, os.path.join(SCRIPTS, 'ingest.py'), proj, '--keep-order', *[s['file'] for s in spec['shots']]])
        elif stage == 'faces':
            run(stage, [py, os.path.join(SCRIPTS, 'faces.py'), proj])
        elif stage == 'edl':
            run(stage, [py, os.path.join(SCRIPTS, 'edl_from_words.py'), proj])
        elif stage == 'assemble':
            run(stage, [py, os.path.join(SCRIPTS, 'assemble.py'), proj, '--end-hold', str(hold)])
        elif stage == 'transcribe':
            run(stage, [py, os.path.join(SCRIPTS, 'transcribe_cut.py'), proj, spec.get('names', '')])
        elif stage == 'build':
            run(stage, [py, os.path.join(SCRIPTS, 'build_reel.py'), proj, '--look', look, '--platform', platform])
        elif stage == 'render':
            run('lint', [py, os.path.join(SCRIPTS, 'hf.py'), 'lint'], cwd=proj)
            work = os.path.join(proj, 'work')
            os.makedirs(work, exist_ok=True)
            with open(os.path.join(proj, 'index.html'), encoding='utf-8') as fh:
                page = overlays_page(fh.read())
            with open(os.path.join(work, 'overlays.html'), 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(page)
            shutil.rmtree(os.path.join(work, 'overlays'), ignore_errors=True)    # never check a new film against old frames
            hf = [py, os.path.join(SCRIPTS, 'hf.py'), 'render', '--quality', 'high']
            render_both(hf + ['-o', render], hf + ['-c', 'work/overlays.html', '--format', 'png-sequence', '-o', 'work/overlays'], proj)
        elif stage == 'check':
            run('cuts', [py, os.path.join(SCRIPTS, 'check_cuts.py'), proj, render, '--phone'])
            qa = [py, os.path.join(SCRIPTS, 'reel_check.py'), proj, '--render', render, '--sheet']
            if '--require-provenance' in sys.argv:
                qa.append('--require-provenance')
            for flag in ('--expect-cuts', '--expect-text', '--expect-duration'):
                if arg(flag):
                    qa += [flag, arg(flag)]
            got = subprocess.run(qa).returncode
            code = got if got in (0, 1) else skillenv.CRASH      # 1 is a failed check; anything else a crash
            for name, suffix in (('index.html', '.html'), ('layout.json', '-layout.json'), ('build-report.json', '-build-report.json'),
                                 ('qa-report.json', '-qa.json'), ('work/cut_check.jpg', '-cut_check.jpg'),
                                 ('work/caption_sheet.jpg', '-caption_sheet.jpg')):
                if os.path.exists(os.path.join(proj, name)):
                    shutil.copyfile(os.path.join(proj, name), stem + suffix)
            print(f'== check: reel_check exit {got}, make_reel exit {code}  ({stem}*)')
            sys.exit(code)


if __name__ == '__main__':
    skillenv.main_or_3(main)
