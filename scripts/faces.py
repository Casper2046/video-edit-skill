#!/usr/bin/env python3
"""Spec mode: where the speaker's chin is in each shot, so captions can start below it without a hand-set caption_top.

Usage:  PY faces.py <project_dir>
Reads sources.json; writes faces.json {"c1": {"face_bottom": 987, "box": [x0, y0, x1, y1], "frames": 52, "sampled": 56,
"samples": [{"t": 0.0, "face_bottom": 981}, ...]}, ...} in 1080x1920 frame coordinates (the same cover-crop assemble.py
uses). Every STEP-th frame of the shot is sampled (5 frames a shot missed a lean-in); "samples" keeps each one, with
face_bottom None where no face was found, so reel_check can hold every caption FACE_MARGIN under every sampled frame.
face_bottom is the lowest face bottom over the samples: the face box's bottom, or 60px under a landmark chin estimate if that is lower
(on 4 test shots YuNet's box ended 48-96px below the landmark chin estimate, near the hair ends; the landmark guard
keeps captions off the face if a box ever stops short). The move-up fallback in build_reel.py (tight platforms) can
still lift a caption block over the face; build-report.json warns when it does. Face detector: YuNet (OpenCV model zoo, MIT), run with onnxruntime on the CPU;
the model is downloaded once into the skill's .models folder and checked against a pinned sha256.
"""
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillenv  # noqa: E402

CHIN_CLEAR = 60   # px kept clear under the landmark chin estimate
FACE_MARGIN = 20  # px between the lowest face bottom and the caption top (0px let captions touch the jaw)
CHEST_TOP = 940   # captions never start higher than the chest band (references/layout.md)
LINE = 86         # one small caption line must still fit above the safe bottom
STEP = 3          # sample every 3rd frame
MIN_SCORE = .6
MODEL = ('face_detection_yunet_2023mar.onnx', '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4',
         'https://media.githubusercontent.com/media/opencv/opencv_zoo/f12e12798e8314f7c074a6656816c048dcc95b7a/'
         'models/face_detection_yunet/face_detection_yunet_2023mar.onnx')
MODELS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.models')
W, H, SIDE = 1080, 1920, 640   # frame, and the detector's square input (the frame is scaled by 1/3 into it)


def caption_top(face_bottom, platform):
    """first caption line's top for a shot whose lowest face bottom is at y `face_bottom` (None = no face found)"""
    from build_reel import PLATFORMS
    top = CHEST_TOP if face_bottom is None else max(round(face_bottom) + FACE_MARGIN, CHEST_TOP)
    return min(top, H - PLATFORMS[platform]['bottom'] - LINE)


def sample_frames(n, step=STEP):
    """indexes of the frames sampled in an n-frame shot"""
    return list(range(0, n, step))


