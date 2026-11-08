"""Panel login: the configured password, until the admin changes it from the panel."""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading

log = logging.getLogger(__name__)

ITERATIONS = 600_000
MIN_LENGTH = 8


class LoginError(Exception):
    """A refused password change; the message is shown in the panel."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _derive(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


class Login:
    """HTTP Basic login for the panel.

    A password changed from the panel is kept salted and hashed in `path` and wins over
    the one from config/env, so restarts and redeploys don't bring the old one back.
    Without `path` a change lasts until the process exits.
    """

    def __init__(self, user, password, path=None):
        self.user = user or "admin"
        self.password = password or None
        self.path = path
        self.stored = self._load()
        self._accepted = None  # fast check of the last good password: Basic auth runs on every request
        self._lock = threading.Lock()

    @property
    def required(self):
        return bool(self.password or self.stored)

    def _load(self):
        if not self.path:
            return None
        try:
            with open(self.path) as fh:
                scheme, iterations, salt, digest = fh.read().strip().split("$")
            if scheme != "pbkdf2_sha256":
                raise ValueError("unknown scheme %r" % scheme)
            return int(iterations), bytes.fromhex(salt), bytes.fromhex(digest)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.error("ignoring unreadable %s, using the configured password: %s", self.path, exc)
            return None

    def _save(self, stored):
        iterations, salt, digest = stored
        tmp = "%s.%d.tmp" % (self.path, threading.get_ident())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("pbkdf2_sha256$%d$%s$%s\n" % (iterations, salt.hex(), digest.hex()))
        os.replace(tmp, self.path)

    def check(self, password):
        stored = self.stored
        if not stored:
            return bool(self.password) and hmac.compare_digest(password.encode(), self.password.encode())
        quick = hashlib.sha256(stored[1] + password.encode()).digest()
        accepted = self._accepted
        if accepted is not None and hmac.compare_digest(quick, accepted):
            return True
        iterations, salt, digest = stored
        if not hmac.compare_digest(_derive(password, salt, iterations), digest):
            return False
        self._accepted = quick
        return True

    def authorized(self, header):
        if not self.required:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user.encode(), self.user.encode()) & self.check(password)

    def change(self, current, new):
        if not isinstance(current, str) or not isinstance(new, str):
            raise ValueError("passwords must be strings")
        with self._lock:
            if not self.required:
                raise LoginError(409, "Questo nodo non ha un login da modificare")
            if not self.check(current):
                raise LoginError(403, "La password attuale non è corretta")
            if len(new) < MIN_LENGTH:
                raise LoginError(400, "La nuova password deve avere almeno %d caratteri" % MIN_LENGTH)
            salt = secrets.token_bytes(16)
            stored = (ITERATIONS, salt, _derive(new, salt, ITERATIONS))
            if self.path:
                self._save(stored)
            self.stored, self._accepted = stored, None
        log.info("panel password changed for user %r", self.user)
