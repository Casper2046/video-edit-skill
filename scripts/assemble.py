#!/usr/bin/env python3
"""Cut the reel straight from the originals into <project>/assets/aroll.mp4 (1080x1920, clean 30fps CFR from t=0).

Usage:  PY assemble.py <project_dir> [--end-hold SECONDS]     (PY = the skill's Python, see SKILL.md; any Python 3 works)
--end-hold freezes the last frame (with silence) for that long after the last segment, room for an end card.
Needs <project>/sources.json (from ingest.py) and <project>/edl.json:
  [{"id": "hook", "clip": "c1", "in": 12.66, "out": 15.72, "zoom": 1.0, "cx": 0.5, "cy": 0.5, "line": "Everybody's..."}, ...]
  zoom > 1 = punch-in cropped from the original (sharp when the source is 4K), centred on (cx, cy) as frame fractions.
Writes segments.json with each segment's first frame in the cut ("frame") and frame count ("frames"). Every segment is
a whole number of frames and its sound is padded/trimmed to exactly that length, so picture and sound never drift
apart across the joins; src_out is the in point + frames/30. A segment that continues the previous one's take with no
gap gets no fades at that join. The mix is limited to -3dBFS.

Why the CFR re-encode: ffmpeg concat leaves a 0.033s video start offset and timestamp gaps; `hyperframes remove-background`
then duplicates frames and the cutout drifts out of sync with the base video by the end. setpts=N/30 fixes it.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402

FF, FP = skillenv.tool('ffmpeg'), skillenv.tool('ffprobe')


def size(f):
    w, h = subprocess.run([FP, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', f],
                          **skillenv.TEXT).stdout.strip().split(',')[:2]
    return int(w), int(h)


def crop(z, cx, cy, sw, sh):
    if z <= 1:
        return ''
    w, h = round(sw / z / 2) * 2, round(sh / z / 2) * 2
    x = min(max(round(cx * sw - w / 2), 0), sw - w)
    y = min(max(round(cy * sh - h / 2), 0), sh - h)
    return f'crop={w}:{h}:{x}:{y},'


def length(f):
    """a shot's length: picture or sound, whichever runs longer (generated shots often carry sound past the last frame)"""
    out = subprocess.run([FP, '-v', 'error', '-show_entries', 'stream=duration', '-of', 'csv=p=0', f], **skillenv.TEXT).stdout
    return max(float(v) for v in out.split() if v.replace('.', '', 1).isdigit())


def frames(f):
    return int(subprocess.run([FP, '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries', 'stream=nb_read_frames',
                               '-of', 'csv=p=0', f], **skillenv.TEXT).stdout.strip().split(',')[0])