def face_bottom(box, kps):
    """box [x0, y0, x1, y1]; kps: right eye, left eye, nose tip, right and left mouth corner as [x, y].
    Chin estimate = mouth + (mouth - nose): the lower face is about twice the nose-to-mouth drop."""
    mouth = (kps[3][1] + kps[4][1]) / 2
    return max(box[3], mouth + (mouth - kps[2][1]) + CHIN_CLEAR)


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def ensure_model(dst=MODELS, download=_get):
    """path to the pinned YuNet model, downloading it once; bytes that do not match the pinned sha256 are never saved"""
    name, sha, url = MODEL
    path = os.path.join(dst, name)
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            if hashlib.sha256(fh.read()).hexdigest() == sha:
                return path
    data = download(url)
    if hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError(f'{name} from {url} does not match the pinned sha256 {sha}: not saved')
    os.makedirs(dst, exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def detect(session, bgr):
    """best face in a 640x640 BGR uint8 image -> (score, [x0, y0, x1, y1], [[x, y] * 5]) in that image, or None.
    YuNet 2023mar head decoding as in OpenCV's FaceDetectorYN (strides 8/16/32, score = sqrt(cls * obj))."""
    import numpy as np
    blob = bgr.astype(np.float32).transpose(2, 0, 1)[None]
    names = [o.name for o in session.get_outputs()]
    out = dict(zip(names, session.run(None, {session.get_inputs()[0].name: blob})))
    best = None
    for s in (8, 16, 32):
        n = SIDE // s
        cls, obj, box = out[f'cls_{s}'].reshape(-1), out[f'obj_{s}'].reshape(-1), out[f'bbox_{s}'].reshape(-1, 4)
        kps = out[f'kps_{s}'].reshape(-1, 10)
        score = np.sqrt(np.clip(cls, 0, 1) * np.clip(obj, 0, 1))
        i = int(score.argmax())
        if best is None or score[i] > best[0]:
            r, c = divmod(i, n)
            cx, cy = (c + box[i, 0]) * s, (r + box[i, 1]) * s
            w, h = np.exp(box[i, 2]) * s, np.exp(box[i, 3]) * s
            best = (float(score[i]), [float(cx - w / 2), float(cy - h / 2), float(cx + w / 2), float(cy + h / 2)],
                    [[float((kps[i, 2 * j] + c) * s), float((kps[i, 2 * j + 1] + r) * s)] for j in range(5)])
    return best if best and best[0] >= MIN_SCORE else None


def frames(f, step=STEP):
    """[(t, 640x640 BGR input)] for the sampled frames (sample_frames): cover-cropped to 1080x1920 like assemble.py,
    scaled 1/3 and padded into the detector's square input. One decode per shot."""
    import numpy as np
    rate = subprocess.run([skillenv.tool('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=r_frame_rate',
                           '-of', 'csv=p=0', f], **skillenv.TEXT).stdout.strip().split(',')[0]
    num, den = (rate.split('/') + ['1'])[:2]
    fps = float(num) / float(den)
    raw = subprocess.run([skillenv.tool('ffmpeg'), '-loglevel', 'error', '-i', f, '-vf',
                          'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,scale=360:640',
                          '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], capture_output=True, check=True).stdout
    imgs = np.frombuffer(raw, np.uint8).reshape(-1, 640, 360, 3)
    out = []
    for i in sample_frames(len(imgs), step):
        pad = np.zeros((SIDE, SIDE, 3), np.uint8)
        pad[:, :360] = imgs[i]
        out.append((round(i / fps, 3), pad))
    return out


def main():
    import onnxruntime as ort
    skillenv.utf8_stdio()
    proj = skillenv.path_arg(sys.argv[1])
    with open(os.path.join(proj, 'sources.json'), encoding='utf-8') as fh:
        src = json.load(fh)
    session = ort.InferenceSession(ensure_model(), providers=['CPUExecutionProvider'])
    result = {}
    for cid, f in src.items():
        shot = frames(f)
        hits = [(t, detect(session, img)) for t, img in shot]
        found = [(t, [v * 3 for v in h[1]], [[x * 3, y * 3] for x, y in h[2]]) for t, h in hits if h]
        samples = {t: None for t, _ in hits}
        for t, b, k in found:
            samples[t] = round(face_bottom(b, k))
        if not found:
            result[cid] = {'face_bottom': None, 'box': None, 'frames': 0, 'sampled': len(hits),
                           'samples': [{'t': t, 'face_bottom': None} for t in samples]}
            print(f'{cid}  no face found in {len(hits)} frames')
            continue
        _, b, k = max(found, key=lambda x: samples[x[0]])
        mouth = (k[3][1] + k[4][1]) / 2
        bottoms = [v for v in samples.values() if v is not None]
        result[cid] = {'face_bottom': max(bottoms), 'box': [round(v) for v in b], 'chin_estimate': round(2 * mouth - k[2][1]),
                       'frames': len(found), 'sampled': len(hits), 'samples': [{'t': t, 'face_bottom': v} for t, v in samples.items()]}
        missed = [t for t, v in samples.items() if v is None]
        print(f'{cid}  face bottom y{max(bottoms)}  box {result[cid]["box"]}  landmark chin y{result[cid]["chin_estimate"]}  '
              f'({len(found)}/{len(hits)} frames, range {min(bottoms)}-{max(bottoms)})' + (f'  no face at {missed}' if missed else ''))
    with open(os.path.join(proj, 'faces.json'), 'w', encoding='utf-8') as fh:
        json.dump(result, fh, indent=1)


if __name__ == '__main__':
    main()
