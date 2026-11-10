#!/usr/bin/env python3
"""Ticket 11 (planned mode for any episode): each take's cut is measured once from its own audio and words (take_cuts.py)
and travels in the packet (take meta.cut), the film lays the takes end to end from those cuts (no kit placements), and the
captions are built from each take's words and the line's caption groups (no kit captions). Film 3 through this path
reproduces r3's cuts, r3's captions and run-6's graphic frames. Synthetic and fixture tests; the film-3 measurement test
reads the r3 takes from the evidence folder and is skipped without them.
    PY tests/test_general_layout.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import render_planned as P  # noqa: E402
import take_cuts as C  # noqa: E402

with open(os.path.join(ROOT, 'tests', 'fixtures', 'film3-general.json'), encoding='utf-8') as fh:
    F3 = json.load(fh)
EVIDENCE = '/Users/andy/CortexWorkspace/evidence/scratch-18d571abeb0a4cd4b00e8ab43b69f1da/remake-film3-20261108'
F3_FILES = ['gates/take1-1080.mp4', 'gates/take2-1080.mp4'] + [f'raw/take{n}-raw.mp4' for n in range(3, 8)]


def cut(a, b, speech=(0.0, 0.0)):
    return {'fromFrame': a, 'toFrame': b, 'fps': 24, 'speech': list(speech), 'rule': C.RULE}


def line(lid, text, a, b, words=None, captions=None, speech=(0.0, 0.0)):
    ln = {'id': lid, 'text': text, 'take': {'id': f'take-{lid}', 'sha256': lid * 32, 'storagePath': f'{lid}.mp4', 'width': 720,
                                              'height': 1280, 'words': words or [], 'meta': {'cut': cut(a, b, speech)}}}
    return {**ln, 'captions': captions} if captions else ln


def film3_packet(graphics=(), persistent=()):
    """film 3's lines with their stored words, r3's caption groups and r3's cuts as measured cuts"""
    lines = [line(ln['id'], ln['text'], ln['r3Cut']['fromFrame'], ln['r3Cut']['toFrame'], ln['take']['words'], ln['captions'],
                  (ln['r3Cut']['speechStart'], 0.0)) for ln in F3['lines']]
    return {'versions': {'kit': P.PIN['kit'], 'engine': dict(P.PIN)}, 'lines': lines, 'graphics': list(graphics),
            'persistent': list(persistent)}


def tone_wav(path, parts, rate=16000):
    """a wav of (seconds, loud?) parts: a 220 Hz tone at -12 dBFS or silence"""
    import numpy as np
    out = []
    for sec, loud in parts:
        t = np.arange(int(sec * rate)) / rate
        out.append((0.25 * np.sin(2 * np.pi * 220 * t)) if loud else np.zeros_like(t))
    import wave
    data = (np.concatenate(out) * 32767).astype('<i2').tobytes()
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


class TakeCut(unittest.TestCase):
    def test_the_cut_runs_from_a_tenth_before_speech_to_0_12_s_after_it_on_whole_frames(self):
        # film 3 take 1: speech 0.25-3.74 s, words 0-3.8 s -> frames 3 (0.15 s floored) to 95 (3.92 s ceiled)
        self.assertEqual(C.cut_frames((0.25, 3.74), [{'start': 0, 'end': 3.8}], 97, 24), (3, 95))

    def test_a_word_heard_after_the_last_loud_sound_holds_the_cut_open(self):
        self.assertEqual(C.cut_frames((0.25, 3.0), [{'start': .3, 'end': 3.5}], 200, 24), (4, 87))   # 0.2 s floored; 3.62 s ceiled

    def test_a_word_heard_before_the_first_loud_sound_does_not_move_the_cut_earlier(self):
        # r3 (plan2.py): the in point is max(first loud sound, first word) less 0.10 s
        self.assertEqual(C.cut_frames((1.1, 3.84), [{'start': .8, 'end': 3.66}], 97, 24), (24, 96))

    def test_the_cut_never_runs_past_the_takes_last_frame_or_before_its_first(self):
        self.assertEqual(C.cut_frames((0.02, 3.98), [{'start': 0, 'end': 3.82}], 97, 24), (0, 97))

    def test_the_first_and_last_script_words_bound_the_cut_not_a_sound_before_them(self):
        # face23be l1: the take opens on an ad-libbed "Mm," (loud from 0.0 s); the script starts on "Overwhelmed,"
        words = [{'word': 'Mm,', 'start': 0.0, 'end': 0.3}, {'word': 'overwhelmed,', 'start': 0.62, 'end': 1.1},
                 {'word': 'but', 'start': 1.1, 'end': 1.3}, {'word': 'wait.', 'start': 1.3, 'end': 1.8}]
        self.assertEqual(C.script_words(words, 'Overwhelmed, but wait.'), words[1:])
        self.assertEqual(C.cut_frames((0.0, 1.8), C.script_words(words, 'Overwhelmed, but wait.'), 120, 24), (12, 47))
        self.assertEqual(C.script_words(words, None), words)

    def test_an_unclean_opening_never_cuts_off_speech(self):
        # face23be l6: soft-edged object until 0.79 s, "Pick" from 0.01 s: the cut keeps the words (spec mode's opening rule)
        self.assertEqual(C.cut_frames((0.01, 3.95), [{'start': 0.0, 'end': 3.9}], 97, 24), (0, 97))

    def test_speech_runs_from_the_first_to_the_last_loud_sound_ignoring_a_blip_at_the_files_end(self):
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, 't.wav')
            tone_wav(wav, [(0.3, False), (1.0, True), (0.4, False), (0.02, True)])
            start, end = C.speech_span(wav)
        self.assertAlmostEqual(start, 0.3, delta=0.02)
        self.assertAlmostEqual(end, 1.3, delta=0.02)

    def test_silence_throughout_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, 't.wav')
            tone_wav(wav, [(1.0, False)])
            with self.assertRaises(C.NoSpeech):
                C.speech_span(wav)


