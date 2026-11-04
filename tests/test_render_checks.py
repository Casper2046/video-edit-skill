#!/usr/bin/env python3
"""Checks measured on the render (scripts/overlay_mask.py, reel_check.py). Run with the skill's Python (needs numpy,
Pillow and ffmpeg):   PY -m unittest tests.test_render_checks
Synthetic only: overlay frames are RGBA PNGs drawn per run with boxes at known pixels and frames; every expected value
comes from that construction.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import overlay_mask as M  # noqa: E402
import reel_check as Q  # noqa: E402
import skillenv  # noqa: E402
from build_reel import PLATFORMS  # noqa: E402

W, H = 1080, 1920
RED, GREEN, WHITE = (255, 0, 0), (0, 255, 0), (255, 255, 255)


def frame(*rects):
    """an empty RGBA overlay frame with (x0, y0, x1, y1, rgb, alpha) rectangles drawn half-open"""
    a = np.zeros((H, W, 4), np.uint8)
    for x0, y0, x1, y1, rgb, alpha in rects:
        a[y0:y1, x0:x1] = (*rgb, alpha)
    return a


def write(d, frames):
    for i, a in enumerate(frames):
        Image.fromarray(a, 'RGBA').save(os.path.join(d, f'frame_{i + 1:06d}.png'))
    return d


class Mask(unittest.TestCase):
    """the mask on a synthetic overlay sequence: captions tagged red, every text shadow green (overlays_page)"""

    def test_boxes_per_frame_from_alpha_over_32_with_text_shadows_kept_apart(self):
        frames = [frame(),
                  frame((100, 1000, 400, 1100, RED, 255),          # a caption
                        (100, 1100, 400, 1120, GREEN, 100),        # its text shadow: halo, not ink
                        (90, 990, 410, 1000, GREEN, 32),           # the shadow's fringe: alpha 32 is not over the threshold
                        (200, 300, 800, 360, WHITE, 200),          # a card background: ink
                        (50, 50, 52, 52, WHITE, 255)),             # a 4px speck: not a box
                  frame((100, 1000, 150, 1050, RED, 33),           # two glyphs touching at a corner: one box
                        (150, 1050, 200, 1100, RED, 255),
                        (600, 1000, 650, 1040, RED, 255))]         # a third, 400px away: its own box
        with tempfile.TemporaryDirectory() as d:
            got = M.read(write(d, frames))
        self.assertEqual([f['t'] for f in got], [0.0, 1 / 30, 2 / 30])
        self.assertEqual(got[0], {'t': 0.0, 'ink': [], 'caption': [], 'halo': [], 'caption_halo': []})
        self.assertEqual(sorted(got[1]['ink']), [[100, 1000, 400, 1100], [200, 300, 800, 360]])
        self.assertEqual(got[1]['caption'], [[100, 1000, 400, 1100]])
        self.assertEqual(sorted(got[1]['halo']), [[100, 1000, 400, 1120], [200, 300, 800, 360]])
        self.assertEqual(got[1]['caption_halo'], [[100, 1000, 400, 1120]])
        glyphs = [[100, 1000, 200, 1100], [600, 1000, 650, 1040]]
        for k in ('ink', 'caption', 'halo', 'caption_halo'):
            self.assertEqual(sorted(got[2][k]), glyphs)

    def test_a_card_drawn_over_a_caption_is_not_caption(self):
        # a white card's box shadow (ink: only text shadows are halo) runs into the caption: one ink region, but only
        # the red pixels are caption
        a = frame((100, 700, 900, 940, (0, 0, 0), 90), (100, 600, 900, 900, WHITE, 255), (150, 950, 850, 1050, RED, 255))
        a[940:950, 100:900] = (0, 0, 0, 60)
        got = M.boxes(a)
        self.assertEqual(got['ink'], [[100, 600, 900, 1050]])
        self.assertEqual(got['caption'], [[150, 950, 850, 1050]])

    def test_overlays_page_keeps_the_overlays_and_tags_captions_and_text_shadows(self):
        page = ('<style>@font-face{src:url(\'assets/fonts/A.woff2\')}</style>\n<script src="assets/vendor/gsap.min.js"></script>\n'
                '<style>\n.dw{position:absolute;color:#FFFFFF;text-shadow:0 10px 40px rgba(0,0,0,.55),0 2px 8px rgba(60,30,20,.35);x:1}\n'
                '.step{color:#F2EFE9;text-shadow:0 2px 12px rgba(0,0,0,.6);white-space:nowrap}\n'
                '.msg{background:#fff;box-shadow:0 20px 50px rgba(0,0,0,.35)}\n'
                '</style>\n</head>\n<div id="root">\n  <audio id="bga" src="assets/aroll.mp4" data-start="0"></audio>\n'
                '<audio id="sfx0" src="assets/sfx/w.mp3"></audio>\n  <div id="stage">\n'
                '    <video id="bgv" class="full g" src="assets/aroll.mp4" muted></video>\n  </div>\n'
                '<div id="w0-0" class="dw sm">hi</div>\n</div>')
        out = M.overlays_page(page)
        self.assertNotIn('<audio', out)
        self.assertNotIn('<video', out)
        self.assertIn('<div id="stage">\n    <i class="g"></i>\n  </div>', out)
        self.assertIn("url('assets/fonts/A.woff2')", out)
        self.assertIn('src="assets/vendor/gsap.min.js"', out)
        self.assertIn('text-shadow:0 10px 40px rgba(0,255,0,.55),0 2px 8px rgba(0,255,0,.35);x:1}', out)
        self.assertIn('.step{color:#F2EFE9;text-shadow:0 2px 12px rgba(0,255,0,.6);', out)
        self.assertIn('box-shadow:0 20px 50px rgba(0,0,0,.35)', out)        # a card's shadow is not text shadow
        self.assertIn('html,body,#root{background:transparent !important}.fx{display:none !important}'
                      '.dw[id^="w"]{color:#f00 !important}', out)
        self.assertIn('<div id="w0-0" class="dw sm">hi</div>', out)


def at(t, ink=(), caption=(), halo=None, caption_halo=None):
    """one frame of overlay boxes; captions are ink too, halo defaults to the ink"""
    ink = [list(b) for b in ink] + [list(b) for b in caption]
    return {'t': t, 'ink': ink, 'caption': [list(b) for b in caption], 'halo': [list(b) for b in halo] if halo else ink,
            'caption_halo': [list(b) for b in caption_halo] if caption_halo else [list(b) for b in caption]}


class SafeZone(unittest.TestCase):
    """ink inside the platform zone (reels: x 35..1045, y 220..1470, x <= 980 from y 1155) on every settled frame"""
    LAYOUT = [{'id': 'w0-0', 'kind': 'caption', 'box': [100, 1300, 900, 1470], 't0': 0.0, 't1': 2.0},   # settled .2-1.85
              {'id': 'st0', 'kind': 'step', 'box': [300, 240, 780, 280], 't0': 0.0, 't1': 2.0},
              {'id': 'msg', 'kind': 'message', 'box': [110, 790, 970, 910], 't0': 0.0, 't1': 2.0}]
    CAP = (100, 1300, 900, 1470)

    def check(self, *frames):
        return Q.safe_zone(list(frames), PLATFORMS['reels'], self.LAYOUT)

    def test_a_halo_outside_the_zone_with_its_ink_inside_passes_and_is_recorded(self):
        fails, info = self.check(at(1.0, caption=[self.CAP], halo=[(100, 1300, 900, 1477)]))
        self.assertEqual(fails, {})
        self.assertEqual(info['w0-0'], {'halo_px': 7, 'spill_px': 0, 'spill_frames': 0})
        self.assertEqual(info['st0'], {'halo_px': 0, 'spill_px': 0, 'spill_frames': 0})

    def test_ink_outside_the_zone_on_a_settled_frame_fails_and_is_named(self):
        fails, _ = self.check(at(1.0, ink=[(300, 219, 780, 280)], caption=[(100, 1300, 900, 1471)]))
        self.assertEqual(fails, {'st0': (1.0, [300, 219, 780, 280]), 'w0-0': (1.0, [100, 1300, 900, 1471])})

    def test_a_card_background_outside_the_zone_fails(self):
        fails, _ = self.check(at(1.0, ink=[(110, 790, 1046, 910)]))
        self.assertEqual(fails, {'msg': (1.0, [110, 790, 1046, 910])})

    def test_ink_outside_only_during_its_fade_in_passes_and_is_recorded(self):
        fails, info = self.check(at(1 / 30, caption=[(60, 1290, 940, 1480)]), at(2 / 30, caption=[(80, 1295, 920, 1475)]),
                                 at(1.0, caption=[self.CAP]))
        self.assertEqual(fails, {})
        self.assertEqual(info['w0-0'], {'halo_px': 0, 'spill_px': 10, 'spill_frames': 2})

    def test_ink_on_screen_outside_its_planned_span_fails(self):
        on = [at(1.0, caption=[self.CAP]), at(2.0 + 1 / 30, caption=[self.CAP])]       # one frame past the span: allowed
        self.assertEqual(Q.unexplained_ink(on, self.LAYOUT), {})
        late = on + [at(2.0 + 2 / 30, caption=[self.CAP]), at(5.0, ink=[(0, 0, 40, 40)])]
        self.assertEqual(Q.unexplained_ink(late, self.LAYOUT), {'w0-0': (2.0 + 2 / 30, list(self.CAP)), 'unplanned': (5.0, [0, 0, 40, 40])})


def film_audio(bursts, dur=3.0, sr=16000):
    """the film's sound: room noise at -50dB, speech-like noise at -15dB inside each (start, end)"""
    rnd = np.random.default_rng(7)
    t = np.arange(int(dur * sr)) / sr
    amp = np.where(np.any([(t >= a) & (t < b) for a, b in bursts], axis=0), 10 ** (-15 / 20), 10 ** (-50 / 20))
    return (np.clip(rnd.normal(0, 1, len(t)) * amp / 2, -1, 1) * 32767).astype('<i2').tobytes(), sr


