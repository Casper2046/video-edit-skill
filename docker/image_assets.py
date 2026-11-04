#!/usr/bin/env python3
"""Render image build steps (docker/Dockerfile runs them; nothing here runs at render time).

  models    the faster-whisper CT2 models at pinned revisions into HF_HOME; model.bin checked against its pinned sha256,
            refs/main pointed at the pinned revision so WhisperModel('small.en') resolves offline
  assets    YuNet (faces.py's pinned sha256) into .models/, GSAP 3.14.2 (make_reel.py's pinned sha256) into vendor/
  verify    run with no network: both models load, YuNet opens, GSAP vendors from the local copy, the venv passes
            setup.py's self-test, the installed hyperframes is the pinned version
  manifest  JSON on stdout: versions, revisions and sha256s of everything in the image
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# (faster-whisper name, Hugging Face repo, pinned revision, model.bin sha256). Revisions resolved 2026-11-05 from
# https://huggingface.co/api/models/<repo>; they are also what the skill's host cache holds.
MODELS = [('small.en', 'Systran/faster-whisper-small.en', 'd1d751a5f8271d482d14ca55d9e2deeebbae577f',
           '62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a'),
          ('medium.en', 'Systran/faster-whisper-medium.en', 'a29b04bd15381511a9af671baec01072039215e3',
           '11b220779aea4c6f3ce9d2549c8a95ea869ed84066864b999531ef53e594fe5b')]
FILES = ['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt']


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def repo_dir(repo):
    return os.path.join(os.environ['HF_HOME'], 'hub', 'models--' + repo.replace('/', '--'))


def models():
    from faster_whisper.utils import _MODELS, download_model
    for name, repo, rev, bin_sha in MODELS:
        if _MODELS.get(name) != repo:
            sys.exit(f'faster-whisper maps {name} to {_MODELS.get(name)}, pinned {repo}')
        path = download_model(name, revision=rev)       # the same allow_patterns WhisperModel uses
        missing = [f for f in FILES if not os.path.isfile(os.path.join(path, f))]
        if missing or os.path.basename(path) != rev:
            sys.exit(f'{repo}@{rev}: got {path}, missing {missing}')
        got = sha256(os.path.join(path, 'model.bin'))
        if got != bin_sha:
            sys.exit(f'{repo}@{rev} model.bin sha256 {got}, pinned {bin_sha}')
        os.makedirs(os.path.join(repo_dir(repo), 'refs'), exist_ok=True)
        with open(os.path.join(repo_dir(repo), 'refs', 'main'), 'w', encoding='utf-8') as fh:
            fh.write(rev)                               # offline, WhisperModel(name) reads refs/main
        print(f'{repo}@{rev} model.bin {os.path.getsize(os.path.join(path, "model.bin"))} B sha256 ok')


def assets():
    sys.path.insert(0, os.path.join(SK, 'scripts'))
    import faces
    import make_reel
    print('yunet', faces.ensure_model())
    make_reel.vendor_gsap(os.path.join(SK, 'vendor'))
    print('gsap', os.path.join(SK, 'vendor', make_reel.GSAP[0]))


def verify():
    sys.path.insert(0, os.path.join(SK, 'scripts'))
    import onnxruntime as ort
    from faster_whisper import WhisperModel
    import faces
    import make_reel
    import setup
    import skillenv
    if os.environ.get('HF_HUB_OFFLINE') != '1':
        sys.exit('HF_HUB_OFFLINE is not 1')
    for name, _, _, _ in MODELS:
        WhisperModel(name, device='cpu', compute_type='int8')   # exactly as ingest.py / transcribe_cut.py load them
        print('whisper', name, 'loads offline')
    ort.InferenceSession(faces.ensure_model(download=None), providers=['CPUExecutionProvider'])
    print('yunet loads')
    with tempfile.TemporaryDirectory() as d:
        make_reel.vendor_gsap(d)
        if sha256(os.path.join(d, make_reel.GSAP[0])) != make_reel.GSAP[1]:
            sys.exit('gsap not vendored from the local copy')
    print('gsap vendors offline')
    if not setup.venv_ok(sys.executable):
        sys.exit('venv self-test failed')
    print('venv self-test ok')
    hf = skillenv.installed_hyperframes()
    pin = skillenv.HYPERFRAMES.split('@')[1]
    if not hf or skillenv._hyperframes_version(hf) != pin:
        sys.exit(f'installed hyperframes {hf} is not {pin}')
    print('hyperframes', hf, pin)
    if skillenv.load():
        sys.exit('a .platform.json is in the image')
    print('no .platform.json; tools from PATH:', skillenv.tool('ffmpeg'), skillenv.tool('ffprobe'))


def run(cmd):
    return subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace').stdout.strip()


def manifest():
    sys.path.insert(0, os.path.join(SK, 'scripts'))
    import faces
    import make_reel
    import skillenv
    skill = {}
    for root, dirs, files in os.walk(SK):
        dirs[:] = sorted(d for d in dirs if d not in ('.venv', '__pycache__'))
        for f in sorted(files):
            p = os.path.join(root, f)
            if p != os.path.join(SK, 'MANIFEST.json'):
                skill[os.path.relpath(p, SK)] = sha256(p)
    whisper = []
    for name, repo, rev, _ in MODELS:
        snap = os.path.join(repo_dir(repo), 'snapshots', rev)
        whisper.append({'name': name, 'repo': repo, 'revision': rev,
                        'files': {f: {'bytes': os.path.getsize(os.path.join(snap, f)), 'sha256': sha256(os.path.join(snap, f))}
                                  for f in FILES}})
    apt = dict(ln.split(' ', 1) for ln in run(['dpkg-query', '-W', '-f', '${Package} ${Version}\n']).splitlines() if ' ' in ln)
    with open('/opt/chrome/VERSION', encoding='utf-8') as fh:
        chrome = fh.read().strip().splitlines()
    out = {
        'built': os.environ.get('BUILD_DATE'), 'arch': run(['uname', '-m']),
        'base_image': os.environ.get('BASE_IMAGE'), 'skill_commit': os.environ.get('SKILL_COMMIT'),
        'node': run(['node', '--version']), 'hyperframes': skillenv._hyperframes_version(skillenv.installed_hyperframes()),
        'hyperframes_path': skillenv.installed_hyperframes(),
        'chrome_headless_shell': {'version': chrome[0], 'path': chrome[-1],
                                  'playwright': os.environ.get('PLAYWRIGHT_VERSION'),
                                  'chrome_for_testing': os.environ.get('CHROME_HEADLESS_SHELL_VERSION'),
                                  'puppeteer_browsers': os.environ.get('PUPPETEER_BROWSERS_VERSION')},
        'ffmpeg': {'apt': apt.get('ffmpeg'), 'version_line': run(['ffmpeg', '-version']).splitlines()[0]},
        'python': {'version': sys.version.split()[0], 'requirements_lock_sha256': sha256(os.path.join(SK, 'docker', 'requirements.lock')),
                   'packages': {p['name']: p['version'] for p in json.loads(run([sys.executable, '-m', 'pip', 'list', '--format=json']))}},
        'whisper_models': whisper, 'hf_home': os.environ.get('HF_HOME'), 'hf_hub_offline': os.environ.get('HF_HUB_OFFLINE'),
        'yunet': {'file': faces.ensure_model(download=None), 'sha256': faces.MODEL[1], 'url': faces.MODEL[2]},
        'gsap': {'file': os.environ.get('VIDEO_EDIT_GSAP'), 'sha256': make_reel.GSAP[1], 'urls': make_reel.GSAP[2]},
        'env': {k: os.environ.get(k) for k in ('PRODUCER_HEADLESS_SHELL_PATH', 'VIDEO_EDIT_HYPERFRAMES', 'VIDEO_EDIT_GSAP',
                                                'HYPERFRAMES_NO_TELEMETRY', 'PRODUCER_LOW_MEMORY_MODE')},
        'skill_files': skill,
        'apt': apt,
    }
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == '__main__':
    {'models': models, 'assets': assets, 'verify': verify, 'manifest': manifest}[sys.argv[1]]()