@unittest.skipUnless(all(os.path.isfile(os.path.join(EVIDENCE, f)) for f in F3_FILES), 'film 3 takes not on this machine')
class Film3Measured(unittest.TestCase):
    def test_film3s_takes_measure_to_r3s_cuts(self):
        for ln, f in zip(F3['lines'], F3_FILES):
            m = C.measure(os.path.join(EVIDENCE, f), ln['take']['words'])
            self.assertEqual((m['fromFrame'], m['toFrame']), (ln['r3Cut']['fromFrame'], ln['r3Cut']['toFrame']), ln['id'])
            self.assertAlmostEqual(m['speech'][0], ln['r3Cut']['speechStart'], delta=0.025, msg=ln['id'])

    def test_film3s_captions_from_measured_cuts_show_every_word_on_r3s_frame(self):
        pk = film3_packet()
        for ln, f in zip(pk['lines'], F3_FILES):
            ln['take']['meta']['cut'] = {k: v for k, v in C.measure(os.path.join(EVIDENCE, f), ln['take']['words']).items()
                                         if k in ('fromFrame', 'toFrame', 'fps', 'speech', 'rule')}
        place, total = P.layout(pk, 24)
        page = P.captions_page(P.caption_groups(pk, place, 24), total / 24)
        self.assertEqual([(w, P.first_frame(t, 24)) for w, t in P.caption_words(page)],
                         [(w, P.first_frame(t, 24)) for w, t in P.caption_words(R33_CAPTIONS)])


with open(os.path.join(ROOT, 'kits', 'film3-r3.3', 'captions.html'), encoding='utf-8') as fh:
    R33_CAPTIONS = fh.read()


