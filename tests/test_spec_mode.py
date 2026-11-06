#!/usr/bin/env python3
"""Spec mode tests: EDL planner, reel builder, reel check. Run with the skill's Python (needs Pillow):
    PY tests/test_spec_mode.py
Synthetic inputs only: no media, no Whisper, no render.
"""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import build_reel as B  # noqa: E402
import edl_from_words as E  # noqa: E402
import faces as F  # noqa: E402
import reel_check as Q  # noqa: E402
import make_reel as M  # noqa: E402

NARR = ["Hello there, friend. 'Take five' and breathe slowly now.", 'Look up. Spot a grey, a bird.']


def said(text, t0=.5, step=.3, spoken=None):
    """Whisper-style words for a narration: one word every `step` seconds; `spoken` overrides the heard text"""
    toks = (spoken or text).split()
    return [{'text': w.strip("'"), 'start': round(t0 + k * step, 3), 'end': round(t0 + k * step + .25, 3)} for k, w in enumerate(toks)]


def spec(**over):
    s = {'slug': 't', 'shots': [{'file': 'a.mp4', 'narration': NARR[0], 'caption_top': 1000},
                                {'file': 'b.mp4', 'narration': NARR[1], 'caption_top': 1000}],
         'sentences': [{'id': 'hi', 'hold': 0}, {'id': 'relax', 'hold': .3}, {'id': 'up'}, {'id': 'notice', 'hold': .2}],
         'cards': {'steps': [{'from': 'hi', 'to': 'relax', 'text': 'one step'}],
                   'chips': [{'until': 'end', 'items': [{'sentence': 'notice', 'word': 'grey'}, {'sentence': 'notice', 'word': 'bird'}]}],
                   'message': {'from': 'notice', 'until': 'end', 'label': 'example', 'text': 'Got a minute today?', 'top': 790},
                   'end': {'lines': [['the end', 'sm']], 'hold': 1.2}}}
    s.update(over)
    return s


def inputs(sp=None):
    sp = sp or spec()
    words = {'c1': said(NARR[0]), 'c2': said(NARR[1], spoken=NARR[1].replace('grey', 'gray'))}
    sil = {'c1': [[0, .5], [4, 6]], 'c2': [[0, .5], [1.0, 1.4], [3.1, 6]]}
    edl = E.plan(sp, words, sil, {'c1': 6.0, 'c2': 6.0})
    fr, segs = 0, []
    for e in edl:
        n = round((e['out'] - e['in']) * 30)
        segs.append({**e, 'src_in': e['in'], 'src_out': e['out'], 'frame': fr, 'frames': n})
        fr += n
    return sp, words, sil, edl, segs, fr / 30 + sp['cards']['end']['hold']


