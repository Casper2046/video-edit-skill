#!/usr/bin/env python3
"""Planned mode tests (SPEC v1.3 contracts B and E): cue words resolve to a line's word range and then to an onset on the
render transcript, or refuse with the graphic id; the timing table is computed from measured frames. Synthetic only: no
media, no render, no network.
    PY tests/test_render_planned.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import render_planned as P  # noqa: E402

L2 = 'What is the first thing to move off the table?'
L3 = 'Your phone. Put it in another room, out of reach.'


def g(cue, gid='g-x', hold=2.2):
    return {'id': gid, 'treatment': 'photo_panel', 'layout': 'photo_panel', 'cue': cue, 'holdSec': hold, 'assets': []}


def words(text, t0, step=.3):
    """a transcript of text: one word every step seconds from t0"""
    return [{'text': w, 'start': round(t0 + k * step, 3), 'end': round(t0 + k * step + .25, 3)} for k, w in enumerate(text.split())]


class CueSpan(unittest.TestCase):
    def test_words_with_their_index_resolve_to_that_range(self):
        self.assertEqual(P.cue_span(L3, g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'})), (1, 1))

    def test_case_and_punctuation_are_ignored(self):
        self.assertEqual(P.cue_span(L3, g({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'Out of REACH'})), (7, 9))

    def test_a_word_that_occurs_once_needs_no_index(self):
        self.assertEqual(P.cue_span(L3, g({'lineId': 'l3', 'words': 'another room'})), (5, 6))

    def test_a_cue_word_not_in_the_line_refuses_with_the_graphic_id(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L3, g({'lineId': 'l3', 'words': 'tablet'}, 'g-tablet'))
        self.assertIn('g-tablet', str(e.exception))
        self.assertIn('not in line l3', str(e.exception))

    def test_a_repeated_word_without_its_index_refuses_with_the_graphic_id(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L2, g({'lineId': 'l2', 'words': 'the'}, 'g-the'))
        self.assertIn('g-the', str(e.exception))
        self.assertIn('2 times', str(e.exception))
        self.assertIn('words 2, 8', str(e.exception))

    def test_the_same_repeated_word_with_its_index_resolves(self):
        self.assertEqual(P.cue_span(L2, g({'lineId': 'l2', 'from': 8, 'to': 8, 'words': 'the'})), (8, 8))

    def test_words_that_are_not_the_words_at_the_index_refuse(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L3, g({'lineId': 'l3', 'from': 2, 'to': 2, 'words': 'phone'}, 'g-off'))
        self.assertIn('g-off', str(e.exception))

    def test_an_index_past_the_line_refuses(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L3, g({'lineId': 'l3', 'from': 10, 'to': 10}, 'g-far'))
        self.assertIn('g-far', str(e.exception))


def packet(*graphics):
    return {'versions': {'kit': 'film3-r3', 'engine': dict(P.PIN)},
            'lines': [{'id': 'l2', 'text': L2}, {'id': 'l3', 'text': L3}], 'graphics': list(graphics), 'persistent': []}


PLACE = {'l2': {'start': 0.0, 'duration': 4.0}, 'l3': {'start': 4.0, 'duration': 3.6}}


class Resolve(unittest.TestCase):
    def transcript(self):
        return words(L2, .5) + words(L3, 4.2)

    def test_a_graphic_enters_on_the_frame_that_holds_its_first_cue_words_onset(self):
        (r,) = P.resolve(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-phone')), self.transcript(), PLACE, 24)
        self.assertEqual(r['onset'], 4.5)                 # "phone" at 4.2 + 1 x 0.3
        self.assertEqual(r['startFrame'], 108)            # 4.5 s x 24
        self.assertEqual(r['frames'], 53)                 # 2.2 s x 24 = 52.8
        self.assertLessEqual(r['startFrame'] / 24, r['onset'])
        self.assertLess(r['onset'], (r['startFrame'] + 1) / 24)

    def test_a_cue_word_missing_from_the_render_transcript_refuses_with_the_graphic_id(self):
        heard = [w for w in self.transcript() if w['text'] != 'phone.']
        with self.assertRaises(P.Refused) as e:
            P.resolve(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-phone')), heard, PLACE, 24)
        self.assertIn('g-phone', str(e.exception))
        self.assertIn('render transcript', str(e.exception))

    def test_a_hold_past_its_lines_take_refuses_with_the_graphic_id(self):
        with self.assertRaises(P.Refused) as e:
            P.resolve(packet(g({'lineId': 'l3', 'from': 7, 'to': 9}, 'g-long', hold=2.5)), self.transcript(), PLACE, 24)
        self.assertIn('g-long', str(e.exception))
        self.assertIn('past', str(e.exception))

    def test_a_cue_on_a_line_the_packet_does_not_have_refuses(self):
        with self.assertRaises(P.Refused) as e:
            P.check_cues(packet(g({'lineId': 'l9', 'from': 0, 'to': 0}, 'g-l9')))
        self.assertIn('g-l9', str(e.exception))


class TimingTable(unittest.TestCase):
    def resolved(self, gid='g-phone', onset=4.5, start=108):
        return {'id': gid, 'treatment': 'photo_panel', 'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone',
                'onset': onset, 'startFrame': start, 'frames': 53, 'holdSec': 2.2}

    def test_hold_and_onset_come_from_the_measured_frames(self):
        (row,), failures = P.timing_table([self.resolved()], {'g-phone': list(range(108, 161))}, 24)
        self.assertEqual((row['measuredFirstFrame'], row['measuredLastFrame']), (108, 160))
        self.assertAlmostEqual(row['measuredHoldSec'], 53 / 24, places=5)
        self.assertEqual(row['onsetDeltaFrames'], 0)
        self.assertTrue(row['pass'])
        self.assertEqual(failures, [])

    def test_a_main_picture_shown_under_two_seconds_fails(self):
        (row,), failures = P.timing_table([self.resolved()], {'g-phone': list(range(108, 155))}, 24)   # 47 frames
        self.assertFalse(row['pass'])
        self.assertTrue(any('g-phone' in f and '2.0' in f for f in failures))

    def test_a_graphic_that_shows_after_its_cue_frame_fails(self):
        (row,), failures = P.timing_table([self.resolved()], {'g-phone': list(range(110, 163))}, 24)
        self.assertEqual(row['onsetDeltaFrames'], 2)
        self.assertTrue(any('g-phone' in f and 'onset' in f for f in failures))

    def test_a_graphic_never_seen_in_the_frames_fails(self):
        _, failures = P.timing_table([self.resolved()], {'g-phone': []}, 24)
        self.assertTrue(any('g-phone' in f and 'not on screen' in f for f in failures))

    def test_two_entrances_within_half_a_second_fail(self):
        a, b = self.resolved('g-a', 4.5, 108), self.resolved('g-b', 4.8, 115)
        _, failures = P.timing_table([a, b], {'g-a': list(range(108, 161)), 'g-b': list(range(115, 168))}, 24)
        self.assertTrue(any('g-a' in f and 'g-b' in f and '0.5' in f for f in failures))


class Entry(unittest.TestCase):
    def test_render_packet_refuses_a_planned_packet_with_a_bad_cue_before_any_media_work(self):
        with tempfile.TemporaryDirectory() as d:
            pk = os.path.join(d, 'packet.json')
            with open(pk, 'w') as fh:
                json.dump(packet(g({'lineId': 'l3', 'words': 'tablet'}, 'g-tablet')), fh)
            os.makedirs(os.path.join(d, 'shots'))
            p = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'render_packet.py'), pk, '--shots',
                                os.path.join(d, 'shots'), '--out', os.path.join(d, 'out')], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn('g-tablet', p.stderr)

    def test_a_packet_pinned_to_another_engine_build_refuses(self):
        pk = packet()
        pk['versions']['engine'] = {**P.PIN, 'hyperframes': '0.8.34'}
        with self.assertRaises(P.Refused) as e:
            P.check_pin(pk)
        self.assertIn('0.8.34', str(e.exception))


if __name__ == '__main__':
    unittest.main()