class Layout(unittest.TestCase):
    def test_takes_play_end_to_end_from_their_cuts(self):
        pk = {'lines': [line('a', 'One.', 3, 95), line('b', 'Two.', 24, 96), line('c', 'Three.', 0, 10)]}
        place, total = P.layout(pk, 24)
        self.assertEqual(total, 92 + 72 + 10)
        self.assertEqual([(p['startFrame'], p['fromFrame'], p['frames']) for p in place.values()], [(0, 3, 92), (92, 24, 72), (164, 0, 10)])
        self.assertEqual(place['b']['start'], 92 / 24)
        self.assertEqual(place['b']['mediaStart'], 1.0)
        self.assertEqual(place['b']['duration'], 3.0)

    def test_film3s_cuts_lay_out_as_r3_and_the_old_kit_placed_them(self):
        place, total = P.layout(film3_packet(), 24)
        with open(os.path.join(ROOT, 'kits', 'film3-r3.3', 'kit.json'), encoding='utf-8') as fh:
            old = json.load(fh)
        for lid, t in old['takes'].items():
            self.assertAlmostEqual(place[lid]['start'], t['start'], places=5)
            self.assertAlmostEqual(place[lid]['mediaStart'], t['mediaStart'], places=5)
            self.assertAlmostEqual(place[lid]['duration'], t['duration'], places=5)
        self.assertAlmostEqual(total / 24, old['duration'], places=5)

    def test_a_take_with_no_measured_cut_refuses_naming_its_line(self):
        pk = {'lines': [line('a', 'One.', 0, 10), {'id': 'b', 'text': 'Two.', 'take': {'id': 't', 'sha256': 'b' * 64}}]}
        with self.assertRaises(P.Refused) as e:
            P.layout(pk, 24)
        self.assertIn('b', str(e.exception))
        self.assertIn('cut', str(e.exception))

    def test_an_empty_cut_or_a_cut_at_another_frame_rate_refuses(self):
        for bad in (cut(10, 10), {**cut(0, 10), 'fps': 30}):
            pk = {'lines': [{'id': 'a', 'text': 'One.', 'take': {'id': 't', 'sha256': 'a' * 64, 'meta': {'cut': bad}}}]}
            with self.assertRaises(P.Refused):
                P.layout(pk, 24)

    def test_a_cut_past_its_takes_last_frame_refuses(self):
        pk = {'lines': [line('a', 'One.', 0, 120)]}
        with self.assertRaises(P.Refused) as e:
            P.check_cuts(pk, {'a' * 32: 97})
        self.assertIn('97', str(e.exception))

    def test_a_nine_line_packet_passes_the_packet_checks(self):
        pk = {'versions': {'engine': dict(P.PIN)}, 'graphics': [], 'persistent': [],
              'lines': [line(f'l{k}', f'Line {k} is here.', 0, 48) for k in range(1, 10)]}
        P.check_packet(pk, P.load_kit())
        self.assertEqual(P.layout(pk, 24)[1], 9 * 48)


class Captions(unittest.TestCase):
    def test_film3s_captions_from_its_take_words_are_r3s(self):
        pk = film3_packet()
        place, total = P.layout(pk, 24)
        page = P.captions_page(P.caption_groups(pk, place, 24), total / 24)
        self.assertEqual(P.caption_words(page), P.caption_words(R33_CAPTIONS))
        tl = lambda s: re.findall(r'tl\.[^\n]*', s[s.index('<script>'):])          # noqa: E731
        self.assertEqual(tl(page), tl(R33_CAPTIONS))
        body = lambda s: re.findall(r'<div class="cg"[^\n]*', s)                   # noqa: E731
        self.assertEqual(body(page), body(R33_CAPTIONS))

    def test_caption_groups_must_be_their_lines_words(self):
        pk = {'lines': [line('a', 'Your phone. Put it away.', 0, 96, captions='Your phone.* | Put it there.')]}
        with self.assertRaises(P.Refused) as e:
            P.caption_groups(pk, P.layout(pk, 24)[0], 24)
        self.assertIn('a', str(e.exception))

    def test_a_line_without_groups_breaks_after_punctuation_or_three_words_and_accents_the_word_that_ends_a_phrase(self):
        # r3's accents fall mostly on phrase-final words ("full?", "hacks.", "table?", "phone.", "room,", "reach.", "page.")
        self.assertEqual(P.default_groups('Stand up and press your feet into the floor, like you are pushing it away.'),
                         'Stand up and | press your feet | into the floor,* | like you are | pushing it away.*')

    def test_a_group_too_wide_for_the_action_column_is_set_smaller_by_its_real_glyph_widths(self):
        # face23be l3 "Name something you": r3's 0.6 em a character gave 75 px, its ink reached x 983 past the Reels action
        # column (right edge 945 from y 1155, which the caption band reaches)
        groups = [[('Name', False, 1.0, 1.2), ('something', False, 1.2, 1.5), ('you', False, 1.5, 1.6)]]
        size = int(re.search(r'font-size:(\d+)px', P.captions_page(groups, 2.0)).group(1))
        self.assertLess(size, 75)
        self.assertLessEqual(540 + P.caption_width(['Name', 'something', 'you'], size) / 2, 945)

    def test_words_the_takes_transcript_merged_or_missed_are_timed_evenly_between_their_heard_neighbours(self):
        words = [{'word': 'Stand', 'start': .3, 'end': .5}, {'word': 'up', 'start': .5, 'end': .7},
                 {'word': 'press', 'start': 1.0, 'end': 1.2}]
        pk = {'lines': [line('a', 'Stand up and press.', 0, 48, words, speech=(.3, 1.2))]}
        groups = P.caption_groups(pk, P.layout(pk, 24)[0], 24)
        self.assertEqual([(t, s) for g in groups for t, _, s, _ in g], [('Stand', .3), ('up', .5), ('and', .7), ('press.', 1.0)])

    def test_a_missed_tail_is_spread_to_the_cuts_end_and_every_group_shows(self):
        # review 10 Nov: with "your feet into the floor." unheard, every one of them took the last heard time and two groups
        # were hidden on the frame they showed
        words = [{'word': w, 'start': .3 + .25 * k, 'end': .5 + .25 * k} for k, w in enumerate('Stand up and press'.split())]
        pk = {'lines': [line('a', 'Stand up and press your feet into the floor.', 0, 96, words, speech=(.3, 1.25))]}
        groups = P.caption_groups(pk, P.layout(pk, 24)[0], 24)
        starts = [s for g in groups for _, _, s, _ in g]
        self.assertEqual(starts, sorted(set(starts)))                       # strictly later word by word
        self.assertLessEqual(starts[-1], 96 / 24 - 0.12)                     # by the cut's end, less its tail
        page = P.captions_page(groups, 4.0)
        for gi in range(len(groups) - 1):
            on = float(re.search(rf'tl\.set\("#cg{gi}", {{ opacity: 1[^)]*\}}, ([\d.]+)\)', page).group(1))
            off = float(re.search(rf'tl\.set\("#cg{gi}", {{ opacity: 0[^)]*\}}, ([\d.]+)\)', page).group(1))
            self.assertGreater(off, on, gi)

    def test_a_take_with_no_words_refuses_naming_its_line(self):
        pk = {'lines': [line('a', 'Stand up.', 0, 48, [], speech=(.3, 1.0))]}
        with self.assertRaises(P.Refused) as e:
            P.caption_groups(pk, P.layout(pk, 24)[0], 24)
        self.assertIn('line a', str(e.exception))
        self.assertIn('words', str(e.exception))

    def test_ampersands_and_angle_brackets_are_shown_and_read_back_as_the_words(self):
        words = [{'word': w, 'start': .3 + .3 * k, 'end': .5 + .3 * k} for k, w in enumerate(['Q&A', 'time', '<now>.'])]
        pk = {'lines': [line('a', 'Q&A time <now>.', 0, 48, words, speech=(.3, 1.1))]}
        place, total = P.layout(pk, 24)
        page = P.captions_page(P.caption_groups(pk, place, 24), total / 24)
        self.assertIn('Q&amp;A', page)
        self.assertEqual([w for w, _ in P.caption_words(page)], ['Q&A', 'time', '<now>.'])