class Planner(unittest.TestCase):
    def test_sentences_split_on_end_punctuation_and_keep_quotes(self):
        self.assertEqual(E.sentences("Hi. 'Take five' now! Why? end"),
                         [['Hi.'], ["'Take", "five'", 'now!'], ['Why?'], ['end']])
        self.assertEqual(E.sentences("He said 'stop.' Then left."), [['He', 'said', "'stop.'"], ['Then', 'left.']])

    def test_align_counts_spelling_variants_and_skips_missing_words(self):
        ws = [{'text': t} for t in ['a', 'color,', 'and', 'sound']]
        self.assertEqual(E.align(['a', 'colour,', 'and', 'a', 'sound.'], ws), [0, 1, 2, None, 3])

    def test_onset_fix_moves_clip_start_word_to_speech_onset(self):
        ws = [{'text': 'Look', 'start': 0.0, 'end': .7}, {'text': 'up', 'start': .8, 'end': 1.0}]
        out = E.onset_fix(ws, [[0, .46], [1.2, 2]])
        self.assertEqual([w['start'] for w in out], [.46, .8])
        self.assertEqual(E.onset_fix([{'text': 'x', 'start': .3, 'end': .6}], [[0, .46]])[0]['start'], .3)   # not a clip-start word

    def snap(self, speech, soft=(), noise=0, clean_from=None, hum=(), blips=()):
        import array
        import random
        sr = 16000
        rnd = random.Random(3)
        pcm = array.array('h', [int(rnd.uniform(-noise, noise)) for _ in range(sr * 3)])
        # hum: noise between sentences, under the speech line but above the room floor + 4dB; blips: 10-30ms of it just
        # over the line. soft: a weak /s/ or /h/ just above the room noise
        for amp, spans in ((40, hum), (80, blips), (5000, speech), (50, soft)):
            for a, b in spans:
                pcm[int(a * sr):int(b * sr)] = array.array('h', [amp if k % 9 < 4 else -amp for k in range(int(b * sr) - int(a * sr))])
        sp = {'shots': [{'file': 'a.mp4', 'narration': 'Hello there. Look up.'}]}
        if clean_from is not None:
            sp['shots'][0]['clean_from'] = clean_from
        ws = [{'text': 'Hello', 'start': .5, 'end': .7}, {'text': 'there.', 'start': .75, 'end': .9},   # Whisper: ends .1 early,
              {'text': 'Look', 'start': 1.3, 'end': 1.8}, {'text': 'up.', 'start': 1.85, 'end': 1.95}]  # starts .35 early
        return E.plan(sp, {'c1': ws}, {'c1': []}, {'c1': 3.0}, audio={'c1': (pcm.tobytes(), sr)})

    def test_cuts_land_in_the_pause_not_inside_a_word(self):
        a, b = self.snap([(.5, 1.0), (1.65, 2.2)])
        self.assertTrue(1.06 <= a['out'] <= 1.59 and 1.06 <= b['in'] <= 1.59, (a['out'], b['in']))
        self.assertGreaterEqual(b['out'], 2.26)

    def test_a_shots_first_sentence_keeps_its_first_word_despite_a_pause_inside_the_sentence(self):
        # a test run: a sentence with a comma pause was cut after the pause and lost its first word
        a, _ = self.snap([(.5, .85), (1.1, 1.3), (1.65, 2.2)])      # Hello there, | (pause .25s) ... | Look up.
        self.assertTrue(.38 <= a['in'] <= .46, a['in'])

    def test_a_soft_start_or_end_is_still_sound_not_pause(self):
        # a test run: a soft /s/ began 70ms before the level the planner called speech
        a, b = self.snap([(.5, 1.0), (1.65, 2.2)], soft=[(1.0, 1.08), (1.55, 1.65)], noise=30)
        self.assertGreaterEqual(a['out'], 1.08 + .05)
        self.assertLessEqual(b['in'], 1.55 - .05)

    def test_noise_between_sentences_is_cut_as_one_pause(self):
        # a test shot: 1.2s between two sentences held noise at the speech line with 10-30ms peaks just over it; the
        # pause broke into fragments, the planner joined the sentences seamlessly and kept the 1.2s of dead air
        blips = [(t, t + d) for t, d in ((1.08, .02), (1.17, .03), (1.26, .01), (1.33, .03), (1.42, .02), (1.51, .02))]
        a, b = self.snap([(.5, 1.0), (1.65, 2.2)], noise=30, hum=[(1.0, 1.65)], blips=blips)
        self.assertGreater(b['in'], a['out'])                     # a cut, not a seamless join
        self.assertLessEqual(a['out'], 1.0 + .15 + .12 + .01)      # sound end + soft-edge walk + 0.12
        self.assertGreaterEqual(b['in'], 1.65 - .15 - .1 - .01)

    def test_an_evenly_noisy_short_pause_is_still_cut(self):
        # review: a 0.40s pause filled with noise just under the line was cut before the soft-edge cap and joined after
        # it (each capped walk kept 0.15s of the noise as "soft sound", leaving too little to cut)
        a, b = self.snap([(.5, 1.0), (1.4, 2.2)], noise=30, hum=[(1.0, 1.4)])
        self.assertGreater(b['in'], a['out'])
        self.assertLessEqual(a['out'], 1.0 + .12 + .01)

    def test_a_soft_tail_up_to_015s_is_sound_and_a_longer_one_is_noise(self):
        # a real decay ("noon." fading out) measured 0.15s; walks of 0.18-0.30s ran through noise between sentences
        a, _ = self.snap([(.5, 1.0), (1.65, 2.2)], soft=[(1.0, 1.15)], noise=30)
        self.assertGreaterEqual(a['out'], 1.15 + .1)
        a, _ = self.snap([(.5, 1.0), (1.65, 2.2)], noise=30, hum=[(1.0, 1.18)])
        self.assertLessEqual(a['out'], 1.0 + .12 + .01)

    def test_only_short_quiet_runs_over_the_line_count_as_pause(self):
        # 10ms frames at -55dB, speech line -50dB: a run of 4 frames 4dB over it is noise, 5 frames or 7dB over is sound
        def gaps(run, over):
            return E.pauses([-55] * 20 + [-50 + over] * run + [-55] * 20, -50, 0, 1)
        self.assertEqual(gaps(4, 4), [(0, .44)])
        self.assertEqual(gaps(5, 4), [(0, .2), (.25, .45)])
        self.assertEqual(gaps(2, 7), [(0, .2), (.22, .42)])

    def test_a_shot_opening_on_unclean_frames_starts_at_its_first_clean_frame_when_no_speech_is_lost(self):
        # a test shot opened on a blurred foreground object (frames 0-17) that the cut kept
        a, _ = self.snap([(.5, 1.0), (1.65, 2.2)], clean_from=.45)
        self.assertEqual(a['in'], .45)
        self.assertNotIn('opening', a)
        a, b = self.snap([(.5, 1.0), (1.65, 2.2)], clean_from=1.4)        # a later sentence never moves
        self.assertTrue(.38 <= a['in'] <= .46 and b['in'] < 1.6, (a['in'], b['in']))

    def test_an_unclean_opening_stays_when_moving_the_in_point_would_cut_speech(self):
        a, _ = self.snap([(.5, 1.0), (1.65, 2.2)], clean_from=.48)        # the first word starts at .50
        self.assertTrue(.38 <= a['in'] <= .46, a['in'])
        self.assertRegex(a['opening'], r'unclean until 0\.48s kept: the first word starts at 0\.(49|5\d)s')

    def test_sentences_with_no_room_for_a_cut_join_seamlessly(self):
        a, b = self.snap([(.5, 1.0), (1.04, 1.6)])
        self.assertEqual(a['out'], b['in'])
        self.assertTrue(1.0 <= b['in'] <= 1.04, b['in'])

    def test_plan_cuts_one_segment_per_sentence_with_holds_and_caps(self):
        _, words, _, edl, _, _ = inputs()
        self.assertEqual([e['id'] for e in edl], ['hi', 'relax', 'up', 'notice'])
        self.assertEqual(edl[0]['in'], .44)                      # first word .5 - .06
        self.assertEqual(edl[0]['out'], round(words['c1'][3]['start'] - .08, 3))   # capped at the next word - .08
        self.assertEqual(edl[1]['out'], round(4 + .08 + .3, 3))  # silence start after the last word + .08 + hold
        self.assertEqual(edl[3]['out'], round(3.1 + .08 + .2, 3))
        self.assertEqual(edl[1]['tokens'][0], "'Take")
        self.assertIsNotNone(edl[3]['word_idx'][2])              # grey <- gray

    def test_plan_caps_out_at_the_shot_length(self):
        sp = spec()
        sp['sentences'][3]['hold'] = 5
        edl = E.plan(sp, {'c1': said(NARR[0]), 'c2': said(NARR[1])}, {'c1': [[4, 6]], 'c2': [[3.1, 6]]}, {'c1': 6.0, 'c2': 5.555})
        self.assertEqual(edl[3]['out'], 5.55)

    def test_plan_rejects_wrong_sentence_count_and_unmatched_narration(self):
        sp = spec(sentences=[{'id': 'a'}])
        with self.assertRaisesRegex(ValueError, 'lists 1 sentences, the narration has 4'):
            E.plan(sp, {'c1': said(NARR[0]), 'c2': said(NARR[1])}, {'c1': [], 'c2': []}, {'c1': 6, 'c2': 6})
        with self.assertRaisesRegex(ValueError, 'not found in what was said'):
            E.plan(spec(), {'c1': said('Something else entirely said here today.'), 'c2': said(NARR[1])},
                   {'c1': [], 'c2': []}, {'c1': 6, 'c2': 6})


