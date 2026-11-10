#!/usr/bin/env python3
"""Planned mode tests (SPEC v1.3 contracts B, C and E): cue words resolve to a line's word range and then to an onset on the
render transcript, or refuse with the graphic id; only the kit's pieces on their layouts are accepted; the step counter is
one element with timed updates; the timing table is computed from measured frames. Synthetic only: no media, no render,
no network.
    PY tests/test_render_planned.py
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

L2 = 'What is the first thing to move off the table?'
L3 = 'Your phone. Put it in another room, out of reach.'
L4 = 'Then stay fully present with the one thing in front of you. Just that page.'
KIT = P.load_kit()


def g(cue, gid='g-x', hold=2.2, **kw):
    return {'id': gid, 'treatment': 'photo_panel', 'layout': 'photo_panel', 'cue': cue, 'holdSec': hold, 'assets': [], **kw}


def words(text, t0, step=.3):
    """a transcript of text: one word every step seconds from t0"""
    return [{'text': w, 'start': round(t0 + k * step, 3), 'end': round(t0 + k * step + .25, 3)} for k, w in enumerate(text.split())]


def still(aid='still-phone', sha='a' * 64, w=474, h=1000, **meta):
    return {'id': aid, 'type': 'still', 'storagePath': f'gen/{aid}.png', 'sha256': sha, 'width': w, 'height': h,
            **({'meta': meta} if meta else {})}


def take(tid, sha, w=720, h=1280, **meta):
    return {'id': tid, 'type': 'take', 'storagePath': f'takes/{tid}.mp4', 'sha256': sha, 'width': w, 'height': h, 'words': [],
            **({'meta': meta} if meta else {})}


def counter(*updates, until='l4'):
    return {'id': 'p-counter', 'treatment': 'step_counter', 'layout': 'overlay', 'untilLineEnd': until, 'subject': 'steps',
            'updates': [{'cue': c, 'text': t} for c, t in updates]}


class CueSpan(unittest.TestCase):
    def test_words_with_their_index_resolve_to_that_range(self):
        self.assertEqual(P.cue_span(L3, {'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-x'), (1, 1))

    def test_case_and_punctuation_are_ignored(self):
        self.assertEqual(P.cue_span(L3, {'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'Out of REACH'}, 'g-x'), (7, 9))

    def test_a_word_that_occurs_once_needs_no_index(self):
        self.assertEqual(P.cue_span(L3, {'lineId': 'l3', 'words': 'another room'}, 'g-x'), (5, 6))

    def test_a_cue_word_not_in_the_line_refuses_with_the_graphic_id(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L3, {'lineId': 'l3', 'words': 'tablet'}, 'g-tablet')
        self.assertIn('g-tablet', str(e.exception))
        self.assertIn('not in line l3', str(e.exception))

    def test_a_repeated_word_without_its_index_refuses_with_the_graphic_id(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L2, {'lineId': 'l2', 'words': 'the'}, 'g-the')
        self.assertIn('g-the', str(e.exception))
        self.assertIn('2 times', str(e.exception))
        self.assertIn('words 2, 8', str(e.exception))

    def test_the_same_repeated_word_with_its_index_resolves(self):
        self.assertEqual(P.cue_span(L2, {'lineId': 'l2', 'from': 8, 'to': 8, 'words': 'the'}, 'g-x'), (8, 8))

    def test_words_that_are_not_the_words_at_the_index_refuse(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L3, {'lineId': 'l3', 'from': 2, 'to': 2, 'words': 'phone'}, 'g-off')
        self.assertIn('g-off', str(e.exception))

    def test_an_index_range_without_its_words_refuses_with_the_graphic_id(self):
        """review: an index range with no words was unchecked; every cue carries the words it copied from the line"""
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L3, {'lineId': 'l3', 'from': 1, 'to': 1}, 'g-bare')
        self.assertIn('g-bare', str(e.exception))
        self.assertIn('words', str(e.exception))

    def test_an_index_past_the_line_refuses(self):
        with self.assertRaises(P.Refused) as e:
            P.cue_span(L3, {'lineId': 'l3', 'from': 10, 'to': 10, 'words': 'reach'}, 'g-far')
        self.assertIn('g-far', str(e.exception))


def packet(*graphics, persistent=()):
    return {'versions': {'kit': P.PIN['kit'], 'engine': dict(P.PIN)},
            'lines': [{'id': 'l2', 'text': L2, 'take': take('take2', '2' * 64)},
                      {'id': 'l3', 'text': L3, 'take': take('take3', '3' * 64, topBandShiftPx=200)},
                      {'id': 'l4', 'text': L4, 'take': take('take4', '4' * 64, targets=[
                          {'id': 'notepad-page', 'box': [33.333, 1086.667, 390.667, 1249.333]}])}],
            'graphics': list(graphics), 'persistent': list(persistent)}


PLACE = {'l2': {'start': 0.0, 'duration': 4.0}, 'l3': {'start': 4.0, 'duration': 3.6}, 'l4': {'start': 7.6, 'duration': 4.4}}
BAND = {'mediaStart': 0.5, 'mediaEnd': 4.041667, 'frame': [1080, 1918], 'bandHeightPx': 555}


class Checks(unittest.TestCase):
    """contract C: only the kit's pieces on their layouts; a malformed graphic refuses naming itself"""
    def refused(self, pk):
        with self.assertRaises(P.Refused) as e:
            P.check_packet(pk, KIT)
        return str(e.exception)

    def test_the_film_3_pieces_on_their_layouts_are_accepted(self):
        P.check_packet(packet(
            g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-panel', assets=[still(), still('still-sticky', 'b' * 64)]),
            g({'lineId': 'l3', 'from': 0, 'to': 1, 'words': 'Your phone.'}, 'g-band', treatment='top_band_broll', layout='top_band',
              assets=[{**still('broll-x', 'c' * 64, 480, 854), 'type': 'broll', 'meta': BAND}]),
            g({'lineId': 'l4', 'from': 12, 'to': 14, 'words': 'Just that page.'}, 'g-ring', 1.5, treatment='circle', layout='overlay',
              target='notepad-page'),
            persistent=[counter(({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, '1/3'),
                                ({'lineId': 'l4', 'from': 2, 'to': 2, 'words': 'fully'}, '2/3'))]), KIT)

    def test_a_cue_on_a_line_the_packet_does_not_have_refuses(self):
        self.assertIn('g-l9', self.refused(packet(g({'lineId': 'l9', 'from': 0, 'to': 0, 'words': 'x'}, 'g-l9'))))

    def test_a_graphic_without_a_cue_refuses_with_its_id_not_as_an_unreadable_packet(self):
        gr = g({}, 'g-nocue')
        del gr['cue']
        msg = self.refused(packet(gr))
        self.assertIn('g-nocue', msg)
        self.assertIn('cue', msg)

    def test_a_graphic_without_a_hold_refuses_with_its_id(self):
        gr = g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-nohold')
        del gr['holdSec']
        self.assertIn('g-nohold', self.refused(packet(gr)))

    def test_an_unsupported_treatment_refuses_as_unsupported(self):
        msg = self.refused(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-card', treatment='word_card',
                                    layout='overlay')))
        self.assertIn('g-card', msg)
        self.assertIn('unsupported', msg)

    def test_a_kit_piece_on_another_layout_refuses_as_unsupported(self):
        msg = self.refused(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-band', treatment='top_band_broll',
                                    layout='photo_panel', assets=[{**still('b', 'c' * 64), 'type': 'broll', 'meta': BAND}])))
        self.assertIn('g-band', msg)
        self.assertIn('unsupported', msg)

    def test_a_photo_panel_with_more_pictures_than_its_slots_refuses(self):
        msg = self.refused(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-three',
                                    assets=[still('a1', '1' * 64), still('a2', '2' * 64), still('a3', '5' * 64)])))
        self.assertIn('g-three', msg)

    def test_a_band_whose_b_roll_has_no_trim_refuses(self):
        msg = self.refused(packet(g({'lineId': 'l3', 'from': 0, 'to': 1, 'words': 'Your phone.'}, 'g-band', treatment='top_band_broll',
                                    layout='top_band', assets=[{**still('b', 'c' * 64), 'type': 'broll'}])))
        self.assertIn('g-band', msg)
        self.assertIn('mediaStart', msg)

    def test_a_circle_whose_target_is_not_on_its_lines_take_refuses(self):
        msg = self.refused(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-ring', 1.5, treatment='circle',
                                    layout='overlay', target='notepad-page')))
        self.assertIn('g-ring', msg)
        self.assertIn('notepad-page', msg)

    def test_a_persistent_element_that_is_not_the_step_counter_refuses(self):
        p = counter(({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, '1/3'))
        p['treatment'] = 'recap_list'
        msg = self.refused(packet(persistent=[p]))
        self.assertIn('p-counter', msg)
        self.assertIn('unsupported', msg)

    def test_a_counter_update_the_kit_cannot_show_refuses(self):
        msg = self.refused(packet(persistent=[counter(({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, '1/3'),
                                                      ({'lineId': 'l4', 'from': 2, 'to': 2, 'words': 'fully'}, 'two done'))]))
        self.assertIn('p-counter', msg)
        self.assertIn('two done', msg)

    def test_a_counter_update_without_cue_words_refuses_with_the_counter_id(self):
        msg = self.refused(packet(persistent=[counter(({'lineId': 'l3', 'from': 7, 'to': 9}, '1/3'))]))
        self.assertIn('p-counter', msg)


class Resolve(unittest.TestCase):
    def transcript(self):
        return words(L2, .5) + words(L3, 4.2) + words(L4, 7.7, .25)

    def test_a_graphic_enters_on_the_first_frame_at_or_after_its_first_cue_words_onset(self):
        (r,) = P.resolve(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-phone')), self.transcript(), PLACE, 24)
        self.assertEqual(r['onset'], 4.5)                 # "phone" at 4.2 + 1 x 0.3
        self.assertEqual(r['startFrame'], 108)            # 4.5 s x 24, on a frame
        self.assertEqual(r['frames'], 53)                 # 2.2 s x 24 = 52.8
        out = g({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, 'g-out', 1.0)
        (r,) = P.resolve(packet(out), self.transcript(), PLACE, 24)
        self.assertEqual(r['startFrame'], 152)            # "out" at 6.3 s = frame 151.2: the first frame at or after it
        self.assertGreaterEqual(r['startFrame'] / 24, r['onset'])
        self.assertLess((r['startFrame'] - 1) / 24, r['onset'])

    def test_a_cue_word_missing_from_the_render_transcript_refuses_with_the_graphic_id(self):
        heard = [w for w in self.transcript() if w['text'] != 'phone.']
        with self.assertRaises(P.Refused) as e:
            P.resolve(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-phone')), heard, PLACE, 24)
        self.assertIn('g-phone', str(e.exception))
        self.assertIn('render transcript', str(e.exception))

    def test_a_hold_past_its_lines_take_refuses_with_the_graphic_id(self):
        with self.assertRaises(P.Refused) as e:
            P.resolve(packet(g({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, 'g-long', hold=2.5)),
                      self.transcript(), PLACE, 24)
        self.assertIn('g-long', str(e.exception))
        self.assertIn('past', str(e.exception))

    def test_a_graphic_held_until_its_lines_end_ends_on_the_cut(self):
        (r,) = P.resolve(packet(g({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, 'g-cut', hold=1.5,
                                  holdUntilLineEnd=True)), self.transcript(), PLACE, 24)
        self.assertEqual(r['startFrame'], 152)            # "out" at 4.2 + 7 x 0.3 = 6.3 s (frame 151.2)
        self.assertEqual(r['startFrame'] + r['frames'], round(7.6 * 24))

    def test_the_counter_is_one_element_from_its_first_update_to_its_lines_end_with_timed_updates(self):
        pk = packet(persistent=[counter(({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, '1/3'),
                                        ({'lineId': 'l4', 'from': 2, 'to': 2, 'words': 'fully'}, '2/3'),
                                        ({'lineId': 'l4', 'from': 12, 'to': 13, 'words': 'Just that'}, '3/3'))])
        (r,) = P.resolve(pk, self.transcript(), PLACE, 24)
        self.assertEqual(r['id'], 'p-counter')
        self.assertEqual(r['startFrame'], 152)                              # "out" 6.3 s
        self.assertEqual(r['startFrame'] + r['frames'], round(12.0 * 24))  # l4's take ends at 12.0 s
        self.assertEqual([(u['text'], u['frame']) for u in r['updates']],
                         [('1/3', 152), ('2/3', 197), ('3/3', 257)])        # "fully" 8.2 s, "Just" 10.7 s

    def test_a_counter_update_missing_from_the_render_transcript_refuses_with_the_counter_id(self):
        pk = packet(persistent=[counter(({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, '1/3'),
                                        ({'lineId': 'l4', 'from': 2, 'to': 2, 'words': 'fully'}, '2/3'))])
        heard = [w for w in self.transcript() if w['text'] != 'fully']
        with self.assertRaises(P.Refused) as e:
            P.resolve(pk, heard, PLACE, 24)
        self.assertIn('p-counter', str(e.exception))

    def test_a_circle_target_box_in_the_takes_pixels_lands_in_the_film_frame(self):
        box = P.target_box(packet()['lines'][2]['take'], 'notepad-page', 'g-ring')
        self.assertEqual([round(v) for v in box], [50, 1630, 586, 1874])


FILM3 = [('l1', 'Is your plate full? Circle one thing, then try these three hacks.'), ('l2', L2), ('l3', L3), ('l4', L4),
         ('l5', 'Now give that page a clear start. What small ritual could mark it?'),
         ('l6', 'Pick one tiny ritual, like pouring tea, and do it every time you start.'),
         ('l7', 'Phone away, one thing, same ritual. Now pour your tea and begin.')]
FILM3_PLACE = {k: {'start': v['start'], 'duration': v['duration']} for k, v in KIT['takes'].items()}


def film3_packet(*graphics):
    return {'versions': {'kit': P.PIN['kit'], 'engine': dict(P.PIN)}, 'graphics': list(graphics), 'persistent': [],
            'lines': [{'id': lid, 'text': t, 'take': take(f'take{i + 1}', str(i) * 64)} for i, (lid, t) in enumerate(FILM3)]}


def film3_heard(**onsets):
    """a render transcript of film 3: each word where the kit's captions show it, except the onsets given (word text: s)"""
    caps = P.caption_words(KIT)
    return [{'text': w, 'start': onsets.get(w, t), 'end': onsets.get(w, t) + .1} for w, t in caps]


class CaptionEntry(unittest.TestCase):
    """lead 10 Nov: a graphic enters on the first frame at or after its cue word's onset, the frame the caption switches
    to that word; never on a frame whose caption still shows the previous word"""
    def test_the_kits_captions_are_film_3s_86_words_in_order(self):
        caps = P.caption_words(KIT)
        self.assertEqual([w for w, _ in caps], ' '.join(t for _, t in FILM3).split())
        self.assertEqual(dict(caps[78:80]), {'same': 23.977, 'ritual.': 24.077})

    def test_the_pen_enters_on_the_frame_whose_caption_shows_ritual_not_same(self):
        """r3-hand run: medium.en heard "ritual." at 24.08 s; the pen entered on frame 577 (24.042 s), its caption still "same"."""
        pk = film3_packet(g({'lineId': 'l7', 'from': 5, 'to': 5, 'words': 'ritual.'}, 'g-pen', 2.0, treatment='top_band_broll',
                            layout='top_band', holdUntilLineEnd=True))
        caps = P.caption_frames(KIT)
        (r,) = P.resolve(pk, film3_heard(**{'same': 23.92, 'ritual.': 24.08}), FILM3_PLACE, 24, caps)
        self.assertEqual(r['startFrame'], 578)
        self.assertEqual(P.caption_at(caps, r['startFrame']), 'ritual.')
        self.assertEqual(P.caption_at(caps, r['startFrame'] - 1), 'same')

    def test_a_graphic_whose_caption_comes_after_the_spoken_onset_waits_for_the_caption(self):
        """2/3 on "present": heard at 11.24 s (frame 269.8 -> 270), its caption at 11.253 s (frame 271)"""
        pk = film3_packet(g({'lineId': 'l4', 'from': 3, 'to': 3, 'words': 'present'}, 'g-present', 1.5, treatment='circle',
                            layout='overlay', target='x'))
        caps = P.caption_frames(KIT)
        (r,) = P.resolve(pk, film3_heard(present=11.24), FILM3_PLACE, 24, caps)
        self.assertEqual(r['startFrame'], 271)
        self.assertEqual(P.caption_at(caps, 270), 'fully')

    def test_a_graphic_first_seen_while_the_caption_shows_the_previous_word_fails(self):
        caps = P.caption_frames(KIT)
        r = {'id': 'g-pen', 'treatment': 'top_band_broll', 'lineId': 'l7', 'from': 5, 'to': 5, 'words': 'ritual.', 'onset': 24.08,
             'startFrame': 578, 'frames': 57, 'holdSec': 2.0, 'textWords': 0, 'captionIndex': 79}
        _, failures = P.timing_table([r], {'g-pen': m(range(577, 635))}, 24, caps)
        self.assertTrue(any('g-pen' in f and "'same'" in f for f in failures), failures)
        rows, failures = P.timing_table([r], {'g-pen': m(range(578, 635))}, 24, caps)
        self.assertEqual(failures, [])
        self.assertEqual(rows[0]['captionAtFirstFrame'], 'ritual.')


class Media(unittest.TestCase):
    def test_the_same_picture_bytes_on_two_graphics_stage_and_match(self):
        """review: two graphics (or two ids) with the same bytes were refused as duplicates"""
        with tempfile.TemporaryDirectory() as d:
            for name, data in (('l2.mp4', b'two'), ('l3.mp4', b'three'), ('l4.mp4', b'four'),
                               ('g-a-still.png', b'same'), ('g-b-still.png', b'same')):
                with open(os.path.join(d, name), 'wb') as fh:
                    fh.write(data)
            same = P.sha256_bytes(b'same')
            pk = packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-a', assets=[still('still-a', same)]),
                        g({'lineId': 'l2', 'from': 0, 'to': 0, 'words': 'What'}, 'g-b', assets=[still('still-b', same)]))
            for ln, data in zip(pk['lines'], (b'two', b'three', b'four')):
                ln['take']['sha256'] = P.sha256_bytes(data)
            files = P.match_files(pk, d)
            with open(files[P.sha256_bytes(b'same')], 'rb') as fh:
                self.assertEqual(fh.read(), b'same')

    def test_a_picture_missing_from_the_shots_refuses_with_its_graphic(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(P.Refused) as e:
                P.match_files(packet(g({'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone'}, 'g-a', assets=[still()])), d)
            self.assertIn('l2', str(e.exception))


def m(shown, moving=(), **regions):
    return {'shown': list(shown), 'moving': list(moving), 'regions': {k: list(v) for k, v in regions.items()}}


class TimingTable(unittest.TestCase):
    def resolved(self, gid='g-phone', onset=4.5, start=108, treatment='photo_panel', **kw):
        return {'id': gid, 'treatment': treatment, 'lineId': 'l3', 'from': 1, 'to': 1, 'words': 'phone',
                'onset': onset, 'startFrame': start, 'frames': 53, 'holdSec': 2.2, 'textWords': 0, **kw}

    def test_hold_and_onset_come_from_the_measured_frames(self):
        (row,), failures = P.timing_table([self.resolved()], {'g-phone': m(range(108, 161), moving=range(109, 116))}, 24)
        self.assertEqual((row['measuredFirstFrame'], row['measuredLastFrame']), (108, 160))
        self.assertAlmostEqual(row['measuredHoldSec'], 53 / 24, places=5)
        self.assertEqual(row['onsetDeltaFrames'], 0)
        self.assertTrue(row['pass'])
        self.assertEqual(failures, [])

    def test_the_fully_in_place_frame_is_recorded_as_information(self):
        (row,), failures = P.timing_table([self.resolved()], {'g-phone': m(range(108, 161), moving=range(109, 116))}, 24)
        self.assertEqual(row['fullyInFrame'], 115)          # the last frame that moved
        self.assertAlmostEqual(row['fullyInAfterFirstSec'], 7 / 24, places=5)
        self.assertEqual(failures, [])

    def test_a_main_picture_shown_under_two_seconds_fails(self):
        (row,), failures = P.timing_table([self.resolved()], {'g-phone': m(range(108, 155))}, 24)   # 47 frames
        self.assertFalse(row['pass'])
        self.assertTrue(any('g-phone' in f and '2.0' in f for f in failures))

    def test_a_graphic_that_shows_after_its_cue_frame_fails(self):
        (row,), failures = P.timing_table([self.resolved()], {'g-phone': m(range(110, 163))}, 24)
        self.assertEqual(row['onsetDeltaFrames'], 2)
        self.assertTrue(any('g-phone' in f and 'onset' in f for f in failures))

    def test_a_graphic_never_seen_in_the_frames_fails(self):
        _, failures = P.timing_table([self.resolved()], {'g-phone': m([])}, 24)
        self.assertTrue(any('g-phone' in f and 'not on screen' in f for f in failures))

    def test_two_entrances_within_half_a_second_fail(self):
        a, b = self.resolved('g-a', 4.5, 108), self.resolved('g-b', 4.8, 115)
        _, failures = P.timing_table([a, b], {'g-a': m(range(108, 161)), 'g-b': m(range(115, 168))}, 24)
        self.assertTrue(any('g-a' in f and 'g-b' in f and '0.5' in f for f in failures))

    def test_text_on_a_picture_must_read_one_second_plus_point_three_a_word(self):
        """r3's R2b: 'Today' + '+9 more' = 3 words -> 1.9 s"""
        r = self.resolved(textWords=3)
        (row,), failures = P.timing_table([r], {'g-phone': m(range(108, 152))}, 24)        # 44 frames = 1.833 s
        self.assertTrue(any('g-phone' in f and '1.9' in f for f in failures))

    def counter(self):
        ups = [{'text': '1/3', 'frame': 151, 'onset': 151 / 24, 'lineId': 'l3', 'words': 'out of reach.'},
               {'text': '2/3', 'frame': 196, 'onset': 196 / 24, 'lineId': 'l4', 'words': 'fully'},
               {'text': '3/3', 'frame': 256, 'onset': 256 / 24, 'lineId': 'l4', 'words': 'Just that'}]
        return {'id': 'p-counter', 'treatment': 'step_counter', 'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.',
                'onset': 151 / 24, 'startFrame': 151, 'frames': 149, 'holdSec': 149 / 24, 'textWords': 1, 'updates': ups}

    def test_each_counter_state_is_measured_where_its_number_changes_and_holds_until_the_next(self):
        rows, failures = P.timing_table([self.counter()], {'p-counter': m(range(151, 300), number=[151, 152, 196, 197, 256, 257])}, 24)
        self.assertEqual(failures, [])
        states = [r for r in rows if r.get('update')]
        self.assertEqual([(r['update'], r['measuredFirstFrame'], r['onsetDeltaFrames']) for r in states],
                         [('1/3', 151, 0), ('2/3', 196, 0), ('3/3', 256, 0)])
        self.assertEqual([r['measuredLastFrame'] for r in states], [195, 255, 299])

    def test_a_counter_state_that_reads_under_one_and_a_half_seconds_fails(self):
        c = self.counter()
        c['updates'][1]['frame'] = 185                       # 2/3 34 frames after 1/3
        _, failures = P.timing_table([c], {'p-counter': m(range(151, 300), number=[151, 185, 186, 256])}, 24)
        self.assertTrue(any('p-counter' in f and '1/3' in f and '1.5' in f for f in failures), failures)

    def test_a_counter_update_that_changes_late_fails(self):
        _, failures = P.timing_table([self.counter()], {'p-counter': m(range(151, 300), number=[151, 199, 256])}, 24)
        self.assertTrue(any('2/3' in f and 'onset' in f for f in failures), failures)

    def test_a_pulse_is_measured_while_its_slot_glows_and_a_short_one_fails(self):
        c = self.counter()
        c['updates'].insert(2, {'text': 'pulse', 'frame': 210, 'onset': 8.76, 'lineId': 'l4', 'words': 'present'})
        rows, failures = P.timing_table([c], {'p-counter': m(range(151, 300), number=[151, 196, 256], slot3=range(210, 252))}, 24)
        (pulse,) = [r for r in rows if r.get('update') == 'pulse']
        self.assertEqual((pulse['measuredFirstFrame'], pulse['measuredLastFrame'], pulse['onsetDeltaFrames']), (210, 251, 0))
        self.assertEqual(failures, [])
        _, failures = P.timing_table([c], {'p-counter': m(range(151, 300), number=[151, 196, 256], slot3=range(210, 240))}, 24)
        self.assertTrue(any('pulse' in f and '1.5' in f for f in failures), failures)

    def test_a_pulse_ends_before_the_next_update(self):
        c = {**self.counter(), 'startFrame': 151}
        c['updates'] = [c['updates'][0], {'text': 'pulse', 'frame': 210}, {'text': '2/3', 'frame': 250}]
        piece = P.counter_piece(KIT, c, 24)
        self.assertIn('pulse(2, 2.416667, 1.708333)', piece)     # starts a frame early (209), gone by frame 250


class Build(unittest.TestCase):
    def test_a_host_starts_just_before_its_first_frame_so_its_first_frame_shows_it(self):
        """a host starting exactly on a frame's time did not apply its time-0 sets on that frame (r3-hand run 1: circle +1)"""
        start = float(re.search(r'data-start="([\d.]+)"', P.host('g', 'c.html', 300, 45, 24, 2)).group(1))
        self.assertTrue(299.9 < start * 24 < 300, start)

    def test_the_counter_is_built_as_one_element_with_its_updates_timed_inside_it(self):
        pk = packet(persistent=[counter(({'lineId': 'l3', 'from': 7, 'to': 9, 'words': 'out of reach.'}, '1/3'),
                                        ({'lineId': 'l4', 'from': 2, 'to': 2, 'words': 'fully'}, '2/3'),
                                        ({'lineId': 'l4', 'from': 12, 'to': 13, 'words': 'Just that'}, '3/3'))])
        resolved = P.resolve(pk, Resolve.transcript(None), PLACE, 24)
        with tempfile.TemporaryDirectory() as d:
            files = {}
            for ln in pk['lines']:
                files[ln['take']['sha256']] = os.path.join(d, ln['id'] + '.mp4')
                with open(files[ln['take']['sha256']], 'wb') as fh:
                    fh.write(b'x')
            kit = {**KIT, 'takes': {k: {**v, 'mediaStart': 0} for k, v in PLACE.items()}, 'duration': 12.0}
            P.build(pk, files, resolved, kit, os.path.join(d, 'proj'), os.path.join(d, 'masks'))
            with open(os.path.join(d, 'proj', 'index.html')) as fh:
                self.assertEqual(fh.read().count('data-composition-id="p-counter"'), 1)
            with open(os.path.join(d, 'proj', 'compositions', 'p-counter.html')) as fh:
                piece = fh.read()
            self.assertEqual(re.findall(r'step\((\d), ([\d.]+)\)', piece), [('2', '1.875000'), ('3', '4.375000')])   # 197-152, 257-152


class Entry(unittest.TestCase):
    def run_packet(self, pk):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'packet.json')
            with open(path, 'w') as fh:
                json.dump(pk, fh)
            os.makedirs(os.path.join(d, 'shots'))
            return subprocess.run([sys.executable, '-B', os.path.join(ROOT, 'scripts', 'render_packet.py'), path, '--shots',
                                   os.path.join(d, 'shots'), '--out', os.path.join(d, 'out')], capture_output=True, text=True)

    def test_render_packet_refuses_a_planned_packet_with_a_bad_cue_before_any_media_work(self):
        p = self.run_packet(packet(g({'lineId': 'l3', 'words': 'tablet'}, 'g-tablet')))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn('g-tablet', p.stderr)

    def test_a_graphic_without_a_cue_refuses_naming_the_graphic(self):
        gr = g({}, 'g-nocue')
        del gr['cue']
        p = self.run_packet(packet(gr))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn('g-nocue', p.stderr)
        self.assertNotIn('cannot read the packet', p.stderr)

    def test_a_packet_pinned_to_another_engine_build_refuses(self):
        pk = packet()
        pk['versions']['engine'] = {**P.PIN, 'hyperframes': '0.8.34'}
        with self.assertRaises(P.Refused) as e:
            P.check_pin(pk)
        self.assertIn('0.8.34', str(e.exception))


if __name__ == '__main__':
    unittest.main()