class Film3ThroughTheGeneralPath(unittest.TestCase):
    def test_run6s_plan_resolves_to_run6s_frames(self):
        pk = film3_packet(F3['run6']['graphics'], F3['run6']['persistent'])
        place, total = P.layout(pk, 24)
        caps = [(w, P.first_frame(t, 24)) for w, t in P.caption_words(P.captions_page(P.caption_groups(pk, place, 24), total / 24))]
        got = P.resolve(pk, F3['run6']['transcript'], place, 24, caps)
        want = {r['id']: r for r in F3['run6']['resolved']}
        for r in got:
            w = want[r['id']]
            self.assertEqual((r['startFrame'], r['frames']), (w['startFrame'], w['frames']), r['id'])
            self.assertEqual([(u['text'], u['frame']) for u in r.get('updates', [])], [tuple(u) for u in w.get('updates', [])])


with open(os.path.join(ROOT, 'tests', 'fixtures', 'face23be-general.json'), encoding='utf-8') as fh:
    FACE = json.load(fh)


class AnotherEpisode(unittest.TestCase):
    """face23be: 9 lines, no pictures, default caption groups, the counter on the lesson's three jobs"""
    def packet(self):
        lines = [{'id': ln['id'], 'text': ln['text'], 'take': {'id': f"take-{ln['id']}", 'sha256': ln['takeSha256'], 'words': ln['words'],
                                                              'meta': {'cut': ln['cut']}}} for ln in FACE['lines']]
        return {'versions': {'engine': dict(P.PIN)}, 'lines': lines, 'graphics': [], 'persistent': [FACE['counter']]}

    def test_nine_lines_lay_out_from_their_cuts_and_pass_the_packet_checks(self):
        pk = self.packet()
        P.check_packet(pk, P.load_kit())
        place, total = P.layout(pk, 24)
        self.assertEqual(len(place), 9)
        self.assertEqual(total, sum(ln['cut']['toFrame'] - ln['cut']['fromFrame'] for ln in FACE['lines']))
        self.assertEqual(place['l1']['fromFrame'], 20)          # after the ad-libbed "Mm,"

    def test_every_caption_group_fits_the_action_column(self):
        pk = self.packet()
        place, total = P.layout(pk, 24)
        page = P.captions_page(P.caption_groups(pk, place, 24), total / 24)
        for size, inner in re.findall(r'<div class="cg" id="cg\d+" style="font-size:(\d+)px">(.*?)</div>', page):
            words = [P.html.unescape(w) for w in re.findall(r'>([^<]+)</span>', inner)]
            self.assertLessEqual(P.caption_width(words, int(size)), P.CAPTION_MAX_W, words)
        self.assertEqual([w for w, _ in P.caption_words(page)], ' '.join(ln['text'] for ln in FACE['lines']).split())


