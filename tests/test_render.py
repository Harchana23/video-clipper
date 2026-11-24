import pytest

from clipper.ingest import probe
from clipper.models import CropKey, CropPlan, Word
from clipper.render import RenderError, build_filter, pick_encoder, render_clip

AUDIO = ["-c:a", "aac", "-b:a", "160k"]


def test_pick_encoder_is_cached_and_valid():
    enc = pick_encoder()
    assert enc[:2] in (["-c:v", "h264_nvenc"], ["-c:v", "libx264"])
    assert pick_encoder() is enc


def test_build_filter_fit_has_blur_and_overlay():
    f, cmd = build_filter(CropPlan("fit", 1920, 1080, []), None)
    assert "boxblur" in f and "overlay" in f and "subtitles" not in f and cmd is None


def test_build_filter_adds_subtitles_only_when_given():
    f, _ = build_filter(CropPlan("fit", 1920, 1080, []), "c.ass")
    assert "subtitles=c.ass" in f


def test_build_filter_follow_moves_x_with_sendcmd():
    plan = CropPlan("follow", 1280, 720, [CropKey(0, 0), CropKey(2, 600), CropKey(2.5, 100)])
    f, cmd = build_filter(plan, None)
    assert "crop=406:720" in f and "scale=1080:1920" in f and "sendcmd" in f
    assert "crop x" in cmd and "600" in cmd


def test_render_clip_outputs_vertical_1080x1920(tiny_video, tmp_path):
    out = tmp_path / "c1.mp4"
    render_clip(tiny_video, out, 1, 4, CropPlan("follow", 1280, 720, [CropKey(0, 300)]), None, None, 0.1)
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920) and 2.5 < info.duration < 3.5 and info.has_audio
    assert (tmp_path / "c1.jpg").exists()
    assert not (tmp_path / "c1.ass").exists()
    assert not [p for p in tmp_path.iterdir() if ".tmp" in p.name or p.suffix == ".txt"]


def test_render_clip_fit_and_moving_follow(tiny_video, tmp_path):
    keys = [CropKey(0, 0), CropKey(2, 600)]
    render_clip(tiny_video, tmp_path / "a.mp4", 0, 3, CropPlan("follow", 1280, 720, keys), None, None, 0.1)
    render_clip(tiny_video, tmp_path / "b.mp4", 0, 3, CropPlan("fit", 1280, 720, []), None, None, 0.1)
    for n in ("a", "b"):
        info = probe(tmp_path / f"{n}.mp4")
        assert (info.width, info.height) == (1080, 1920)


def test_render_clip_with_captions(tiny_video, tmp_path):
    out = tmp_path / "c2.mp4"
    words = [Word(1.2, 1.5, "hello"), Word(1.6, 2.0, "there"), Word(3.0, 3.4, "friend")]
    render_clip(tiny_video, out, 1, 4, CropPlan("fit", 1280, 720, []), words, "bold_white", 0.1)
    assert probe(out).width == 1080
    assert (tmp_path / "c2.ass").exists()
    assert not [p for p in tmp_path.iterdir() if ".tmp" in p.name or p.suffix == ".txt"]


def test_render_clip_leaves_no_final_file_on_failure(tmp_path):
    with pytest.raises(RenderError):
        render_clip(tmp_path / "missing.mp4", tmp_path / "c.mp4", 0, 3, CropPlan("fit", 1920, 1080, []), None, None, 0.1)
    assert not (tmp_path / "c.mp4").exists() and not list(tmp_path.glob("*.tmp*"))
