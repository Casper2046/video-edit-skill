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
import reel_check as Q  # noqa: E402

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
        self.assertEqual(rep['warnings'], [])
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


class Check(unittest.TestCase):
    def test_onset_takes_the_nearest_silence_end_else_the_word(self):
        self.assertEqual(Q.onset(.4, [.1, .46, 2.0]), .46)
        self.assertEqual(Q.onset(0.0, [1.18, 4.09]), 0.0)

    def test_caption_error_is_reveal_minus_measured_onset_in_cut_time(self):
        segs = [{'id': 'a', 'clip': 'c1', 'frame': 0, 'src_in': .4, 'word_idx': [0]},
                {'id': 'b', 'clip': 'c1', 'frame': 60, 'src_in': 3.0, 'word_idx': [1]}]
        words = {'c1': [{'start': .5}, {'start': 3.1}]}
        page = "tl.fromTo('#w0-0',{},{},0.050);tl.fromTo('#w1-0',{},{},2.130);"
        errs = Q.caption_errors(segs, words, {'c1': [.46, 3.2]}, page)
        self.assertEqual([round(e, 3) for _, _, e in errs], [-.01, -.07])

    def test_cut_frames_within_one_frame(self):
        segs = [{'frame': 0}, {'frame': 84}, {'frame': 182}]
        self.assertIsNone(Q.cut_mismatch(segs, [83, 183]))
        self.assertIn('off by more than 1', Q.cut_mismatch(segs, [80, 183]))
        self.assertIn('2 cuts, expected 1', Q.cut_mismatch(segs, [83]))

    def test_text_check_ignores_case_quotes_and_markup(self):
        page = "<body><div>'take</div><div>five' now</div><span>Got a minute?</span><script>x</script>"
        self.assertEqual(Q.missing_text(page, ["'Take five' now", 'GOT A MINUTE', 'not here']), ['not here'])


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
