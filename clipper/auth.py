from __future__ import annotations

import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

SESSION_MAX_AGE = 30 * 86400
_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _fingerprint(pw_hash: str) -> str:
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:16]


def make_session(secret: str, pw_hash: str) -> str:
    return TimestampSigner(secret).sign(_fingerprint(pw_hash)).decode()


def check_session(token: str | None, secret: str, pw_hash: str, max_age: int = SESSION_MAX_AGE) -> bool:
    if not token:
        return False
    try:
        fp = TimestampSigner(secret).unsign(token, max_age=max_age).decode()
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(fp, _fingerprint(pw_hash))


class Lockout:
    # shortcut: in memory, resets on restart; fine for one user behind Tailscale.
    def __init__(self, max_failures: int = 5, lock_seconds: int = 900):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._failures: list[float] = []

    def record_failure(self, now: float) -> None:
        self._failures = [t for t in self._failures if now - t < self.lock_seconds]
        self._failures.append(now)

    def remaining(self, now: float) -> int:
        recent = [t for t in self._failures if now - t < self.lock_seconds]
        if len(recent) < self.max_failures:
            return 0
        return max(0, int(self.lock_seconds - (now - recent[self.max_failures - 1]) + 0.999))

    def is_locked(self, now: float) -> bool:
        return self.remaining(now) > 0

    def reset(self) -> None:
        self._failures.clear()