def main():
    skillenv.utf8_stdio()
    proj = skillenv.path_arg(sys.argv[1])
    hold = float(sys.argv[sys.argv.index('--end-hold') + 1]) if '--end-hold' in sys.argv else 0.0
    pad_v, pad_a = (f',tpad=stop_mode=clone:stop_duration={hold:.3f}', f',apad=pad_dur={hold:.3f}') if hold > 0 else ('', '')
    work, assets = os.path.join(proj, 'work'), os.path.join(proj, 'assets')
    with open(os.path.join(proj, 'sources.json'), encoding='utf-8') as fh:
        src = json.load(fh)
    with open(os.path.join(proj, 'edl.json'), encoding='utf-8') as fh:
        edl = json.load(fh)
    os.makedirs(work, exist_ok=True)
    os.makedirs(assets, exist_ok=True)
    aroll = os.path.join(assets, 'aroll.mp4')
    parts, meta = [], []
    for k, s in enumerate(edl):
        f = src[s['clip']]
        sw, sh = size(f)
        prev = edl[k - 1] if k else None
        joined = prev is not None and prev['clip'] == s['clip'] and abs(prev['out'] - s['in']) < .001   # same take, no jump
        if joined:
            s = {**s, 'in': meta[-1]['src_out']}
        n = max(1, min(round((s['out'] - s['in']) * 30), int((length(f) - s['in']) * 30 + 1e-6)))   # never past the shot's end
        nxt = edl[k + 1] if k + 1 < len(edl) else None
        fade_out = not (nxt and nxt['clip'] == s['clip'] and abs(s['out'] - nxt['in']) < .001)
        out = os.path.join(work, f'seg_{k}.mp4')
        # vertical fill: scale to cover 1080x1920 then centre-crop (handles 4K 9:16 and odd phone sizes)
        # Picture frame k must show the source at in + k/30, the same moment as the sound. Seek a whole number of 30fps
        # frames early, lay the 30fps grid from the seek point (fps start_time=0), then drop exactly those frames and that
        # much sound. Seeking straight to a 24fps in point made fps=30 start at frame 1 (a 9-shot test run: picture one frame
        # ahead on 11 of 17 segments).
        pre = min(6, int(s['in'] * 30 + 1e-6)) / 30
        seek = s['in'] - pre
        vf = (f"{crop(s.get('zoom', 1), s.get('cx', .5), s.get('cy', .5), sw, sh)}"
              'scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,'
              'tpad=stop_mode=clone:stop_duration=0.2,'      # the last frame holds while the sound finishes
              f'fps=30:start_time=0,trim=start_frame={round(pre * 30)},setpts=PTS-STARTPTS')
        while True:
            d = n / 30               # picture and sound both exactly n frames long: no drift at the joins
            fades = []               # no clicks at the jump cuts; a shot's first frame or a same-take join needs no fade
            if not joined and s['in'] >= .005:
                fades.append('afade=t=in:d=0.02')
            if fade_out:
                fades.append(f'afade=t=out:st={d - 0.03:.4f}:d=0.03')
            af = ','.join([f'atrim=start={pre:.6f}', 'asetpts=PTS-STARTPTS', *fades, f'apad,atrim=end={d:.6f}'])
            subprocess.run([FF, '-loglevel', 'error', '-y', '-ss', f'{seek:.6f}', '-t', f'{d + pre + .1:.3f}', '-i', f,
                            '-map', '0:v:0', '-map', '0:a:0', '-vf', vf, '-frames:v', str(n), '-af', af,
                            '-c:v', 'libx264', '-crf', '14', '-preset', 'medium', '-pix_fmt', 'yuv420p',
                            '-c:a', 'pcm_s16le', '-ar', '48000', out], check=True)
            got = frames(out)
            if got == n:
                break
            n = got                  # the shot ran out of frames: cut the sound to what the picture has
        parts.append(out)
        meta.append({**s, 'src_in': s['in'], 'src_out': round(s['in'] + d, 6)})
    # the list holds bare file names (resolved next to concat.txt), so no drive letters or backslashes end up in it
    concat = os.path.join(work, 'concat.txt')
    with open(concat, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(''.join(skillenv.concat_entry(os.path.basename(p)) for p in parts))
    subprocess.run([FF, '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', concat,
                    '-vf', 'setpts=N/30/TB' + pad_v, '-r', '30', '-fps_mode', 'cfr', '-af', 'asetpts=PTS-STARTPTS,alimiter=limit=0.7:level=false' + pad_a,
                    '-c:v', 'libx264', '-crf', '14', '-preset', 'medium', '-g', '30', '-keyint_min', '30', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '192k', aroll], check=True)
    fr = 0
    for k, m in enumerate(meta):
        n = frames(parts[k])
        m['frame'], m['frames'] = fr, n
        fr += n
    with open(os.path.join(proj, 'segments.json'), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=1)
    for m in meta:
        print(f"frame {m['frame']:4d} ({m['frame'] / 30:6.3f}s) +{m['frames']:3d}f  {m['id']:8s} {m['clip']} {m['in']}-{m['out']}  {m.get('line', '')}")
    total = frames(aroll)
    want = fr + round(hold * 30)
    print(f'total frames {fr} + end hold {want - fr} (aroll.mp4 has {total})' + ('' if total == want else '  << MISMATCH, investigate'))


if __name__ == '__main__':
    main()