class CaptionOnset(unittest.TestCase):
    """two sentences cut at 1.0s (frame 30); speech starts at 0.30s and 1.50s in the film's sound"""
    SEGS = [{'id': 'a', 'clip': 'c1', 'frame': 0, 'frames': 30, 'src_in': 0.0, 'word_idx': [0]},
            {'id': 'b', 'clip': 'c1', 'frame': 30, 'frames': 60, 'src_in': 1.0, 'word_idx': [1]}]
    WORDS = {'c1': [{'start': .35}, {'start': 1.58}]}      # Whisper a little late on both
    AUDIO = film_audio([(.3, .8), (1.5, 2.2)])
    BOX = (100, 1000, 900, 1100)

    def frames(self, first_b):
        """sentence a's caption on frames 8-29 (still drawn at 29, gone at the cut), sentence b's from frame first_b"""
        return [at(i / 30, caption=[self.BOX] if 8 <= i < 30 or i >= first_b else []) for i in range(90)]

    def test_a_caption_appearing_02s_after_the_speech_onset_fails(self):
        shown = Q.caption_shown(self.frames(51), self.SEGS)
        self.assertEqual(shown, {0: 8 / 30, 1: 51 / 30})
        errs = Q.caption_errors(self.SEGS, self.WORDS, self.AUDIO, shown)
        self.assertEqual([sid for sid, _, _ in errs], ['a', 'b'])
        self.assertAlmostEqual(errs[0][1], .3, delta=.04)
        self.assertAlmostEqual(errs[1][1], 1.5, delta=.04)
        self.assertAlmostEqual(errs[1][2], 51 / 30 - 1.5, delta=.04)
        self.assertGreater(max(abs(e) for _, _, e in errs), .15)

    def test_a_caption_on_its_speech_onset_passes(self):
        errs = Q.caption_errors(self.SEGS, self.WORDS, self.AUDIO, Q.caption_shown(self.frames(44), self.SEGS))
        self.assertAlmostEqual(errs[0][2], 8 / 30 - .3, delta=.04)
        self.assertAlmostEqual(errs[1][2], 44 / 30 - 1.5, delta=.04)
        self.assertLess(max(abs(e) for _, _, e in errs), .15)

    def test_a_caption_never_drawn_in_its_segment_cannot_be_timed(self):
        shown = Q.caption_shown(self.frames(90), self.SEGS)
        self.assertEqual(shown, {0: 8 / 30, 1: None})
        self.assertIsNone(Q.caption_errors(self.SEGS, self.WORDS, self.AUDIO, shown)[1][2])

    def test_the_onset_failure_names_the_sentences_over_the_limit_worst_first(self):
        errs = [('a', .3, -.18), ('b', 1.5, .233), ('c', 2.4, .05), ('d', 3.1, None)]
        self.assertEqual(Q.onset_failure(errs, .15), 'caption onset worst 0.233s > 0.15s: b +0.233s, a -0.180s')
        self.assertIsNone(Q.onset_failure(errs[2:], .15))


