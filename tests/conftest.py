import os
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


_SPEECH = """\
Nobody tells you this when you start a business: the first year is mostly about learning what not to do.
I made every mistake you can imagine. I hired too fast, I priced too low, and I ignored my first customers.
Here is the strange part. The mistake that hurt the most was the one I thought was smart.
I spent six months building a feature that nobody asked for. Six months! I was so proud of it.
Then we launched, and exactly three people used it. One of them was my mother.
So what did I change? I started calling customers every single week, and I never stopped.
Why does this work so well? Because people will tell you the truth if you simply ask them a real question.
They will tell you what annoys them, what they would pay for, and what they would never touch.
The second lesson is about money. Cash is oxygen. You can be profitable on paper and still run out of air.
I learned that when a client paid us ninety days late, and we nearly missed payroll.
That week I did something I am not proud of. I stopped sleeping, and I stopped listening to my team.
It was the worst decision of the year, and it taught me the most important thing of all.
The best founders are not the smartest people in the room. They are the people who keep asking questions.
So here is my challenge to you. Pick one customer today and call them. Do not sell anything.
Just ask one question: what is the hardest part of your day? Then be quiet and listen.
I promise you will learn more in ten minutes than in ten weeks of guessing.
The third lesson sounds simple, but it changed everything for me. Say no to almost everything.
Every yes costs you time, and time is the only thing you cannot buy back.
When we finally focused on one product for one type of customer, our revenue tripled in four months.
Can you believe that? We did less, and we earned more. It still surprises me today.
Looking back, the failures were not the opposite of success. They were the road that led to it.
If you are in the middle of a hard year right now, stay with it. It really does get better.
Thank you for listening, and I hope at least one of these lessons saves you six months.
"""

_PS1 = """\
param($TextFile, $OutFile)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile($OutFile)
$s.Speak([System.IO.File]::ReadAllText($TextFile))
$s.Dispose()
"""


@pytest.fixture(scope="session")
def speech_video(tmp_path_factory) -> Path:
    """~2.5 min of English speech (Windows SAPI) over a 1280x720 test pattern."""
    d = tmp_path_factory.mktemp("speech")
    (d / "say.txt").write_text(_SPEECH * 2, encoding="utf-8")
    (d / "say.ps1").write_text(_PS1, encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(d / "say.ps1"),
                    str(d / "say.txt"), str(d / "speech.wav")], check=True)
    out = d / "speech.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25",
         "-i", str(d / "speech.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(out)],
        check=True,
    )
    return out


@pytest.fixture(scope="session")
def face_video() -> Path:
    """No offline face asset exists: point CLIPPER_FACE_VIDEO at a short clip with a visible face."""
    p = os.environ.get("CLIPPER_FACE_VIDEO")
    if not p or not Path(p).is_file():
        pytest.skip("set CLIPPER_FACE_VIDEO to a short clip with a visible face")
    return Path(p)
