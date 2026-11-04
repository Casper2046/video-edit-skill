#!/usr/bin/env python3
"""Offline render image tests: nothing a packet run needs may come from the network once the image is built.
Standard library only, no Docker, no network:   PY tests/test_offline_image.py
"""
import hashlib
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import make_reel as M  # noqa: E402
import setup  # noqa: E402
import skillenv  # noqa: E402

PIN = skillenv.HYPERFRAMES.split('@')[1]


def no_network(*a, **k):
    raise AssertionError('network used')


class InstalledHyperframes(unittest.TestCase):
    def test_the_image_binary_named_in_the_environment_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            hf = os.path.join(d, 'hyperframes')
            open(hf, 'wb').close()
            self.assertEqual(skillenv.installed_hyperframes({'VIDEO_EDIT_HYPERFRAMES': hf}, find=lambda n: (None, False)), hf)

    def test_a_named_binary_that_is_not_there_stops_the_run_instead_of_downloading(self):
        with self.assertRaises(SystemExit):
            skillenv.installed_hyperframes({'VIDEO_EDIT_HYPERFRAMES': os.path.join(ROOT, 'no-such-hyperframes')})

    def test_a_hyperframes_on_path_is_used_only_at_the_pinned_version(self):
        find = lambda n: ('/usr/local/bin/hyperframes', True)   # noqa: E731
        self.assertEqual(skillenv.installed_hyperframes({}, find=find, version=lambda p: PIN), '/usr/local/bin/hyperframes')
        self.assertIsNone(skillenv.installed_hyperframes({}, find=find, version=lambda p: '0.8.141'))   # a Mac's newer global one
        self.assertIsNone(skillenv.installed_hyperframes({}, find=lambda n: (None, False), version=no_network))

    def test_the_installed_binary_replaces_npx_yes(self):
        with mock.patch.object(skillenv, 'installed_hyperframes', return_value='/usr/local/bin/hyperframes'):
            argv, env = skillenv.hyperframes_command(['render', '-o', 'x.mp4'])
        self.assertEqual(argv, ['/usr/local/bin/hyperframes', 'render', '-o', 'x.mp4'])
        self.assertEqual(env['HYPERFRAMES_NO_TELEMETRY'], os.environ.get('HYPERFRAMES_NO_TELEMETRY', '1'))

    def test_without_an_installed_binary_npx_runs_the_pinned_version(self):
        with mock.patch.object(skillenv, 'installed_hyperframes', return_value=None), \
                mock.patch.object(skillenv, 'load', return_value={}), \
                mock.patch.object(skillenv, 'find_node', return_value=('/opt/node/bin/node', 'v22.23.4', True)), \
                mock.patch.object(skillenv, 'npx_command', return_value=['/opt/node/bin/npx']):
            argv, _ = skillenv.hyperframes_command(['lint'])
        self.assertEqual(argv, ['/opt/node/bin/npx', '--yes', skillenv.HYPERFRAMES, 'lint'])


class LocalGsap(unittest.TestCase):
    def pinned(self, data):
        return ('gsap.min.js', hashlib.sha256(data).hexdigest(), ['https://example.invalid/gsap.min.js'])

    def test_a_local_copy_matching_the_pin_is_used_with_no_network(self):
        good = b'gsap 3.14.2 bytes'
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, 'gsap.min.js'), os.path.join(d, 'proj', 'assets', 'vendor')
            with open(src, 'wb') as fh:
                fh.write(good)
            with mock.patch.object(M, 'GSAP', self.pinned(good)), mock.patch.dict(os.environ, {'VIDEO_EDIT_GSAP': src}), \
                    mock.patch.object(M.urllib.request, 'urlopen', side_effect=no_network):
                M.vendor_gsap(dst)
            with open(os.path.join(dst, 'gsap.min.js'), 'rb') as fh:
                self.assertEqual(fh.read(), good)

    def test_a_local_copy_that_does_not_match_the_pin_is_never_used(self):
        good = b'gsap 3.14.2 bytes'
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, 'gsap.min.js'), os.path.join(d, 'proj', 'assets', 'vendor')
            with open(src, 'wb') as fh:
                fh.write(b'tampered')
            with mock.patch.object(M, 'GSAP', self.pinned(good)), mock.patch.dict(os.environ, {'VIDEO_EDIT_GSAP': src}), \
                    mock.patch.object(M.urllib.request, 'urlopen', side_effect=lambda url, timeout: io.BytesIO(good)), \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as out:
                M.vendor_gsap(dst)
            with open(os.path.join(dst, 'gsap.min.js'), 'rb') as fh:
                self.assertEqual(fh.read(), good)
            self.assertIn('does not match the pinned sha256', out.getvalue())


class ImageContents(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
            return fh.read()

    def test_the_image_lock_pins_every_setup_requirement(self):
        lock = self.read(os.path.join('docker', 'requirements.lock'))
        for req in setup.REQUIREMENTS:
            name = re.match(r'[A-Za-z0-9_.-]+', req).group(0).lower().replace('_', '-')
            self.assertRegex(lock.lower().replace('_', '-'), rf'(?m)^{re.escape(name)}==\S+', req)

    def test_sound_effects_machine_records_and_models_stay_out_of_the_build_context(self):
        lines = [ln.strip() for ln in self.read('.dockerignore').splitlines() if ln.strip() and not ln.startswith('#')]
        for path in ('assets/template/assets/sfx', '.platform.json', '.models', '.venv', '.git'):
            hits = [i for i, ln in enumerate(lines) if ln.rstrip('/') in (path, '**/' + path) or ln == '*']
            self.assertTrue(hits, f'{path} not excluded')
            last = max(hits)
            reincluded = [ln for ln in lines[last + 1:] if ln.startswith('!') and path.startswith(ln[1:].rstrip('/'))]
            self.assertFalse(reincluded, f'{path} excluded, then re-included by {reincluded}')


if __name__ == '__main__':
    unittest.main(verbosity=1)