class CaptionPresence(unittest.TestCase):
    """every caption element in layout.json must show on the render within its planned span"""
    SEGS = [{'id': 'hook', 'frame': 0, 'frames': 45}, {'id': 'breath', 'frame': 45, 'frames': 45}]
    LAYOUT = [{'id': 'w0-0', 'kind': 'caption', 'box': [100, 1000, 900, 1080], 't0': .2, 't1': .8},
              {'id': 'w0-1', 'kind': 'caption', 'box': [100, 1090, 900, 1170], 't0': .6, 't1': 1.4},
              {'id': 'w1-0', 'kind': 'caption', 'box': [100, 1000, 900, 1080], 't0': 1.6, 't1': 2.9},
              {'id': 'st0', 'kind': 'step', 'box': [300, 240, 780, 280], 't0': 0.0, 't1': 3.0}]

    def frames(self, drawn):
        """90 frames; drawn: {element id: (first frame, last frame)} of rendered caption ink at its planned box"""
        boxes = {e['id']: e['box'] for e in self.LAYOUT}
        return [at(i / 30, caption=[boxes[k][:2] + [boxes[k][2] - 40, boxes[k][3] - 10] for k, (a, b) in drawn.items() if a <= i <= b])
                for i in range(90)]

    def test_every_caption_shown_in_its_span_passes(self):
        got = Q.caption_presence(self.frames({'w0-0': (8, 23), 'w0-1': (20, 40), 'w1-0': (50, 85)}), self.LAYOUT, self.SEGS)
        self.assertEqual(got, {})

    def test_a_caption_never_shown_fails_naming_the_element_and_its_sentence(self):
        got = Q.caption_presence(self.frames({'w0-0': (8, 23), 'w1-0': (50, 85)}), self.LAYOUT, self.SEGS)
        self.assertEqual(got, {'w0-1': 'hook'})

    def test_a_caption_shown_only_outside_its_span_is_not_shown(self):
        got = Q.caption_presence(self.frames({'w0-0': (8, 23), 'w0-1': (60, 70), 'w1-0': (50, 85)}), self.LAYOUT, self.SEGS)
        self.assertEqual(got, {'w0-1': 'hook'})


