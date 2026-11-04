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
  render      renders/<slug>-<label>.mp4                                  hf.py lint, hf.py render
  check       cut sheet, phone copy, caption sheet, QA report             check_cuts.py, reel_check.py
--from / --to resume or stop (a new look or platform only needs --from build). The label defaults to <look>-<platform>.
Per-variant copies of index.html, layout.json, build-report.json, qa-report.json and the sheets go to renders/.
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
        sys.exit(code)


def note_render_warnings(proj, output):
    """keep the render's output in work/render.log and add its [WARN] lines to build-report.json (reel_check fails on them)"""
    os.makedirs(os.path.join(proj, 'work'), exist_ok=True)
    with open(os.path.join(proj, 'work', 'render.log'), 'w', encoding='utf-8') as fh:
        fh.write(output)
    warned = [ln.strip() for ln in output.splitlines() if '[WARN]' in ln]
    path = os.path.join(proj, 'build-report.json')
    if warned and os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            rep = json.load(fh)
        rep['warnings'] = rep.get('warnings', []) + warned
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(rep, fh, indent=1)


def run_logged(stage, cmd, proj, cwd=None):
    """run() for the render: its output is shown, kept and searched for warnings"""
    t0 = time.time()
    print(f'== {stage}: {" ".join(os.path.basename(c) if os.sep in c else c for c in cmd[1:4])} ...', flush=True)
    p = subprocess.run(cmd, cwd=cwd, **skillenv.TEXT)
    print(p.stdout + p.stderr, end='', flush=True)
    note_render_warnings(proj, p.stdout + p.stderr)
    print(f'== {stage}: exit {p.returncode} in {time.time() - t0:.0f}s', flush=True)
    if p.returncode:
        sys.exit(p.returncode)


def setup(spec_path, proj):
    """copy the reel spec (shot paths made absolute) and the template's support files into the project"""
    with open(spec_path, encoding='utf-8') as fh:
        spec = json.load(fh)
    base = os.path.dirname(os.path.abspath(spec_path))
    for s in spec['shots']:
        s['file'] = os.path.normpath(os.path.join(base, skillenv.path_arg(s['file'])))
        if not os.path.exists(s['file']):
            sys.exit(f'shot not found: {s["file"]}')
    os.makedirs(os.path.join(proj, 'renders'), exist_ok=True)
    with open(os.path.join(proj, 'reel.json'), 'w', encoding='utf-8') as fh:
        json.dump(spec, fh, indent=1)
    for rel in ('hyperframes.json', 'package.json'):
        shutil.copyfile(os.path.join(TEMPLATE, rel), os.path.join(proj, rel))
    for sub, ext in (('fonts', ('.woff2', '.css')), ('sfx', ('.mp3',))):
        src, dst = os.path.join(TEMPLATE, 'assets', sub), os.path.join(proj, 'assets', sub)
        os.makedirs(dst, exist_ok=True)
        for f in os.listdir(src):
            if f.endswith(ext):
                shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))
    vendor_gsap(os.path.join(proj, 'assets', 'vendor'))
    return spec


def vendor_gsap(dst):
    """a local, hash-checked GSAP: a CDN fetch that fails mid-render (seen 2026-11-02) blocks the whole render"""
    name, sha, urls = GSAP
    path = os.path.join(dst, name)
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            if hashlib.sha256(fh.read()).hexdigest() == sha:
                return
    os.makedirs(dst, exist_ok=True)
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
    sys.exit('could not get GSAP 3.14.2 (needed once per project); check the network and run again')


def main():
    skillenv.utf8_stdio()
    if len(sys.argv) < 3 or '--out' not in sys.argv:
        sys.exit(__doc__)
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
            run_logged(stage, [py, os.path.join(SCRIPTS, 'hf.py'), 'render', '-o', render, '--quality', 'high'], proj, cwd=proj)
        elif stage == 'check':
            run('cuts', [py, os.path.join(SCRIPTS, 'check_cuts.py'), proj, render, '--phone'])
            qa = [py, os.path.join(SCRIPTS, 'reel_check.py'), proj, '--render', render, '--sheet']
            if '--require-provenance' in sys.argv:
                qa.append('--require-provenance')
            for flag in ('--expect-cuts', '--expect-text', '--expect-duration'):
                if arg(flag):
                    qa += [flag, arg(flag)]
            code = subprocess.run(qa).returncode
            for name, suffix in (('index.html', '.html'), ('layout.json', '-layout.json'), ('build-report.json', '-build-report.json'),
                                 ('qa-report.json', '-qa.json'), ('work/cut_check.jpg', '-cut_check.jpg'),
                                 ('work/caption_sheet.jpg', '-caption_sheet.jpg')):
                if os.path.exists(os.path.join(proj, name)):
                    shutil.copyfile(os.path.join(proj, name), stem + suffix)
            print(f'== check: exit {code}  ({stem}*)')
            sys.exit(code)


if __name__ == '__main__':
    main()
