import random

import pytest

from clipper.captions import apply_corrections, build_ass, chunk_words, words_in_range
from clipper.models import CaptionEvent, Word


def W(s, e, t):
    return Word(s, e, t)


@pytest.fixture
def words_fixture_random():
    rng = random.Random(1234)
    words, t = [], 0.0
    for i in range(200):
        t += rng.uniform(0.05, 1.2)
        d = rng.uniform(0.1, 0.5)
        words.append(Word(t, t + d, f"w{i:03d}"))
        t += d
    return words


def test_tight_pair_shows_together_at_first_onset():
    ev = chunk_words([W(1.0, 1.2, "nobody"), W(1.3, 1.6, "tells")])
    assert ev == [CaptionEvent(1.1, 2.0, "NOBODY TELLS")]  # ends at 1.6 + 0.1 + 0.3


def test_gap_pair_reveals_progressively():
    ev = chunk_words([W(1.0, 1.2, "wait"), W(2.0, 2.3, "what")])
    assert ev[0] == CaptionEvent(1.1, 2.1, "WAIT")
    assert ev[1] == CaptionEvent(2.1, 2.7, "WAIT WHAT")  # ends at 2.3 + 0.1 + 0.3


def test_trailing_odd_word_is_its_own_chunk():
    ev = chunk_words([W(0, 0.2, "a"), W(0.3, 0.5, "b"), W(0.6, 0.9, "c")])
    assert [e.text for e in ev] == ["A B", "C"]
    assert ev[0].end == 0.7  # cut off by the next chunk's first start, not hold


def test_no_word_ever_appears_before_spoken(words_fixture_random):
    ev = chunk_words(words_fixture_random, delay=0.1)
    starts = {w.text.upper(): w.start for w in words_fixture_random}
    assert {t for e in ev for t in e.text.split()} == set(starts)  # every word shown
    for e in ev:
        first, *rest = e.text.split()
        assert e.start >= starts[first] + 0.1 - 1e-9  # lead word: never early
        for token in rest:
            # a tight pair (<= 0.45 s apart) may share the first word's onset; otherwise strictly not early
            early = starts[token] + 0.1 - e.start
            assert early <= 1e-9 or starts[token] - starts[first] <= 0.45 + 1e-9


def test_events_never_overlap(words_fixture_random):
    ev = chunk_words(words_fixture_random)
    assert all(a.end <= b.start + 1e-9 for a, b in zip(ev, ev[1:]))
    assert all(e.end > e.start for e in ev)


def test_words_in_range_shifts_to_clip_time():
    assert words_in_range([W(10, 10.5, "a"), W(20, 20.5, "b")], 9.0, 15.0) == [W(1.0, 1.5, "a")]


def test_apply_corrections_keeps_timing_and_removes_empty():
    out = apply_corrections([W(0, 1, "price"), W(1, 2, "in")], {0: "pricing", 1: ""})
    assert out == [W(0, 1, "pricing")]


def test_build_ass_escapes_braces_and_backslashes():
    ass = build_ass([CaptionEvent(0, 1, "A{B}\\C")], "bold_white")
    assert "A(B)/C" in ass and "{B}" not in ass  # ASS has no brace escape


def test_build_ass_styles_and_resolution():
    ass = build_ass([CaptionEvent(0, 1, "HI")], "yellow_pop")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:01.00" in ass
    assert "&H0042C5F5" in ass


def test_build_ass_start_rounds_up_to_centisecond():
    ass = build_ass([CaptionEvent(1.234, 2.234, "HI"), CaptionEvent(1.1, 1.5, "X")], "bold_white")
    assert "Dialogue: 0,0:00:01.24,0:00:02.23," in ass  # start never earlier than computed; end nearest
    assert "Dialogue: 0,0:00:01.10,0:00:01.50," in ass  # exact centiseconds stay put