class Openings(unittest.TestCase):
    """openings.json (render_packet.py) is copied into the QA report; a clean_from not found or not applied is printed"""
    NOTES = [{'shot': 1, 'clean_from': .75, 'note': 'soft-edged object over the opening settled at frame 18'},
             {'shot': 2, 'clean_from': None, 'note': 'clean opening: no soft-edged region over frame 0'},
             {'shot': 3, 'clean_from': .5, 'note': 'soft-edged object over the opening settled at frame 12'}]
    SEGS = [{'id': 'a', 'clip': 'c1', 'opening': 'unclean until 0.75s kept: the first word starts at 0.02s'},
            {'id': 'b', 'clip': 'c2'}, {'id': 'c', 'clip': 'c3'}, {'id': 'd', 'clip': 'c3'}]

    def test_the_notes_are_copied_and_shots_without_a_clean_from_applied_are_named(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'openings.json'), 'w', encoding='utf-8') as fh:
                json.dump(self.NOTES, fh)
            notes, lines = Q.openings_report(d, self.SEGS)
        self.assertEqual(notes, self.NOTES)
        self.assertEqual(lines, ['opening shot 1: clean_from 0.75s not applied (segment a: unclean until 0.75s kept: '
                                 'the first word starts at 0.02s)',
                                 'opening shot 2: no clean_from found (clean opening: no soft-edged region over frame 0)'])

    def test_a_project_without_openings_json_has_no_notes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Q.openings_report(d, self.SEGS), (None, []))


