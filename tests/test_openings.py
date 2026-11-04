#!/usr/bin/env python3
"""Unclean-opening detection (scripts/openings.py). Run with the skill's Python (needs numpy and ffmpeg):
    PY -m unittest tests.test_openings
Synthetic shots are generated per run as raw grey frames muxed into .nut (no encode). The labelled real shots are
private: point VIDEO_EDIT_OPENING_FIXTURES at a folder holding labels.json
({"shots": [{"file": "<shot file, absolute or relative to the folder>", "clean_from": seconds or null}]}).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import openings as O  # noqa: E402
import skillenv  # noqa: E402

W, H, FPS, N = 360, 640, 24, 96   # 4s shot
GONE = 18                         # the synthetic occluder's opacity reaches 0 at this frame (0.75s, like the labelled unclean shot)


def blur(a, sigma):
    """separable Gaussian blur of a 2-D float array"""
    r = int(3 * sigma)
    k = np.exp(-np.arange(-r, r + 1) ** 2 / (2 * sigma ** 2))
    k /= k.sum()
    a = np.apply_along_axis(lambda v: np.convolve(np.pad(v, r, mode='edge'), k, 'valid'), 0, a)
    return np.apply_along_axis(lambda v: np.convolve(np.pad(v, r, mode='edge'), k, 'valid'), 1, a)


def shot(occluder=None):
    """N grey frames: a flat wall (top) over a sharp block-textured subject that sways +-3px, sensor noise on every
    frame (sigma 0.4: the labelled shots' quietest tiles sit 0.15-0.36 grey levels from their median frame); `occluder`
    'soft' or 'sharp' puts a dark disk at the top-left edge whose opacity falls linearly to 0 at GONE"""
    rnd = np.random.default_rng(5)
    texture = np.kron(rnd.integers(40, 220, (H // 8, W // 8)), np.ones((8, 8)))
    yy, xx = np.mgrid[0:H, 0:W]
    disk = ((xx - 10) ** 2 + (yy - 110) ** 2 < 70 ** 2).astype(float)
    mask = blur(disk, 18) if occluder == 'soft' else disk
    frames = []
    for i in range(N):
        f = np.full((H, W), 150.0)
        dx = int(round(3 * np.sin(i / 6)))
        f[260:600, 60:300] = np.roll(texture, dx, axis=1)[260:600, 60:300]
        if occluder:
            a = max(0.0, 1 - i / GONE) * mask
            f = f * (1 - a) + 20 * a
        f += rnd.normal(0, .4, f.shape)
        frames.append(np.clip(f, 0, 255).astype(np.uint8))
    return np.stack(frames)


def write(frames, path):
    subprocess.run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray',
                    '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-', '-c:v', 'rawvideo', path],
                   input=frames.tobytes(), check=True)
    return path


class Synthetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files = {k: write(shot(k), os.path.join(cls.tmp.name, f'{k or "clean"}.nut')) for k in ('soft', 'sharp', None)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_a_soft_foreground_object_over_the_opening_is_found_within_two_frames(self):
        t, note = O.clean_from(self.files['soft'])
        self.assertIsNotNone(t, note)
        self.assertLessEqual(abs(t * FPS - GONE), 2, (t, note))
        self.assertIn('settled', note)

    def test_a_clean_shot_has_no_clean_from(self):
        t, note = O.clean_from(self.files[None])
        self.assertIsNone(t, note)
        self.assertTrue(note)

    def test_a_sharp_object_over_the_opening_is_action_not_an_unclean_opening(self):
        # an in-focus object moving at a shot's opening is action (a labelled shot is clean): only soft edges count
        t, note = O.clean_from(self.files['sharp'])
        self.assertIsNone(t, note)

    def test_the_cli_prints_one_line_per_file(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'openings.py'), self.files['soft'], self.files[None]],
                             **skillenv.TEXT, check=True).stdout.splitlines()
        self.assertEqual(len(out), 2, out)
        self.assertTrue(out[0].startswith('soft.nut') and out[1].startswith('clean.nut'), out)


FIX = os.environ.get('VIDEO_EDIT_OPENING_FIXTURES', '')
HAVE_FIX = bool(FIX) and os.path.exists(os.path.join(FIX, 'labels.json'))


@unittest.skipUnless(HAVE_FIX, 'private labelled shots: set VIDEO_EDIT_OPENING_FIXTURES')
class Labelled(unittest.TestCase):
    """the detector against clean_from labels set by frame review on real generated shots"""

    def test_each_label_is_matched_within_two_frames(self):
        with open(os.path.join(FIX, 'labels.json'), encoding='utf-8') as fh:
            shots = json.load(fh)['shots']
        off = {}
        for s in shots:
            path = os.path.join(FIX, s['file'])
            got, note = O.clean_from(path)
            want, fps = s['clean_from'], skillenv.video_fps(path)
            if (got is None) != (want is None) or (got is not None and abs(got - want) * fps > 2 + 1e-6):
                off[os.path.basename(path)] = (got, want, note)
        self.assertEqual(off, {})


if __name__ == '__main__':
    unittest.main()