class Builder(unittest.TestCase):
    def build(self, sp=None, look='bold', platform='reels', cut_words=None):
        sp, words, _, _, segs, dur = inputs(sp)
        return B.build(sp, B.load_look(look), platform, segs, words, cut_words or [], dur, sfx={'whoosh-short'}), segs

    def test_captions_say_the_narration_in_the_look_case(self):
        (page, layout, rep), _ = self.build()
        text = Q.screen_text(page)
        self.assertIn('take five and breathe slowly now', text)
        self.assertIn("'take five'", page)
        self.assertEqual([w for w in rep['warnings'] if not w.startswith('up: caption shown only')], [])   # 'up' is 0.57s
        self.assertTrue(all(B.inside(B.PLATFORMS['reels'], e['box']) for e in layout))

    def test_caption_times_prefer_the_cut_transcript_inside_the_segment(self):
        sp, words, _, _, segs, dur = inputs()
        T1 = segs[1]['frame'] / 30
        cut = [{'text': t, 'start': T1 + .37, 'end': T1 + .5} for t in ['Take']]
        page, _, _ = B.build(sp, B.load_look('bold'), 'reels', segs, words, cut, dur)
        self.assertAlmostEqual(Q.reveal_times(page)[1], T1 + .32, places=2)    # .37 - .05 lead
        far = [{'text': 'Take', 'start': T1 + 30, 'end': T1 + 31}]               # outside the segment: ignored
        page, _, _ = B.build(sp, B.load_look('bold'), 'reels', segs, words, far, dur)
        self.assertNotAlmostEqual(Q.reveal_times(page)[1], T1 + 29.95, places=1)

    def test_first_caption_of_a_sentence_follows_the_measured_speech_onset(self):
        # Whisper hangs a pause on the next word (a test run: word at 4.77, speech at 5.22): the onset wins
        sp, words, _, _, segs, dur = inputs()
        T1 = segs[1]['frame'] / 30
        cut = [{'text': 'Take', 'start': T1 + .10, 'end': T1 + .5}]
        onset_src = segs[1]['src_in'] + .60
        page, _, _ = B.build(sp, B.load_look('bold'), 'reels', segs, words, cut, dur, onsets={1: onset_src})
        self.assertAlmostEqual(Q.reveal_times(page)[1], T1 + .55, places=2)     # onset .60 - .05 lead
        page, _, _ = B.build(sp, B.load_look('bold'), 'reels', segs, words, cut, dur, onsets={})
        self.assertAlmostEqual(Q.reveal_times(page)[1], T1 + .05, places=2)     # no onset measured: the cut word

    def test_every_caption_chunk_is_fully_visible_long_enough_to_read(self):
        # a test run: a last word said just before the cut showed at full opacity for 0.095s. Later chunks of a
        # sentence come in early enough for 0.30s at full opacity; the first chunk stays on the speech onset.
        sp, words, _, _, segs, dur = inputs()
        page, _, rep = B.build(sp, B.load_look('bold'), 'reels', segs, words, [], dur)
        holds = {c: round(v, 3) for c, v in Q.chunk_holds(page, dur).items()}
        self.assertTrue(all(holds[c] >= .3 for c in ('w0-0', 'w0-1', 'w3-0', 'w3-1')), holds)
        self.assertIn('up: caption shown only', ' '.join(rep['warnings']))          # 0.57s sentence: too short to fix here

    def test_the_page_declares_every_font_it_uses_inline(self):
        # the HyperFrames compiler reads @font-face from the page only; fonts.css behind a <link> warned on every render
        (page, _, _), _ = self.build()
        used = set(re.findall(r"font-family:'([^']+)'", page.split('<body>')[0]))
        declared = set(re.findall(r"@font-face\{font-family:'([^']+)'[^}]*src:url\('assets/fonts/[^']+\.woff2'\)", page))
        self.assertTrue(used and used <= declared, (used, declared))

    def test_chips_pop_on_their_word(self):
        sp, words, _, _, segs, dur = inputs()
        page, _, _ = B.build(sp, B.load_look('bold'), 'reels', segs, words, [], dur)
        g = 3
        grey = segs[g]['frame'] / 30 + words['c2'][segs[g]['word_idx'][2]]['start'] - segs[g]['src_in']
        t = float(re.search(r"tl\.fromTo\('#ch0-0',.*?,([\d.]+)\);", page).group(1))
        self.assertAlmostEqual(t, grey - .03, places=2)

    def test_spec_mistakes_are_named(self):
        sp = spec()
        sp['sentences'][0]['chunks'] = [[2, 'sm']]
        with self.assertRaisesRegex(B.SpecError, 'sentence hi: chunks cover 2 words, it has 3'):
            self.build(sp)
        sp = spec()
        sp['cards']['chips'][0]['items'][0]['word'] = 'purple'
        with self.assertRaisesRegex(B.SpecError, '"purple" is not said in sentence notice'):
            self.build(sp)
        sp = spec()
        sp['cards']['steps'][0]['to'] = 'nope'
        with self.assertRaisesRegex(B.SpecError, 'no sentence "nope"'):
            self.build(sp)
        sp = spec()
        sp['cards']['end']['hold'] = .3
        with self.assertRaisesRegex(B.SpecError, 'end card only gets'):
            self.build(sp)

    def test_long_words_shrink_to_the_safe_width(self):
        st = B.style(B.load_look('bold'), 'big y')
        ln = B.line('incomprehensibilities', st, 1000, B.PLATFORMS['reels'], 860)
        self.assertLess(ln['fs'], st['size'])
        self.assertLessEqual(ln['box'][2] - ln['box'][0], 860)
        self.assertTrue(B.inside(B.PLATFORMS['reels'], ln['box']))

    def test_tight_bottom_falls_back_to_one_line_then_moves_up(self):
        z, look = B.PLATFORMS['tiktok'], B.load_look('bold')
        items = [('one two three', B.style(look, 'sm')), ('four', B.style(look, 'md y'))]
        self.assertEqual(B.block(items, 900, z, 860)[0], 'stack')
        self.assertEqual(B.block(items, 1100, z, 860)[0], 'replace')
        mode, lines, warn = B.block(items, 1200, z, 860)
        self.assertEqual(mode, 'replace')
        self.assertIn('moved up', warn)
        self.assertTrue(all(B.inside(z, ln['box']) for ln in lines))

    def test_descenders_count_against_the_safe_bottom(self):
        st = B.style(B.load_look('bold'), 'big y')
        flat, deep = B.line('to be', st, 1000, B.PLATFORMS['reels'], 860), B.line('to begin', st, 1000, B.PLATFORMS['reels'], 860)
        self.assertGreater(deep['box'][3], flat['box'][3])
        self.assertGreater(deep['box'][3], 1000 + deep['h'])

    def test_tiktok_button_column_pushes_text_left(self):
        ln = B.line('a wide caption line', B.style(B.load_look('bold'), 'md'), 1000, B.PLATFORMS['tiktok'], 860)
        self.assertLessEqual(ln['box'][2], 780)
        self.assertTrue(B.inside(B.PLATFORMS['tiktok'], ln['box']))

    def test_caption_above_the_top_safe_line_is_an_error(self):
        sp = spec()
        sp['shots'][0]['caption_top'] = 100
        with self.assertRaisesRegex(B.SpecError, 'outside the reels safe zone'):
            self.build(sp)

    def test_every_look_and_platform_builds(self):
        for look in sorted(f[:-5] for f in os.listdir(os.path.join(ROOT, 'looks'))):
            for platform in B.PLATFORMS:
                with self.subTest(look=look, platform=platform):
                    (page, layout, _), _ = self.build(look=look, platform=platform)
                    self.assertTrue(all(B.inside(B.PLATFORMS[platform], e['box']) for e in layout))
                    self.assertIn('window.__timelines["main"]', page)

    def test_look_packs_only_name_fonts_that_ship(self):
        for f in os.listdir(os.path.join(ROOT, 'looks')):
            look = B.load_look(f[:-5])
            for st in [*look['classes'].values(), {**look['label'], 'style': 'normal'}]:
                path = os.path.join(B.FONTS, f"{st['family'].replace(' ', '')}-{st['weight']}-{st['style']}.woff2")
                self.assertTrue(os.path.exists(path), path)

    def test_default_chunks_cover_every_word(self):
        for n in range(1, 15):
            self.assertEqual(sum(c for c, _ in B.default_chunks(n)), n)


class OneTake(unittest.TestCase):
    """Generated shots are one take each (Andy, 6 Nov: "jumpy ... weird zoom in out fast on the same angle"): a one_take
    spec never cuts the pause inside a shot and never zooms the picture on a cut."""

    def test_sentences_of_one_shot_join_seamlessly_and_keep_the_whole_take(self):
        _, _, _, cut, _, _ = inputs()
        _, _, _, edl, _, _ = inputs(spec(one_take=True))
        for e, nxt in zip(edl, edl[1:]):
            if e['clip'] == nxt['clip']:
                self.assertEqual(e['out'], nxt['in'])
        for clip in ('c1', 'c2'):
            take, was = [e for e in edl if e['clip'] == clip], [e for e in cut if e['clip'] == clip]
            self.assertEqual((take[0]['in'], take[-1]['out']), (was[0]['in'], was[-1]['out']))
            self.assertAlmostEqual(sum(e['out'] - e['in'] for e in take), take[-1]['out'] - take[0]['in'], places=3)

    def test_without_one_take_a_pause_inside_a_shot_is_still_cut(self):
        _, _, _, edl, _, _ = inputs()
        self.assertLess(edl[0]['out'], edl[1]['in'])

    def test_a_one_take_build_never_zooms_the_picture_on_a_cut(self):
        sp, words, _, _, segs, dur = inputs(spec(one_take=True))
        page, _, _ = B.build(sp, B.load_look('bold'), 'reels', segs, words, [], dur)
        self.assertNotIn("tl.fromTo('#stage'", page)
        sp, words, _, _, segs, dur = inputs()
        page, _, _ = B.build(sp, B.load_look('bold'), 'reels', segs, words, [], dur)
        self.assertIn("tl.fromTo('#stage'", page)


