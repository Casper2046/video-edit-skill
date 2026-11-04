#!/usr/bin/env python3
"""Spec mode: a shot's first clean frame (clean_from) from the footage itself, for edl_from_words' opening rule.

Usage:  PY openings.py <video> [<video> ...]
Prints one line per file: name, clean_from in seconds (or -), and the note saying why.

An unclean opening is a soft-edged (out-of-focus) foreground object over the first frames that then leaves; on a
labelled shot, clean from 0.75s by frame review. On that shot the whole-frame
sharpness (variance of the Laplacian) stays within 6% of the shot's median through the unclean frames, so the rule
looks at tiles and at how far each tile is from the shot's own settled picture:
- frames are read grey at 360x640 and split into an 8x6 tile grid; the settled picture is the shot's per-pixel median;
- dev[f, t] = mean |frame f - median| over tile t, divided by the shot-wide median of dev (all frames, all tiles);
- soft[t] = |Laplacian| / |gradient| of (frame 0 - median) over tile t, divided by the same ratio on the shot's own
  frames (median over tiles and frames): below 1 the difference has softer edges than the shot's in-focus content;
- a tile is unclean at the opening when dev[0] >= START, soft[t] < SOFT and dev[0] >= DECAY x its largest dev over the
  first 0.5s (the object is already there and leaving, not arriving: a hand coming into shot is action);
- clean_from = the first frame from which every such tile stays at dev <= SETTLED for SETTLE_FRAMES frames, searched up
  to MAX_OPENING. Nothing unclean, or nothing settling in time: None, and the caller keeps the shot's start.
Thresholds come from a held-out episode's labelled shots (one unclean): the unclean shot's worst tile has dev 8.8 and
soft 0.60 at frame 0. The softest frame-0 tile at dev >= START on a clean shot has soft 0.72 and dev 9.1-10.1 for the
whole 1.5s: the soft gate lets it through and the settle rejects it. The unclean shot settles to dev 1.8 / 1.3 / 0.8 at
frames 16 / 18 / 20: SETTLED 0.9-1.8 lands frames 20-16, within 2 frames of the 0.75s label (frame 18); 1.2 sits in
the middle (frame 19).
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402

W, H = 360, 640           # grey read size (the 720x1280 shots at half size)
ROWS, COLS = 8, 6         # tile grid (at 16x12, tiles on two clean labelled shots measured soft 0.66-0.74)
START = 4.0               # an unclean tile's frame-0 deviation, x the shot-wide median deviation
SOFT = 0.75               # ... and its edge sharpness, x the shot's own
DECAY = 0.75              # ... and frame 0 is at least this share of its largest deviation over the first 0.5s
SETTLED = 1.2             # settled: deviation back to this x the shot-wide median
SETTLE_FRAMES = 3         # ... for this many frames in a row
MAX_OPENING = 1.5         # seconds searched for the settle (an opening, not the shot's content)


def grey_frames(path):
    """all frames as one (n, H, W) uint8 array, one decode"""
    import numpy as np
    raw = subprocess.run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-i', path, '-vf', f'scale={W}:{H}',
                          '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W)


def _tiles(a):
    """mean of each tile of a 2-D array -> (ROWS, COLS)"""
    h, w = a.shape[0] // ROWS, a.shape[1] // COLS
    return a[:h * ROWS, :w * COLS].reshape(ROWS, h, COLS, w).mean(axis=(1, 3))


def _edges(a):
    """per-tile |Laplacian| and |gradient| of a 2-D float array"""
    import numpy as np
    c = a[1:-1, 1:-1]
    lap = np.abs(a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * c)
    grad = np.hypot(a[1:-1, 2:] - a[1:-1, :-2], a[2:, 1:-1] - a[:-2, 1:-1]) / 2
    return _tiles(lap), _tiles(grad)


def measure(frames):
    """(dev (n, ROWS, COLS) relative to the shot-wide median, soft (ROWS, COLS) of frame 0's difference)"""
    import numpy as np
    med = np.median(frames, axis=0).astype(np.float32)
    dev = np.stack([_tiles(np.abs(f.astype(np.float32) - med)) for f in frames])
    dev /= max(float(np.median(dev)), 1e-6)
    lap, grad = _edges(frames[0].astype(np.float32) - med)
    own = np.median([(lambda l, g: l / (g + 1e-6))(*_edges(f.astype(np.float32))) for f in frames[::4]])
    return dev, lap / (grad + 1e-6) / own


def clean_from(video_path):
    """(seconds of the first clean frame, note), or (None, note) when the opening is clean or never settles"""
    import numpy as np
    rate, frames = skillenv.video_fps(video_path), grey_frames(video_path)
    dev, soft = measure(frames)
    early = dev[:max(1, round(.5 * rate))].max(axis=0)
    hot = (dev[0] >= START) & (soft < SOFT) & (dev[0] >= DECAY * early)
    if not hot.any():
        r, c = np.unravel_index(dev[0].argmax(), dev[0].shape)
        return None, (f'clean opening: no soft-edged region over frame 0 (largest frame-0 deviation {dev[0][r, c]:.1f}x '
                      f'the shot median, tile r{r}c{c}, edge sharpness {soft[r, c]:.2f}x the shot\'s)')
    where = ', '.join(f'r{r}c{c}' for r, c in zip(*np.nonzero(hot)))
    worst = dev[:, hot].max(axis=1)
    calm = worst <= SETTLED
    last = min(len(frames), int(MAX_OPENING * rate) + 1)
    for f in range(1, last):
        if calm[f:f + SETTLE_FRAMES].all() and len(calm[f:f + SETTLE_FRAMES]) == SETTLE_FRAMES:
            return round(f / rate, 3), (f'soft-edged object over the opening (tiles {where}, frame-0 deviation '
                                        f'{worst[0]:.1f}x the shot median) settled at frame {f}')
    return None, (f'no unclean opening found: a soft-edged region over frame 0 (tiles {where}, {worst[0]:.1f}x the shot '
                  f'median) does not settle within {MAX_OPENING}s, so it is read as the shot\'s content')


def main():
    skillenv.utf8_stdio()
    for p in sys.argv[1:]:
        t, note = clean_from(skillenv.path_arg(p))
        print(f'{os.path.basename(p)}  {"-" if t is None else f"{t:.3f}"}  {note}')


if __name__ == '__main__':
    main()
