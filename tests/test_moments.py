import numpy as np

from clipper.models import Candidate, Moment, Word
from clipper.moments import (
    audio_candidates,
    dedupe,
    final_score,
    hook_score,
    pace_score,
    select,
    sentence_ends,
    snap,
)


def sentence_words(n_words, per_sentence=5):
    """1 s per word, word i spans [i, i+1]; every `per_sentence`-th word ends with '.'."""
    return [
        Word(i, i + 1, f"w{i}." if (i + 1) % per_sentence == 0 else f"w{i}")
        for i in range(n_words)
    ]


def cand(s, e):
    return Candidate(s, e, "t", 80, "r")


def test_sentence_ends():
    ws = [Word(0, 1, "a"), Word(1, 2, "b."), Word(2, 3, "c?"), Word(3, 4, "d")]
    assert sentence_ends(ws) == [1, 2]


def test_snap_extends_to_sentence_boundaries():
    ws = sentence_words(60)  # sentences: [0-5) [5-10) [10-15) ...
    out = snap(cand(12.5, 17.2), ws, 5, 30)
    assert (out.start, out.end) == (10, 20)  # starts at first word of its sentence, ends after the "."
    assert (out.title, out.ai_score, out.reason) == ("t", 80, "r")


def test_snap_extends_short_by_whole_sentences():
    ws = sentence_words(60)
    out = snap(cand(10, 14), ws, 12, 30)  # 10-15 is 5 s < 12 -> two more sentences
    assert (out.start, out.end) == (10, 25)


def test_snap_cuts_long_at_sentence_end():
    ws = sentence_words(60)
    out = snap(cand(10, 50), ws, 5, 18)  # limit 28 -> last sentence end <= 28 is 25
    assert (out.start, out.end) == (10, 25)


def test_snap_cuts_long_at_word_end_without_sentence():
    ws = [Word(i, i + 1, f"w{i}") for i in range(40)]  # no punctuation at all
    out = snap(cand(0, 40), ws, 5, 18)
    assert (out.start, out.end) == (0, 18)


def test_snap_drops_when_source_runs_out():
    ws = sentence_words(100)  # source ends at 100 s
    assert snap(cand(91, 95), ws, 30, 90) is None  # only 10 s left from 90


def test_snap_no_words_clamps_length():
    assert snap(cand(10, 15), [], 30, 90) == cand(10, 40)
    assert snap(cand(10, 200), [], 30, 90) == cand(10, 100)
    assert snap(cand(10, 50), [], 30, 90) == cand(10, 50)


def test_hook_score():
    assert hook_score("Why does nobody talk about this?") == 1.0
    assert hook_score("The weather is fine. Why?") == 0.0  # only the first sentence counts
    assert hook_score("Is it?") == 0.4
    assert hook_score("There are 3 ways.") == 0.3
    assert hook_score("") == 0.0


def test_pace_score():
    def ws(n, dur):
        return [Word(i * dur / n, (i + 1) * dur / n, "x") for i in range(n)]

    assert pace_score([]) == 0.0
    assert pace_score(ws(25, 10)) == 1.0  # 2.5 wps
    assert pace_score(ws(5, 10)) == 0.0  # 0.5 wps
    assert pace_score(ws(55, 10)) == 0.0  # 5.5 wps
    assert abs(pace_score(ws(25, 20)) - 0.5) < 1e-9  # 1.25 wps -> halfway up the ramp
    assert abs(pace_score(ws(45, 10)) - 0.5) < 1e-9  # 4.5 wps -> halfway down


def test_final_score_weights_energy_more_for_gaming():
    assert final_score(50, 1.0, 0, 0, "gaming") > final_score(50, 1.0, 0, 0, "talk")


def test_final_score_values():
    assert final_score(100, 1, 1, 1, "talk") == 100
    assert final_score(80, 0.5, 1, 0, "talk") == 68  # 48+10+10
    assert final_score(80, 0.6, 1, 0, "movie") == 64  # 32+27+5
    assert final_score(0, 0, 0, 0, "gaming") == 0


def test_dedupe_keeps_higher_score():
    a, b = Moment(0, 40, "a", 70), Moment(10, 50, "b", 80)
    assert dedupe([a, b]) == [b]


def test_dedupe_keeps_non_overlapping_and_light_overlap():
    a, b, c = Moment(0, 40, "a", 70), Moment(30, 70, "b", 80), Moment(100, 140, "c", 60)
    assert dedupe([c, b, a]) == [a, b, c]  # 10/40 = 25% overlap -> both kept; sorted by start


def test_dedupe_tie_keeps_earlier():
    a, b = Moment(0, 40, "a", 70), Moment(5, 45, "b", 70)
    assert dedupe([b, a]) == [a]


def test_select_caps_per_hour():
    ms = [Moment(i * 100, i * 100 + 40, str(i), 90) for i in range(30)]
    assert len(select(ms, 3600, 55, 15)) == 15


def test_select_sorted_threshold_and_tie_order():
    ms = [Moment(50, 90, "late", 80), Moment(0, 40, "early", 80), Moment(100, 140, "top", 95), Moment(200, 240, "low", 10)]
    assert [m.title for m in select(ms, 3600, 55, 15)] == ["top", "early", "late"]


def test_select_minimum_one_for_tiny_per_hour():
    ms = [Moment(0, 40, "a", 90), Moment(100, 140, "b", 80)]
    assert [m.title for m in select(ms, 60, 55, 15)] == ["a"]  # ceil(60/3600*15) = 1


def test_select_short_source():
    assert select([Moment(0, 30, "a", 20)], 150, 55, 15) == [Moment(0, 30, "a", 20)]
    assert select([Moment(0, 30, "a", 20)], 100, 55, 15) == []
    assert select([], 150, 55, 15) == []


def test_select_fallback_picks_best():
    ms = [Moment(0, 30, "a", 20), Moment(50, 80, "b", 40)]
    assert select(ms, 500, 55, 15) == [Moment(50, 80, "b", 40)]


def test_find_moments_falls_back_to_audio_when_no_words():
    energy = np.zeros(1200)
    energy[400] = 1.0  # hop 0.5 s -> peak at 200 s
    c = audio_candidates(energy, 0.5, 30, 90, 1)[0]
    assert c.start < 200 < c.end and 30 <= c.end - c.start <= 90
    assert c.title == "Moment at 03:20"
    assert c.ai_score == 50


def test_audio_candidates_non_overlapping_and_clamped():
    energy = np.zeros(1200)  # 600 s total
    energy[2], energy[10], energy[800] = 1.0, 0.9, 0.5  # 1st and 2nd peaks collide
    cs = audio_candidates(energy, 0.5, 30, 90, 2)
    assert len(cs) == 2  # the 0.9 peak collides with the 1.0 window, so the 0.5 peak is taken
    for c in cs:
        assert 0 <= c.start and c.end <= 600 and c.end - c.start == 60
    assert cs[0].start == 0  # clamped at the start, length preserved
    assert cs[1].start < 400 < cs[1].end