class AutoCaptionTop(unittest.TestCase):
    def test_captions_start_20px_plus_3px_render_headroom_under_the_face_bottom_but_not_above_the_chest_band(self):
        # a test run: captions sat exactly on two shots' face bottom (0px margin). A held-out episode's run: on
        # rendered frames the render check found face bottoms up to 3px below faces.py's lowest on the source frames
        self.assertEqual(F.caption_top(987, 'reels'), 1010)
        self.assertEqual(F.caption_top(1158, 'reels'), 1181)
        self.assertEqual(F.caption_top(854, 'reels'), 940)      # wide shot: chest band floor
        self.assertEqual(F.caption_top(None, 'reels'), 940)     # no face found

    def test_every_frame_is_sampled(self):
        self.assertEqual(F.sample_frames(10), list(range(10)))
        self.assertEqual(F.sample_frames(2), [0, 1])

    def test_a_one_frame_face_spike_sets_the_face_bottom(self):
        # a held-out episode, one shot: YuNet put the face bottom 33px lower for one frame the every-3rd-frame grid skipped; the
        # render check found it on a rendered frame and the caption planned under the sampled faces was 1px short
        import subprocess
        import tempfile
        from unittest import mock
        import numpy as np

        def detect(session, img):          # stand-in YuNet: the white block's box, landmarks at its top
            ys, xs = np.nonzero(img[..., 0] > 128)
            box = [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]
            return .9, box, [[box[0], box[1]]] * 5
        with tempfile.TemporaryDirectory() as d:
            shot = os.path.join(d, 'c1.mkv')   # 720x1280 24fps: face y400-600, y400-700 on frame 4 only (film y900, y1050)
            subprocess.run([F.skillenv.tool('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=720x1280:r=24:d=0.5',
                            '-vf', "drawbox=x=200:y=400:w=320:h=200:color=white:t=fill:enable='not(eq(n,4))',"
                                   "drawbox=x=200:y=400:w=320:h=300:color=white:t=fill:enable='eq(n,4)'",
                            '-c:v', 'ffv1', shot], check=True)
            with open(os.path.join(d, 'sources.json'), 'w', encoding='utf-8') as fh:
                json.dump({'c1': shot}, fh)
            with mock.patch.object(F, 'detect', detect), mock.patch.object(F, 'ensure_model', return_value='yunet.onnx'), \
                    mock.patch('onnxruntime.InferenceSession'), mock.patch.object(sys, 'argv', ['faces.py', d]), \
                    mock.patch('sys.stdout'):
                F.main()
            with open(os.path.join(d, 'faces.json'), encoding='utf-8') as fh:
                got = json.load(fh)['c1']
        self.assertAlmostEqual(got['face_bottom'], 1050, delta=3)
        self.assertEqual(got['sampled'], 12)

    def test_face_bottom_is_the_box_or_60px_under_the_landmark_chin_whichever_is_lower(self):
        kps = [[400, 500], [600, 500], [500, 600], [450, 700], [550, 700]]   # eyes, nose, mouth corners
        self.assertEqual(F.face_bottom([300, 300, 700, 987], kps), 987)     # box already below chin 800 + 60
        self.assertEqual(F.face_bottom([300, 300, 700, 780], kps), 860)     # short box: chin 700 + (700 - 600) + 60

    def test_caption_top_leaves_room_for_one_line_above_the_safe_bottom(self):
        self.assertEqual(F.caption_top(1400, 'tiktok'), 1174)   # 1260 floor - one 86px line
        self.assertEqual(F.caption_top(1400, 'reels'), 1384)    # 1470 - 86


    def test_builder_uses_the_measured_face_bottom_unless_the_spec_sets_caption_top(self):
        sp, words, _, _, segs, dur = inputs()
        del sp['shots'][0]['caption_top']
        page, _, rep = B.build(sp, B.load_look('bold'), 'reels', segs, words, [], dur,
                               faces={'c1': {'face_bottom': 1030}, 'c2': {'face_bottom': 500}})
        tops = {int(g): int(t) for g, t in re.findall(r'id="w(\d+)-0"[^>]*top:(\d+)px', page)}
        self.assertEqual(tops[0], 1053)                          # auto: face bottom 1030 + 20 + 3 headroom
        self.assertEqual(tops[2], 1000)                          # shot 2 keeps its explicit caption_top
        self.assertEqual(rep['caption_tops'], {'c1': [1053, 'auto'], 'c2': [1000, 'spec']})


    def test_a_model_that_does_not_match_the_pinned_hash_is_never_saved(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, 'sha256'):
                F.ensure_model(d, download=lambda url: b'not the model')
            self.assertEqual(os.listdir(d), [])


class Check(unittest.TestCase):
    def test_caption_error_is_shown_time_minus_measured_onset_in_cut_time(self):
        import array
        sr = 16000
        pcm = array.array('h', [0] * sr * 4)
        for a, b in ((.06, .6), (2.2, 2.6)):        # the film's speech stem: speech at .06 and 2.2 (Whisper: .1 and 2.1)
            pcm[int(a * sr):int(b * sr)] = array.array('h', [6000 if k % 7 < 3 else -6000 for k in range(int(b * sr) - int(a * sr))])
        segs = [{'id': 'a', 'clip': 'c1', 'frame': 0, 'src_in': .4, 'word_idx': [0]},
                {'id': 'b', 'clip': 'c1', 'frame': 60, 'src_in': 3.0, 'word_idx': [1]}]
        words = {'c1': [{'start': .5}, {'start': 3.1}]}
        page = "tl.fromTo('#w0-0',{},{},0.050);tl.fromTo('#w1-0',{},{},2.130);"
        errs = Q.caption_errors(segs, words, (pcm.tobytes(), sr), Q.reveal_times(page))
        self.assertEqual([round(e, 3) for _, _, e in errs], [-.01, -.07])

    def test_cut_frames_within_one_frame(self):
        segs = [{'frame': 0}, {'frame': 84}, {'frame': 182}]
        self.assertIsNone(Q.cut_mismatch(segs, [83, 183]))
        self.assertIn('off by more than 1', Q.cut_mismatch(segs, [80, 183]))
        self.assertIn('2 cuts, expected 1', Q.cut_mismatch(segs, [83]))

    def test_text_check_ignores_case_quotes_and_markup(self):
        page = "<body><div>'take</div><div>five' now</div><span>Got a minute?</span><script>x</script>"
        self.assertEqual(Q.missing_text(page, ["'Take five' now", 'GOT A MINUTE', 'not here']), ['not here'])


# Real-voice onset labels are private (a creator's voice is not repo content). Point VIDEO_EDIT_ONSET_FIXTURES at a
# folder with cN.wav + labels.json ({"sentences": [{sentence, clip, onset, whisper_start, src_in}]}) to run them.
FIX = os.environ.get('VIDEO_EDIT_ONSET_FIXTURES', '')
HAVE_FIX = bool(FIX) and os.path.exists(os.path.join(FIX, 'labels.json'))


def synthetic(onset, sr=16000, dur=2.0, quiet_db=-50, speech_db=-15):
    """room noise, then a speech-like burst from `onset`: 16-bit mono PCM bytes"""
    import array
    import random
    rnd = random.Random(7)
    out = array.array('h')
    for i in range(int(dur * sr)):
        t = i / sr
        amp = 32767 * 10 ** ((speech_db if t >= onset else quiet_db) / 20)
        out.append(int(max(-32767, min(32767, rnd.gauss(0, amp / 2)))))
    return out.tobytes(), sr


def fixture():
    import wave
    with open(os.path.join(FIX, 'labels.json'), encoding='utf-8') as fh:
        rows = json.load(fh)['sentences']
    audio = {}
    for r in rows:
        if r['clip'] not in audio:
            with wave.open(os.path.join(FIX, f"{r['clip']}.wav")) as w:
                audio[r['clip']] = (w.readframes(w.getnframes()), w.getframerate())
    return rows, audio


class SyntheticOnsets(unittest.TestCase):
    def test_reference_finds_speech_after_a_quiet_room_even_when_whisper_is_early(self):
        pcm, sr = synthetic(1.0)
        self.assertAlmostEqual(Q.speech_onset(pcm, sr, .6), 1.0, delta=.04)

    def test_placer_finds_the_speech_start_whisper_missed(self):
        pcm, sr = synthetic(1.0)
        self.assertAlmostEqual(B.speech_start(pcm, sr, .5, .6), 1.0, delta=.07)


class HeldOutFailures(unittest.TestCase):
    """Three QA failures on a 4-shot episode the onset finders were not tuned on"""

    def test_placer_ignores_digital_silence_when_finding_the_room_floor(self):
        # a shot opened on digital silence (-95dB) to 0.28s, room tone (-68dB) to 0.44s, speech from 0.44s: the placer
        # took the silence as its floor and called the room tone speech (0.34s, 117ms early)
        import array
        import random
        rnd, sr, out = random.Random(3), 16000, array.array('h')
        for i in range(int(2.0 * sr)):
            t = i / sr
            db = None if t < .28 else -68 if t < .44 else -18
            out.append(0 if db is None else int(max(-32767, min(32767, rnd.gauss(0, 32767 * 10 ** (db / 20) / 2)))))
        self.assertAlmostEqual(B.speech_start(out.tobytes(), sr, .2, .457), .44, delta=.03)

    def test_planner_finds_room_tone_pauses_in_a_shot_with_digital_silence(self):
        # the speech line sat 10dB above digital silence (-95dB), so room tone (-68dB) counted as sound: no pause between
        # two sentences, no cut, and the 1.2s of dead air between them stayed in the reel
        import array
        import random
        rnd, sr, out = random.Random(4), 16000, array.array('h')
        for i in range(int(3.0 * sr)):
            t = i / sr
            db = None if t < .3 else -18 if t < 1.0 else -68 if t < 2.0 else -18
            out.append(0 if db is None else int(max(-32767, min(32767, rnd.gauss(0, 32767 * 10 ** (db / 20) / 2)))))
        db, line = E.levels(out.tobytes(), sr)
        gaps = E.pauses(db, line, .9, 2.1)
        self.assertTrue(any(p0 <= 1.05 and p1 >= 1.95 for p0, p1 in gaps), (round(line, 1), gaps))

    def test_a_stack_a_few_px_too_tall_shrinks_instead_of_going_one_line_at_a_time(self):
        # 'notice' (big) over 'what you're feeling.' (sm) at y1178 overflowed the Reels floor by 8px: one line at a
        # time made the first chunk flash for 0.09s
        look, z = B.load_look('bold'), B.PLATFORMS['reels']
        items = [('notice', B.style(look, 'big y')), ("what you're feeling.", B.style(look, 'sm'))]
        mode, lines, warn = B.block(items, 1178, z, 860)
        self.assertEqual(mode, 'stack')
        self.assertIsNone(warn)
        self.assertLess(lines[0]['fs'], B.style(look, 'big y')['size'])
        self.assertTrue(all(B.inside(z, ln['box']) for ln in lines))
        self.assertLessEqual(lines[0]['box'][3], lines[1]['box'][1] + 40)      # still one block, top to bottom
        self.assertEqual(B.block(items, 1300, z, 860)[0], 'replace')           # far too tall: shrinking stops at 85%

    def test_one_line_at_a_time_waits_until_the_line_before_has_been_readable(self):
        # 'notice' then 'what you're feeling.' 0.3s later, one line at a time: the second replaced the first at once
        sp = spec()
        sp['shots'][0]['caption_top'] = 1330                                     # too low to stack: one line at a time
        sp['sentences'][1]['chunks'] = [[1, 'big y'], [5, 'sm']]
        sp, words, _, _, segs, dur = inputs(sp)
        page, _, rep = B.build(sp, B.load_look('bold'), 'reels', segs, words, [], dur)
        self.assertEqual(rep['caption_modes']['relax'], 'replace')
        holds = Q.chunk_holds(page, dur)
        self.assertGreaterEqual(round(holds['w1-0'], 3), B.MIN_HOLD, holds)
        self.assertNotIn('relax: caption shown only', ' '.join(rep['warnings']))

    def test_picture_is_compared_above_the_captions_with_only_the_cards_masked(self):
        # the band stopped at the highest box on screen (a step label at y236), leaving only hair and wall: unmeasurable
        layout = [{'id': 'st0', 'kind': 'step', 'box': [100, 236, 980, 277]},
                  {'id': 'ch0', 'kind': 'chips', 'box': [300, 300, 780, 364]},
                  {'id': 'w0-0', 'kind': 'caption', 'box': [200, 1000, 880, 1140]},
                  {'id': 'w1-0', 'kind': 'caption', 'box': [200, 1158, 880, 1300]}]
        self.assertEqual(Q.picture_region(layout), (1000, [[100, 236, 980, 277], [300, 300, 780, 364]]))


@unittest.skipUnless(HAVE_FIX, 'private real-voice onset labels: set VIDEO_EDIT_ONSET_FIXTURES')
class OnsetReference(unittest.TestCase):
    """reel_check's speech onset vs independent labels on real generated voices (not produced by any skill code)"""

    def test_reference_matches_the_independent_labels_within_40ms(self):
        rows, audio = fixture()
        off = {}
        for r in rows:
            pcm, sr = audio[r['clip']]
            got = Q.speech_onset(pcm, sr, r['whisper_start'])
            if abs(got - r['onset']) > .04:
                off[r['sentence']] = (round(got, 3), r['onset'])
        self.assertEqual(off, {})


@unittest.skipUnless(HAVE_FIX, 'private real-voice onset labels: set VIDEO_EDIT_ONSET_FIXTURES')
class CaptionPlacement(unittest.TestCase):
    """build_reel's own onset finder (not reel_check's) on early reveals in the labelled set: s2, s6, s12"""

    def test_placer_finds_the_speech_start_whisper_missed(self):
        rows, audio = fixture()
        for sid in ('s2', 's6', 's12'):
            r = next(x for x in rows if x['sentence'] == sid)
            got = B.speech_start(*audio[r['clip']], r['src_in'], r['whisper_start'])
            self.assertAlmostEqual(got, max(r['onset'], r['src_in']), delta=.07, msg=sid)


class FilmChecks(unittest.TestCase):
    """reel_check measures the film itself: sync, cuts, holds (the gaps a test run's verifier found)"""

    def test_full_opacity_time_of_every_caption_chunk(self):
        page = ("tl.fromTo('#w0-1',{autoAlpha:0},{autoAlpha:1,duration:0.2,ease:'x'},1.850);"
                "tl.to('#w0-1',{autoAlpha:0,duration:0.12},2.145);"
                "tl.fromTo('#w1-0',{autoAlpha:0},{autoAlpha:1,duration:0.2},3.000);")
        self.assertEqual({k: round(v, 3) for k, v in Q.chunk_holds(page, end=5.0).items()}, {'w0-1': .095, 'w1-0': 1.8})

    def test_speech_running_through_a_cut_is_found(self):
        import array
        sr = 16000
        pcm = array.array('h', [0] * sr * 3)
        loud = lambda a, b: array.array('h', [5000 if k % 9 < 4 else -5000 for k in range(int(b * sr) - int(a * sr))])
        pcm[int(.3 * sr):int(1.2 * sr)] = loud(.3, 1.2)          # a word straddles the cut at 1.0
        pcm[int(1.6 * sr):int(2.4 * sr)] = loud(1.6, 2.4)        # the cut at 1.45 sits in a pause
        self.assertEqual(Q.cuts_in_speech(pcm.tobytes(), sr, [(1.0, True, True), (1.45, True, True)]), [1.0])
        self.assertEqual(Q.cuts_in_speech(pcm.tobytes(), sr, [(1.0, True, False)]), [1.0])

    def test_film_onset_ignores_the_previous_sentence_across_the_cut(self):
        import array
        sr = 16000
        pcm = array.array('h', [0] * sr * 3)
        loud = lambda a, b: array.array('h', [5000 if k % 9 < 4 else -5000 for k in range(int(b * sr) - int(a * sr))])
        pcm[int(.5 * sr):int(1.0 * sr)] = loud(.5, 1.0)          # previous sentence runs right up to the cut at 1.0
        pcm[int(1.05 * sr):int(1.6 * sr)] = loud(1.05, 1.6)      # this sentence starts 50ms after the cut
        self.assertAlmostEqual(Q.speech_onset(pcm.tobytes(), sr, .95, not_before=1.0), 1.05, delta=.01)

    def test_a_cut_at_a_shots_first_frame_cuts_nothing_away(self):
        segs = [{'clip': 'c1', 'frame': 0, 'src_in': .4, 'src_out': 1.4}, {'clip': 'c2', 'frame': 30, 'src_in': 0.0, 'src_out': 1.0}]
        self.assertEqual(Q.jump_cuts(segs, {'c1': 1.4, 'c2': 4.0}), [(1.0, False, False)])

    def test_seamless_joins_are_not_cuts(self):
        segs = [{'clip': 'c1', 'frame': 0, 'src_in': .0, 'src_out': 1.0}, {'clip': 'c1', 'frame': 30, 'src_in': 1.0, 'src_out': 2.0},
                {'clip': 'c2', 'frame': 60, 'src_in': .5, 'src_out': 1.5}, {'clip': 'c2', 'frame': 90, 'src_in': 1.6, 'src_out': 2.0}]
        self.assertEqual(Q.jump_cuts(segs, {'c1': 4.0, 'c2': 4.0}), [(2.0, True, True), (3.0, True, True)])

    def test_offset_of_a_shifted_signal_and_of_shifted_frames(self):
        import numpy as np
        rng = np.random.default_rng(1)
        src = rng.standard_normal(16000)
        self.assertAlmostEqual(Q.xcorr_offset(src[3200:6400], src, 16000), .2, places=3)


def pre_emphasised(pieces, seed=0, sr=16000):
    """[(until s, dB)]: white noise at that level as reel_check's speech_onset sees it (after its pre-emphasis), so the
    levels below are its own frame levels: 16-bit mono PCM bytes"""
    import array
    import random
    rnd, out, prev = random.Random(seed), array.array('h'), 0.0
    for until, db in pieces:
        amp = 32767 * 10 ** (db / 20)
        while len(out) < int(until * sr):
            prev = rnd.gauss(0, amp) + .95 * prev
            out.append(int(max(-32767, min(32767, prev))))
    return out.tobytes(), sr


class FilmFalseFailures(unittest.TestCase):
    """Episode 1ecda41a (render eecb53ee, 2026-11-05) failed QA with captions that were on time: the film onset skipped a
    sentence's first word and measured a later one (s2 -0.647s, s11 -0.698s), and a chunk held exactly 0.300s failed
    as shorter than 0.30s (w0-1). Levels are speech_onset's 5ms pre-emphasised frame levels measured on that film."""

    def test_a_breath_before_the_first_word_does_not_push_the_onset_to_a_later_word(self):
        # s2 "Start with your feet": cut 1.167s; a 70ms breath at floor+20dB (film: -36..-41, floor -56), 45ms quiet,
        # "Start" at 1.285s. The onset wanted 120ms at breath level (floor+15) before it, so it skipped "Start" and
        # "with" and took "your" (1.880s in the film: caption error -0.647s)
        pcm, sr = pre_emphasised([(.90, -30), (35 / 30, -65), (1.24, -45), (1.285, -60), (1.42, -28), (1.50, -45),
                                  (1.72, -30), (1.875, -60), (3.0, -28)])
        self.assertAlmostEqual(Q.speech_onset(pcm, sr, 1.107, not_before=35 / 30), 1.285, delta=.01)

    def test_room_tone_on_the_breath_line_does_not_push_the_onset_to_a_later_word(self):
        # s11 "Feet, three slow breaths out": cut 26.1s (here 1.1s); room tone sitting on the breath line (film: -52..-57
        # against a -54.1 line, 4 of 24 frames over it) up to "Feet," at 26.365s; the onset took "three" (27.065s:
        # caption error -0.698s)
        room = [p for k in range(12) for p in ((1.135 + .02 * k, -58), (1.14 + .02 * k, -52))]
        pcm, sr = pre_emphasised([(.95, -26), (1.08, -55), (1.12, -78), *room, (1.365, -58), (1.57, -31), (2.05, -70),
                                  (3.0, -38)])
        self.assertAlmostEqual(Q.speech_onset(pcm, sr, 1.36, not_before=1.1), 1.365, delta=.01)

    def test_a_chunk_held_exactly_the_minimum_is_not_short(self):
        # w0-1 'do?': the planner holds a sentence's last chunk exactly MIN_HOLD (0.544667s in, gone at 1.044667s); the
        # page writes milliseconds (0.545, 1.045) and 1.045 - 0.545 - 0.2 is 0.2999999999999999 in floats
        page = ("tl.fromTo('#w0-1',{autoAlpha:0,scale:1.22},{autoAlpha:1,scale:1,duration:0.2,ease:'power3.out',immediateRender:false},0.545);"
                "tl.to('#w0-1',{autoAlpha:0,scale:.96,filter:'blur(16px)',duration:0.12},1.045);"
                "tl.fromTo('#w0-2',{autoAlpha:0},{autoAlpha:1,duration:0.2},2.000);"
                "tl.to('#w0-2',{autoAlpha:0,duration:0.12},2.299);")
        self.assertEqual(Q.short_holds(page, 29.4), {'w0-2': .099})


class Assemble(unittest.TestCase):
    """assemble.py on synthetic 24fps shots, segment lengths off the 30fps grid (a test run drifted +0.209s by s17)"""

    def test_sound_stays_on_the_picture_grid_and_under_the_peak_limit(self):
        import subprocess
        import tempfile
        ff = B.skillenv.tool('ffmpeg')
        with tempfile.TemporaryDirectory() as d:
            src = {}
            for c in ('c1', 'c2'):
                f = os.path.join(d, f'{c}.mp4')
                subprocess.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=360x640:r=24:d=4', '-f', 'lavfi',
                                '-i', 'sine=f=440:r=48000:d=4,volume=0.99', '-shortest', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                                '-c:a', 'aac', f], check=True)
                src[c] = f
            edl = [{'id': f's{k}', 'clip': c, 'in': a, 'out': b} for k, (c, a, b) in
                   enumerate([('c1', .70, 2.94), ('c1', 2.96, 4.0), ('c2', 2.34, 3.76), ('c2', .516, 3.252), ('c1', .1, 1.238)])]
            for name, data in (('sources.json', src), ('edl.json', edl)):
                with open(os.path.join(d, name), 'w') as fh:
                    json.dump(data, fh)
            subprocess.run([sys.executable, os.path.join(os.path.dirname(B.__file__), 'assemble.py'), d], check=True, capture_output=True)
            with open(os.path.join(d, 'segments.json')) as fh:
                segs = json.load(fh)
            aroll = os.path.join(d, 'assets', 'aroll.mp4')
            probe = lambda sel: float(subprocess.run([B.skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', sel, '-show_entries',
                                                      'stream=duration', '-of', 'csv=p=0', aroll], capture_output=True, text=True).stdout)
            frames = sum(s['frames'] for s in segs)
            self.assertAlmostEqual(probe('v:0'), frames / 30, delta=.001)
            pcm = subprocess.run([ff, '-v', 'error', '-i', aroll, '-vn', '-ac', '1', '-ar', '48000', '-f', 's16le', '-'], capture_output=True).stdout
            self.assertAlmostEqual(len(pcm) / 2 / 48000, frames / 30, delta=.025)   # one AAC frame of padding at most
            for s in segs:
                self.assertAlmostEqual(s['src_out'] - s['src_in'], s['frames'] / 30, delta=1e-6, msg=s['id'])
            # a test run: a 24fps in point between source frames made fps=30 start the picture at frame 1 (start_time
            # 0.033008) while the sound started at 0; concat then ran the picture one frame ahead on 11 of 17 segments
            for k in range(len(segs)):
                starts = subprocess.run([B.skillenv.tool('ffprobe'), '-v', 'error', '-show_entries', 'stream=codec_type,start_time', '-of',
                                         'csv=p=0', os.path.join(d, 'work', f'seg_{k}.mov')], capture_output=True, text=True).stdout.split()
                self.assertEqual(sorted(starts), ['audio,0.000000', 'video,0.000000'], f'seg_{k}')
            # every jump cut fades the sound out over its last 30ms (b76facf commented the fade-out away by accident)
            seg0 = subprocess.run([ff, '-v', 'error', '-i', os.path.join(d, 'work', 'seg_0.mov'), '-vn', '-ac', '1', '-ar', '48000', '-f',
                                   's16le', '-'], capture_output=True).stdout
            import array
            whole, tail = array.array('h', seg0), array.array('h', seg0[-2 * 48:])    # the segment, its last 1ms (fade ends at 0)
            self.assertLess(max(abs(v) for v in tail), .1 * max(abs(v) for v in whole))
            peak = subprocess.run([ff, '-hide_banner', '-i', aroll, '-af', 'volumedetect', '-f', 'null', '-'], capture_output=True, text=True).stderr
            self.assertLessEqual(float(re.search(r'max_volume: (-?[\d.]+) dB', peak).group(1)), -2.5)