class MalformedPackets(unittest.TestCase):
    def packet(self, d, take_file, **changes):
        ln = line('a', 'One two.', 0, 24, [{'word': 'One', 'start': 0, 'end': .3}, {'word': 'two.', 'start': .3, 'end': .6}])
        ln['take']['sha256'] = C.sha256(take_file)
        ln['take'].update(changes.pop('take', {}))
        ln.update(changes)
        path = os.path.join(d, f'p{len(os.listdir(d))}.json')
        with open(path, 'w') as fh:
            json.dump({'versions': {'engine': dict(P.PIN)}, 'lines': [ln], 'graphics': [], 'persistent': []}, fh)
        return path

    def test_bad_caption_groups_take_words_or_a_take_that_is_not_a_video_refuse_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            shots, pk = os.path.join(d, 'shots'), os.path.join(d, 'pk')
            os.makedirs(shots)
            os.makedirs(pk)
            take = os.path.join(shots, 'a.mp4')
            subprocess.run([P.skillenv.tool('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=black:s=72x128:r=24:d=1',
                            '-c:v', 'libx264', take], check=True)
            junk = os.path.join(d, 'junk')
            os.makedirs(junk)
            with open(os.path.join(junk, 'b.mp4'), 'wb') as fh:
                fh.write(b'not a video')
            cases = [(self.packet(pk, take, captions=['One', 'two']), shots),
                     (self.packet(pk, take, take={'words': [{'start': 0, 'end': 1}]}), shots),
                     (self.packet(pk, os.path.join(junk, 'b.mp4')), junk)]
            for i, (path, folder) in enumerate(cases):
                out = os.path.join(d, f'out{i}')
                self.assertEqual(P.main(path, folder, out), 2, i)
                self.assertFalse(os.path.exists(os.path.join(out, 'project')), i)


class MeasureCommand(unittest.TestCase):
    def test_measure_prints_one_cut_per_take_and_refuses_a_silent_take(self):
        with tempfile.TemporaryDirectory() as d:
            ok, silent = os.path.join(d, 'ok.mp4'), os.path.join(d, 'silent.mp4')
            for path, parts in ((ok, [(0.3, False), (1.0, True), (0.4, False)]), (silent, [(1.7, False)])):
                wav = path + '.wav'
                tone_wav(wav, parts)
                subprocess.run([P.skillenv.tool('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=black:s=72x128:r=24:d=1.7',
                                '-i', wav, '-shortest', '-c:v', 'libx264', '-c:a', 'aac', path], check=True)
            words = os.path.join(d, 'words.json')
            with open(words, 'w') as fh:
                json.dump({ok: [{'word': 'Hi', 'start': .3, 'end': 1.2}]}, fh)
            p = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'take_cuts.py'), '--words', words, ok, silent],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual((out[0]['fromFrame'], out[0]['toFrame']), (4, 35))   # 0.3 - 0.1 s floored; 1.3 + 0.12 s ceiled
            self.assertEqual(out[0]['rule'], C.RULE)
            self.assertIn('no speech', out[1]['refused'])
            # --out FILE: the same list written to a file (the render worker keeps only its output's tail)
            dest = os.path.join(d, 'cuts.json')
            q = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'take_cuts.py'), '--words', words, '--out', dest, ok],
                               capture_output=True, text=True)
            self.assertEqual(q.returncode, 0, q.stderr)
            with open(dest) as fh:
                self.assertEqual([(x['fromFrame'], x['toFrame']) for x in json.load(fh)], [(4, 35)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
