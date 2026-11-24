from __future__ import annotations

import urllib.request
from pathlib import Path

import cv2

from .models import CropKey, CropPlan, MediaInfo, VideoType

_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
_MODEL_PATH = Path(__file__).parent / "assets" / "blaze_face_short_range.tflite"


def _ensure_model() -> Path:
    if not _MODEL_PATH.exists():
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MODEL_PATH.with_name(_MODEL_PATH.name + ".tmp")
        urllib.request.urlretrieve(_MODEL_URL, tmp)
        tmp.replace(_MODEL_PATH)
    return _MODEL_PATH


def crop_width(src_h: int) -> int:
    w = round(src_h * 9 / 16)
    return w + (w % 2)


def smooth_track(
    samples: list[tuple[float, float | None]],
    src_w: int,
    crop_w: int,
    cuts: list[float],
    dead_zone: float = 0.08,
    alpha: float = 0.15,
) -> list[CropKey]:
    first = next((x for _, x in samples if x is not None), src_w / 2)
    pos = target = first
    keys: list[CropKey] = []
    prev_t = float("-inf")
    for t, face in samples:
        at_cut = any(prev_t < c <= t for c in cuts)
        if face is not None and (at_cut or abs(face - target) > dead_zone * src_w):
            target = face
        if at_cut:
            pos = target
        else:
            pos += alpha * (target - pos)
        x = min(max(round(pos - crop_w / 2), 0), src_w - crop_w)
        keys.append(CropKey(t, x))
        prev_t = t
    return keys


def detect_faces(source: Path, start: float, end: float, fps: float = 5) -> list[tuple[float, float | None]]:
    from mediapipe import Image, ImageFormat
    from mediapipe.tasks.python import BaseOptions, vision

    opts = vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(_ensure_model()), delegate=BaseOptions.Delegate.CPU),
    )
    cap = cv2.VideoCapture(str(source))
    out: list[tuple[float, float | None]] = []
    try:
        with vision.FaceDetector.create_from_options(opts) as det:
            t = start
            while t < end:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if not ok:
                    break
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                res = det.detect(Image(ImageFormat.SRGB, rgb))
                boxes = [d.bounding_box for d in res.detections]
                big = max(boxes, key=lambda b: b.width * b.height, default=None)
                out.append((t - start, big.origin_x + big.width / 2 if big else None))
                t += 1 / fps
    finally:
        cap.release()
    return out


def scene_cuts(source: Path, start: float, end: float) -> list[float]:
    from scenedetect import ContentDetector, detect

    scenes = detect(str(source), ContentDetector(), start_time=start, end_time=end)
    return [s[0].get_seconds() - start for s in scenes[1:]]


def plan_crop(source: Path, start: float, end: float, video_type: VideoType, info: MediaInfo) -> CropPlan:
    w, h = info.width, info.height
    if video_type == "gaming" or w / h <= 1.0:
        return CropPlan("fit", w, h, [])
    cuts = scene_cuts(source, start, end) if video_type == "movie" else []
    return CropPlan("follow", w, h, smooth_track(detect_faces(source, start, end), w, crop_width(h), cuts))
