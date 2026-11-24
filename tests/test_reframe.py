import pytest

from clipper.ingest import probe
from clipper.models import CropPlan
from clipper.reframe import crop_width, detect_faces, plan_crop, smooth_track


def test_crop_width_is_even_9_16():
    assert crop_width(1080) == 608
    assert crop_width(720) % 2 == 0


def test_smooth_track_ignores_jitter_inside_dead_zone():
    keys = smooth_track([(i / 5, 960 + (5 if i % 2 else -5)) for i in range(50)], 1920, 608, [])
    assert len({k.x for k in keys}) == 1


def test_smooth_track_jumps_at_cut():
    s = [(t / 5, 400.0) for t in range(10)] + [(t / 5, 1500.0) for t in range(10, 20)]
    keys = smooth_track(s, 1920, 608, cuts=[2.0])
    assert keys[10].x == 1500 - 304


def test_smooth_track_clamps_to_frame():
    assert smooth_track([(0, 10.0)], 1920, 608, [])[0].x == 0


def test_smooth_track_holds_on_none():
    keys = smooth_track([(0, 1000.0), (0.2, None), (0.4, None)], 1920, 608, [])
    assert len({k.x for k in keys}) == 1


def test_plan_crop_gaming_and_vertical_use_fit(tiny_video, tiny_vertical_video):
    info = probe(tiny_video)
    p = plan_crop(tiny_video, 0, 4, "gaming", info)
    assert p == CropPlan("fit", 1280, 720, [])
    vinfo = probe(tiny_vertical_video)
    assert plan_crop(tiny_vertical_video, 0, 4, "talk", vinfo).mode == "fit"


def test_detect_faces_no_face_gives_none(tiny_video):
    out = detect_faces(tiny_video, 0, 2)
    assert out and all(x is None for _, x in out)


def test_plan_crop_talk_follows(tiny_video):
    p = plan_crop(tiny_video, 0, 2, "talk", probe(tiny_video))
    assert p.mode == "follow" and p.keys and all(0 <= k.x <= 1280 - 404 for k in p.keys)


@pytest.mark.slow
def test_detect_faces_finds_face(face_video):
    out = detect_faces(face_video, 0, 2)
    assert any(x is not None for _, x in out)


class _TC:
    def __init__(self, s):
        self.s = s

    def get_seconds(self):
        return self.s


def test_scene_cuts_uses_float_times_and_relative_cuts(monkeypatch):
    import scenedetect

    seen = {}

    def fake(path, det, start_time, end_time):
        seen.update(start=start_time, end=end_time)
        return [(_TC(10.0), _TC(12.5)), (_TC(12.5), _TC(15.0)), (_TC(15.0), _TC(20.0))]

    monkeypatch.setattr(scenedetect, "detect", fake)
    from clipper.reframe import scene_cuts

    assert scene_cuts("x.mp4", 10, 20) == [2.5, 5.0]
    assert isinstance(seen["start"], float) and isinstance(seen["end"], float)


def test_plan_crop_movie_passes_cuts_to_smoothing(monkeypatch, tiny_video):
    import clipper.reframe as r

    got = {}
    real = r.smooth_track

    def spy(s, w, cw, cuts, **k):
        got["cuts"] = cuts
        return real(s, w, cw, cuts, **k)

    monkeypatch.setattr(r, "scene_cuts", lambda *a: [1.0, 3.0])
    monkeypatch.setattr(r, "detect_faces", lambda *a: [(0.0, 500.0), (1.0, 900.0)])
    monkeypatch.setattr(r, "smooth_track", spy)
    p = r.plan_crop(tiny_video, 0, 4, "movie", probe(tiny_video))
    assert p.mode == "follow" and p.keys and got["cuts"] == [1.0, 3.0]


def test_plan_crop_zero_samples_falls_back_to_centre(monkeypatch, tiny_video):
    import clipper.reframe as r

    monkeypatch.setattr(r, "detect_faces", lambda *a: [])
    p = r.plan_crop(tiny_video, 0, 4, "talk", probe(tiny_video))
    assert [k.x for k in p.keys] == [(1280 - crop_width(720)) // 2]


def test_detect_faces_raises_on_unopenable_source(tmp_path):
    with pytest.raises(RuntimeError):
        detect_faces(tmp_path / "missing.mp4", 0, 1)