class FaceClearance(unittest.TestCase):
    """rendered caption ink against the face YuNet finds on the same rendered frame (sampled every 3rd frame)"""
    SEGS = [{'id': 'a', 'clip': 'c1', 'frame': 0, 'frames': 12}, {'id': 'b', 'clip': 'c2', 'frame': 12, 'frames': 12}]
    LAYOUT = [{'id': 'w0-0', 'kind': 'caption', 'box': [100, 1030, 900, 1100], 't0': -.2, 't1': .55},   # settled 0-.4
              {'id': 'w1-0', 'kind': 'caption', 'box': [100, 940, 900, 1000], 't0': .2, 't1': 1.0}]     # settled .4-.85

    def check(self, tops, faces, layout=None, halo=0):
        """tops: {frame: caption top} (a caption box 70px tall, its shadow `halo` px above), other frames show none"""
        frames = [at(i / 30, caption=[(100, tops[i], 900, tops[i] + 70)] if i in tops else [],
                     caption_halo=[(100, tops[i] - halo, 900, tops[i] + 70)] if i in tops else None) for i in range(24)]
        return Q.face_clearance(frames, self.SEGS, faces, layout or self.LAYOUT)

    def test_a_caption_drawn_over_the_face_fails(self):
        tops = {i: 1030 if i < 6 else 1005 for i in range(12)}             # the caption rises over the chin at frame 6
        bad, margin, _ = self.check(tops, {0: 1000, 3: 1010, 6: 1010, 9: 1000})
        self.assertEqual(bad, [('w0-0', 6 / 30, 1005, 1030)])
        self.assertEqual(margin, {'a': -5})

    def test_20px_under_the_face_passes_and_19px_fails(self):
        bad, margin, _ = self.check({i: 1030 for i in range(12)}, {0: 1010, 3: 1000, 6: 1010, 9: 1005})
        self.assertEqual((bad, margin), ([], {'a': 20}))
        bad, _, _ = self.check({i: 1029 for i in range(12)}, {0: 1010, 3: 1000, 6: 1010, 9: 1005})
        self.assertEqual(bad, [('w0-0', 0.0, 1029, 1030)])

    def test_a_frame_showing_a_caption_with_no_face_found_fails(self):
        faces = {0: 1000, 3: 1000, 6: None, 9: 1000, 12: None}             # frame 12 shows no caption: not checked
        bad, _, _ = self.check({i: 1030 for i in range(12)}, faces)
        self.assertEqual(bad, [('w0-0', 6 / 30, 1030, 'no face found on this frame')])

    def test_a_shot_with_no_face_anywhere_keeps_the_chest_band(self):
        faces = {0: 1000, 3: 1000, 6: 1000, 9: 1000, 12: None, 15: None, 18: None, 21: None}
        tops = {**{i: 1030 for i in range(12)}, **{i: 940 for i in range(12, 18)}, **{i: 930 for i in range(18, 24)}}
        bad, margin, _ = self.check(tops, faces)
        self.assertEqual(bad, [('w1-0', 18 / 30, 930, 940)])
        self.assertEqual(margin, {'a': 30})

    def test_a_caption_over_the_face_only_during_its_fade_in_passes_and_its_halo_is_recorded(self):
        layout = [{**self.LAYOUT[0], 't0': 0.0}, self.LAYOUT[1]]            # w0-0 fades in over 0-.2s: frames 0-5
        tops = {i: 990 if i < 6 else 1030 for i in range(12)}
        bad, margin, info = self.check(tops, {0: 1000, 3: 1010, 6: 1010, 9: 1000}, layout, halo=8)
        self.assertEqual((bad, margin), ([], {'a': 20}))
        self.assertEqual(info['w0-0'], {'halo_px': 8, 'spill_px': 40, 'spill_frames': 2})
        self.assertEqual(info['w1-0'], {'halo_px': 0, 'spill_px': 0, 'spill_frames': 0})

    def test_faces_are_found_on_every_third_rendered_frame_in_film_pixels(self):
        # a 1080x1920 film, a white 'face' y600-900 for frames 0-5, y700-1000 after; the stand-in detector returns the
        # white block's box in its 640x640 input (the film scaled by 1/3, padded right) with the landmarks at its top
        def detect(img):
            ys, xs = np.nonzero(img[..., 0] > 128)
            box = [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]
            return .9, box, [[box[0], box[1]]] * 5
        with tempfile.TemporaryDirectory() as d:
            film = os.path.join(d, 'film.mkv')
            subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=1080x1920:r=30:d=0.4',
                            '-vf', "drawbox=x=300:y=600:w=480:h=300:color=white:t=fill:enable='lt(n,6)',"
                                   "drawbox=x=300:y=700:w=480:h=300:color=white:t=fill:enable='gte(n,6)'",
                            '-c:v', 'ffv1', film], check=True)
            got = Q.film_faces(film, detect)
        self.assertEqual(sorted(got), [0, 3, 6, 9])
        for i, want in ((0, 900), (3, 900), (6, 1000), (9, 1000)):
            self.assertAlmostEqual(got[i], want, delta=3)