class RenderWarnings(unittest.TestCase):
    def test_compile_warnings_reach_the_build_report(self):
        import tempfile
        out = ('[render] frame 900/1068\n'
               '[WARN] [compile] Audio "bga" (assets/aroll.mp4) is 35.39s but its data-duration is 35.57s — the slot is shortened\n'
               'done\n')
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'build-report.json'), 'w') as fh:
                json.dump({'warnings': ['s3: moved up'], 'cuts': []}, fh)
            M.note_render_warnings(d, out)
            with open(os.path.join(d, 'build-report.json')) as fh:
                rep = json.load(fh)
            with open(os.path.join(d, 'work', 'render.log')) as fh:
                log = fh.read()
        self.assertEqual(len(rep['warnings']), 2)
        self.assertIn('data-duration is 35.57s', rep['warnings'][1])
        self.assertEqual(log, out)


class ShotEnd(unittest.TestCase):
    def test_a_segment_ending_at_the_shot_end_keeps_the_sound_to_its_end(self):
        # a test shot: 97 frames at 24fps (4.042s) of picture, 4.064s of sound; the last word decays until 4.03s
        import subprocess
        import tempfile
        ff = B.skillenv.tool('ffmpeg')
        with tempfile.TemporaryDirectory() as d:
            f, v, a = (os.path.join(d, n) for n in ('c1.mp4', 'v.mp4', 'a.m4a'))
            subprocess.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=360x640:r=24:d=5', '-frames:v', '97', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', v], check=True)
            subprocess.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'sine=f=300:r=48000:d=4.064,volume=0.5', '-c:a', 'aac', a], check=True)
            subprocess.run([ff, '-v', 'error', '-i', v, '-i', a, '-c', 'copy', f], check=True)
            for name, data in (('sources.json', {'c1': f}), ('edl.json', [{'id': 's1', 'clip': 'c1', 'in': 2.46, 'out': 4.064}])):
                with open(os.path.join(d, name), 'w') as fh:
                    json.dump(data, fh)
            subprocess.run([sys.executable, os.path.join(os.path.dirname(B.__file__), 'assemble.py'), d], check=True, capture_output=True)
            with open(os.path.join(d, 'segments.json')) as fh:
                seg = json.load(fh)[0]
        self.assertGreaterEqual(seg['src_out'], 4.04)
        self.assertAlmostEqual(seg['src_out'] - seg['src_in'], seg['frames'] / 30, delta=1e-6)

    def test_the_planner_may_end_a_sentence_at_the_end_of_the_shots_sound(self):
        import array
        sr = 16000
        pcm = array.array('h', [0] * int(sr * 4.064))
        pcm[int(2.57 * sr):int(4.03 * sr)] = array.array('h', [5000 if k % 9 < 4 else -5000 for k in range(int(4.03 * sr) - int(2.57 * sr))])
        sp = {'shots': [{'file': 'a.mp4', 'narration': 'How do you get back?'}]}
        ws = [{'text': 'How', 'start': 2.4, 'end': 2.7}, {'text': 'do', 'start': 2.7, 'end': 2.8}, {'text': 'you', 'start': 2.8, 'end': 3.0},
              {'text': 'get', 'start': 3.0, 'end': 3.3}, {'text': 'back?', 'start': 3.3, 'end': 3.9}]
        edl = E.plan(sp, {'c1': ws}, {'c1': []}, {'c1': 4.0417}, audio={'c1': (pcm.tobytes(), sr)})
        self.assertGreaterEqual(edl[0]['out'], 4.06)


