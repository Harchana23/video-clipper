from clipper.models import Segment, Transcript, Word


def test_transcript_roundtrip(tmp_path):
    t = Transcript("en", [Word(0.0, 0.4, "hi")], [Segment(0.0, 0.4, "hi")])
    t.save(tmp_path / "t.json")
    assert Transcript.load(tmp_path / "t.json") == t
