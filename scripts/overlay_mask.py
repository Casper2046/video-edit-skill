#!/usr/bin/env python3
"""Spec mode: the overlay mask. The film's page is rendered a second time with only its overlays (overlays_page), as an
RGBA PNG sequence, alongside the film render (make_reel.py). Its alpha is the renderer's own; the overlays page only
changes colours: captions are drawn in CAPTION_TAG and every text shadow in SHADOW_TAG, alpha unchanged. Per frame, the
boxes are the 8-connected regions, MIN_AREA pixels or more, of the pixels with alpha over ALPHA (32), split four ways:
  ink           every overlay pixel but text shadows (card and chip backgrounds and box shadows are ink)
  caption       caption ink (CAPTION_TAG)
  halo          every overlay pixel, text shadows included
  caption_halo  caption ink and text shadows

Usage:  PY overlay_mask.py <overlay_frames_dir>     prints the boxes per frame as JSON
Boxes are [x0, y0, x1, y1] in 1080x1920 frame pixels, half-open (x1, y1 one past the last pixel). frame_000001.png is
frame 0, at 0s.
"""
import json
import os
import re
import sys

import numpy as np

FPS = 30
ALPHA = 32       # a pixel is overlay when its alpha is over this (ticket 01 mask decision)
MIN_AREA = 16    # regions smaller than this are shadow-fringe specks (1-4px on a held-out episode's render), not text
CAPTION_TAG, SHADOW_TAG = '#f00', (0, 255, 0)   # colours in the overlays page; alpha is unchanged


def components(m):
    """8-connected regions of a bool mask with at least MIN_AREA pixels -> [[x0, y0, x1, y1]], from runs per row"""
    parent, box, prev, prev_y = [], [], [], -2

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for y in np.flatnonzero(m.any(1)).tolist():
        d = np.diff(m[y].astype(np.int8), prepend=0, append=0)
        cur, above = [], prev if prev_y == y - 1 else []
        for a, b in zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()):
            r = None
            for pa, pb, pl in above:            # touching the row above, diagonals included
                if pa <= b and pb >= a:
                    q = root(pl)
                    if r is None:
                        r = q
                    elif q != r:
                        parent[q] = r
                        bq, br = box[q], box[r]
                        box[r] = [min(bq[0], br[0]), min(bq[1], br[1]), max(bq[2], br[2]), max(bq[3], br[3]), bq[4] + br[4]]
            if r is None:
                r = len(parent)
                parent.append(r)
                box.append([a, y, b, y + 1, 0])
            bx = box[r]
            bx[0], bx[2], bx[3], bx[4] = min(bx[0], a), max(bx[2], b), y + 1, bx[4] + b - a
            cur.append((a, b, r))
        prev, prev_y = cur, y
    return [box[i][:4] for i in range(len(parent)) if parent[i] == i and box[i][4] >= MIN_AREA]


def dominant(rgb, c):
    """pixels whose channel c stands clear of the other two: the tag colours"""
    rest = np.delete(rgb, c, axis=-1).max(-1)
    return rgb[..., c] > 2 * rest + 32


def boxes(rgba):
    """{'ink', 'caption', 'halo', 'caption_halo'} regions of one RGBA frame (see the module docstring)"""
    a = rgba[..., 3] > ALPHA
    rows = np.flatnonzero(a.any(1))
    if not len(rows):
        return {'ink': [], 'caption': [], 'halo': [], 'caption_halo': []}
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    a, rgb = a[y0:y1], rgba[y0:y1, :, :3].astype(np.int16)
    red, green = a & dominant(rgb, 0), a & dominant(rgb, 1)

    def regions(m):
        return [[x0, y + y0, x1, v + y0] for x0, y, x1, v in components(m)]
    return {'ink': regions(a & ~green), 'caption': regions(red), 'halo': regions(a), 'caption_halo': regions(red | green)}


def read(d):
    """[{'t', 'ink', 'caption', 'halo', 'caption_halo'}] for every frame_NNNNNN.png in the overlay render's folder, in order"""
    from PIL import Image
    names = sorted(f for f in os.listdir(d) if re.fullmatch(r'frame_\d+\.png', f))
    out = []
    for i, f in enumerate(names):
        with Image.open(os.path.join(d, f)) as im:
            out.append({'t': i / FPS, **boxes(np.asarray(im.convert('RGBA')))})
    return out


def overlays_page(page):
    """the film's page with only its overlays: no audio or video, a transparent background, the look's layers hidden,
    the captions (ids w<sentence>-<chunk>) in CAPTION_TAG and every text shadow's rgba() colour SHADOW_TAG, alpha kept.
    Rendered from work/
    (an extra page in the project root fails HyperFrames' lint); asset paths stay root-relative, as HyperFrames serves
    every page with the project root as its base"""
    page = re.sub(r'[ \t]*<audio\b[^>]*>\s*</audio>\n?', '', page)
    page = re.sub(r'<video\b[^>]*>\s*</video>', '<i class="g"></i>', page)     # the grade's GSAP target stays
    tag = 'rgba({},{},{},'.format(*SHADOW_TAG)
    page = re.sub(r'text-shadow:[^;}]*', lambda m: re.sub(r'rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,', tag, m.group(0)), page)
    css = ('html,body,#root{background:transparent !important}.fx{display:none !important}'
           f'.dw[id^="w"]{{color:{CAPTION_TAG} !important}}')
    return page.replace('</head>', f'<style>{css}</style>\n</head>', 1)


if __name__ == '__main__':
    json.dump(read(sys.argv[1]), sys.stdout)