class Report(unittest.TestCase):
    """reel_check.main on a one-second synthetic project: the opening notes and caption presence reach qa-report.json"""

    def test_openings_and_a_caption_never_shown_reach_the_report_and_fail_the_run(self):
        from unittest import mock
        import io
        with tempfile.TemporaryDirectory() as d:
            film = os.path.join(d, 'assets', 'aroll.mp4')
            os.makedirs(os.path.dirname(film))
            subprocess.run([skillenv.tool('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=1080x1920:r=30:d=1',
                            '-f', 'lavfi', '-i', 'sine=f=300:d=1:sample_rate=48000', '-shortest', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-c:a', 'aac', film], check=True)
            layout = [{'id': 'w0-0', 'kind': 'caption', 'box': [100, 1000, 900, 1080], 't0': .1, 't1': .5},
                      {'id': 'w0-1', 'kind': 'caption', 'box': [100, 1090, 900, 1170], 't0': .5, 't1': 1.0}]
            notes = [{'shot': 1, 'clean_from': None, 'note': 'clean opening'}]
            files = {'segments.json': [{'id': 'a', 'clip': 'c1', 'frame': 0, 'frames': 30, 'src_in': 0.0, 'src_out': 1.0, 'word_idx': [0]}],
                     'sources.json': {'c1': film}, 'words_c1.json': [{'text': 'hi', 'start': .1, 'end': .4}], 'layout.json': layout,
                     'build-report.json': {'look': 'bold', 'platform': 'reels', 'warnings': []},
                     'reel.json': {'shots': [{'file': film, 'narration': 'Hi.'}]}, 'openings.json': notes}
            for name, data in files.items():
                with open(os.path.join(d, name), 'w', encoding='utf-8') as fh:
                    json.dump(data, fh)
            with open(os.path.join(d, 'index.html'), 'w', encoding='utf-8') as fh:
                fh.write('<body><div id="w0-0">hi</div></body>')
            frames = [at(i / 30, caption=[(120, 1010, 800, 1070)] if 3 <= i <= 15 else []) for i in range(30)]
            os.makedirs(os.path.join(d, 'work'))
            out = io.StringIO()
            with mock.patch.object(sys, 'argv', ['reel_check.py', d, '--render', film]), \
                    mock.patch.object(Q, 'overlay_frames', return_value=(frames, None)), \
                    mock.patch.object(Q, 'film_faces', return_value={}), mock.patch.object(sys, 'stdout', out), \
                    self.assertRaises(SystemExit) as cm:
                Q.main()
            with open(os.path.join(d, 'qa-report.json'), encoding='utf-8') as fh:
                rep = json.load(fh)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(rep['openings'], notes)
        self.assertIn('opening shot 1: no clean_from found (clean opening)', out.getvalue())
        self.assertEqual(rep['caption_presence'], {'captions': 2, 'never_shown': {'w0-1': 'a'}})
        self.assertIn("captions never shown on the render (element: sentence): {'w0-1': 'a'}", rep['failures'])


if __name__ == '__main__':
    unittest.main()