class Provenance(unittest.TestCase):
    """An edit proves which approved script and which produced shots it cuts (a receipts-only verifier found "no creative
    approval" when the spec and the QA receipt carried nothing from the approval record). Optional unless required."""

    def packet(self, d):
        import hashlib
        files = []
        for k, text in enumerate(['Busy day, but no time to stop?', 'How do you get back to the moment?']):
            f = os.path.join(d, f'film-{k + 1}.mp4')
            with open(f, 'wb') as fh:
                fh.write(f'shot {k + 1}'.encode())
            files.append((f, hashlib.sha256(f'shot {k + 1}'.encode()).hexdigest(), text))
        shots = [{'operationId': f'film:{k + 1}', 'sha256': h, 'narration': t} for k, (_, h, t) in enumerate(files)]
        prov = {'source': 'producer', 'episodeId': '00000000-0000-4000-8000-000000000001', 'productionId': '00000000-0000-4000-8000-000000000002',
                'packetSha256': 'a' * 64, 'scriptSha256': 'b' * 64, 'creativeReview': {'status': 'passed', 'inputSha256': 'c' * 64},
                'approval': {'approvedAt': '2026-11-02T10:47:00Z'}, 'shots': shots}
        spec = {'shots': [{'file': f, 'narration': t} for f, _, t in files], 'provenance': prov}
        return spec

    def test_a_spec_without_provenance_passes_unless_provenance_is_required(self):
        self.assertEqual(Q.provenance_problems({'shots': []}), [])
        self.assertEqual(Q.provenance_problems({'shots': []}, require=True),
                         ['no provenance: the spec does not say which approved episode it cuts'])

    def test_provenance_must_show_a_passed_review_and_match_every_shot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            spec = self.packet(d)
            self.assertEqual(Q.provenance_problems(spec), [])
            bad = json.loads(json.dumps(spec)); bad['provenance']['creativeReview']['status'] = 'failed'
            self.assertIn('creative review is failed, not passed', Q.provenance_problems(bad))
            bad = json.loads(json.dumps(spec)); bad['shots'][1]['narration'] = 'How do you get back?'
            self.assertIn('shot 2 narration differs from the approved script', Q.provenance_problems(bad))
            with open(spec['shots'][0]['file'], 'ab') as fh:
                fh.write(b'!')
            self.assertIn('shot 1 file is not the produced shot (sha256 differs)', Q.provenance_problems(spec))
            src = {'c1': spec['shots'][0]['file'], 'c2': spec['shots'][0]['file']}     # what ingest cut: c2 is the wrong file
            self.assertIn('shot 2 cut file (sources.json c2) is not the produced shot', Q.provenance_problems(spec, src))
            bad = json.loads(json.dumps(spec)); bad['shots'].pop()
            self.assertIn('2 produced shots, the spec cuts 1', Q.provenance_problems(bad))


