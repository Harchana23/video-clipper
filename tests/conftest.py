import subprocess
from pathlib import Path

import pytest


def _make(path: Path, size: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration=5:size={size}:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def tiny_video(tmp_path) -> Path:
    return _make(tmp_path / "tiny.mp4", "1280x720")


@pytest.fixture
def tiny_vertical_video(tmp_path) -> Path:
    return _make(tmp_path / "tiny_vertical.mp4", "720x1280")
