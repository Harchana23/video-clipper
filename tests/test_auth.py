import itsdangerous.timed

from clipper.auth import Lockout, check_session, hash_password, make_session, verify_password


def test_hash_and_verify():
    h = hash_password("pw")
    assert verify_password(h, "pw")
    assert not verify_password(h, "nope")


def test_verify_malformed_hash_is_false():
    assert verify_password("not-a-hash", "pw") is False
    assert verify_password("", "pw") is False


def test_session_roundtrip_and_password_change():
    h1, h2 = hash_password("a"), hash_password("b")
    tok = make_session("s", h1)
    assert check_session(tok, "s", h1) and not check_session(tok, "s", h2)


def test_session_rejects_missing_tampered_and_wrong_secret():
    h = hash_password("a")
    tok = make_session("s", h)
    assert not check_session(None, "s", h)
    assert not check_session("", "s", h)
    assert not check_session(tok + "x", "s", h)
    assert not check_session(tok, "other", h)


def test_session_expires(monkeypatch):
    h = hash_password("a")
    tok = make_session("s", h)
    assert check_session(tok, "s", h, max_age=100)
    real = itsdangerous.timed.time.time()
    monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: real + 101)
    assert not check_session(tok, "s", h, max_age=100)


def test_lockout_after_five_failures():
    lk = Lockout()
    for _ in range(5):
        lk.record_failure(100)
    assert lk.is_locked(100 + 899) and not lk.is_locked(100 + 901)
    assert lk.remaining(100 + 899) == 1 and lk.remaining(100 + 901) == 0


def test_four_failures_do_not_lock():
    lk = Lockout()
    for _ in range(4):
        lk.record_failure(100)
    assert not lk.is_locked(101) and lk.remaining(101) == 0


def test_old_failures_age_out_of_window():
    lk = Lockout()
    for t in (0, 1, 2, 3):
        lk.record_failure(t)
    lk.record_failure(1000)  # first four are older than 900s
    assert not lk.is_locked(1001)


def test_reset_clears_lock():
    lk = Lockout()
    for _ in range(5):
        lk.record_failure(100)
    lk.reset()
    assert not lk.is_locked(101)