class PictureSync(unittest.TestCase):
    """reel_check.picture_sync on assemble.py's own output: render frame k of a segment must show the source frame on
    screen at in + k/30 (ffmpeg puts each 24fps frame on its nearest 30fps slot) to under half a frame, matched by
    pixels; a segment whose frames cannot be told apart fails (a test run passed 8 of 17 as 'too static to measure')"""

    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        import numpy as np
        cls.tmp = tempfile.TemporaryDirectory()
        d, ff = cls.tmp.name, B.skillenv.tool('ffmpeg')

        def shot(name, frames):
            f = os.path.join(d, name)
            subprocess.run([ff, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '90x160', '-r', '24', '-i', '-',
                            '-f', 'lavfi', '-i', 'sine=f=440:r=48000:d=4', '-shortest', '-c:v', 'libx264', '-crf', '10',
                            '-pix_fmt', 'yuv420p', '-c:a', 'aac', f], input=frames.tobytes(), check=True)
            return f
        rng = np.random.default_rng(5)
        tex = np.kron(rng.integers(0, 256, (96, 16, 9), dtype=np.uint8), np.ones((1, 10, 10), np.uint8))   # every frame its own
        cls.src = {'c1': shot('c1.mp4', tex), 'c2': shot('c2.mp4', np.repeat(tex[:1], 96, 0))}            # c2: one still frame
        edl = [{'id': f's{k}', 'clip': c, 'in': a, 'out': b} for k, (c, a, b) in
               enumerate([('c1', .70, 2.0), ('c1', .516, 1.9), ('c1', 1.238, 2.6), ('c2', .3, 1.6)])]
        for name, data in (('sources.json', cls.src), ('edl.json', edl)):
            with open(os.path.join(d, name), 'w') as fh:
                json.dump(data, fh)
        subprocess.run([sys.executable, os.path.join(os.path.dirname(B.__file__), 'assemble.py'), d], check=True, capture_output=True)
        with open(os.path.join(d, 'segments.json')) as fh:
            cls.segs = json.load(fh)
        cls.aroll, cls.ahead = os.path.join(d, 'assets', 'aroll.mp4'), os.path.join(d, 'ahead.mp4')
        subprocess.run([ff, '-v', 'error', '-y', '-i', cls.aroll, '-vf', 'trim=start_frame=1,setpts=PTS-STARTPTS', '-c:v', 'libx264',
                        '-crf', '10', '-c:a', 'aac', cls.ahead], check=True)          # the picture one frame ahead of its sound

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_limit_is_under_half_a_frame(self):
        self.assertLess(Q.PIC_LIMIT, .5 / 30)

    def test_assembled_picture_sits_on_its_sound_to_under_half_a_frame(self):
        got = Q.picture_sync(self.aroll, self.segs[:3], self.src, 1920)
        for sid, r in got.items():
            self.assertIsNone(r['reason'], (sid, r))
            self.assertLess(max(abs(r['lo']), abs(r['hi'])), Q.PIC_LIMIT, (sid, r))
            self.assertGreaterEqual(r['matched'], Q.PIC_MIN_MATCHED)

    def test_a_picture_one_frame_ahead_fails(self):
        got = Q.picture_sync(self.ahead, self.segs[:3], self.src, 1920)
        for sid, r in got.items():
            self.assertRegex(r['reason'] or '', 'picture off', (sid, r))

    def test_cards_over_the_picture_are_left_out_of_the_match(self):
        got = Q.picture_sync(self.aroll, self.segs[:1], self.src, 1920, covered=[[0, 0, 1080, 600]])
        self.assertIsNone(got['s0']['reason'], got['s0'])

    def test_frames_that_cannot_be_told_apart_fail_closed(self):
        r = Q.picture_sync(self.aroll, self.segs[3:], self.src, 1920)['s3']
        self.assertRegex(r['reason'] or '', '^unmeasurable', r)


class Example(unittest.TestCase):
    def test_example_spec_is_consistent(self):
        with open(os.path.join(ROOT, 'examples', 'reel.example.json'), encoding='utf-8') as fh:
            sp = json.load(fh)
        n = sum(len(E.sentences(s['narration'])) for s in sp['shots'])
        self.assertEqual(len(sp['sentences']), n)
        self.assertIn(sp['look'], [f[:-5] for f in os.listdir(os.path.join(ROOT, 'looks'))])
        self.assertIn(sp['platform'], B.PLATFORMS)


if __name__ == '__main__':
    unittest.main()
