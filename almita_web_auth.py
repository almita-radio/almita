#!/usr/bin/env python3
"""Single-user HTTP Basic authentication for the ALMITA web server (one port: console, API, streams).

Credentials live OUTSIDE the repository - default ~/.config/almita/web_auth.json, override with the
ALMITA_WEB_AUTH_FILE environment variable - and hold only a PBKDF2-SHA256 hash + salt, never the password.
The file must be private to its owner (no group/other permission bits). A missing, unreadable, malformed
or world-readable file means NOT CONFIGURED, and the server then refuses every request (fail closed).

Set or change the user (prompts for the password, never echoes or logs it):
    .venv/bin/python almita_web_auth.py set-password --user <name>
Check the file without revealing anything:
    .venv/bin/python almita_web_auth.py check

Basic auth sends the password with every request: over plain HTTP it is readable by anyone on the
network path. Fine on the field LAN; put TLS in front of it before exposing it to the Internet.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import threading
from pathlib import Path
from typing import Optional

AUTH_FILE_ENV = "ALMITA_WEB_AUTH_FILE"
DEFAULT_AUTH_FILE = Path.home() / ".config" / "almita" / "web_auth.json"
ITERATIONS = 200_000
MIN_ITERATIONS = 100_000
REALM = "ALMITA"
_MAX_CACHED = 32


def auth_file_path() -> Path:
    return Path(os.environ.get(AUTH_FILE_ENV) or DEFAULT_AUTH_FILE)


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def write_credentials(path: Path, user: str, password: str) -> None:
    """Atomically write user + PBKDF2 hash with mode 0600 (directory 0700)."""
    if not user or ":" in user:
        raise ValueError("user must be non-empty and contain no ':'")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    record = {"user": user, "algorithm": "pbkdf2_sha256", "iterations": ITERATIONS,
              "salt": salt.hex(), "hash": _derive(password, salt, ITERATIONS).hex()}
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Authenticator:
    """Verifies `Authorization: Basic ...` headers against the credentials file.

    The file is re-read when its mtime changes (a password change applies without a restart). A header that
    already verified is remembered by its SHA-256 only, so the deliberately slow PBKDF2 runs once per
    credential, not on every 2-second poll.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else auth_file_path()
        self._lock = threading.Lock()
        self._stamp = None
        self._record = None
        self._verified: set = set()

    def _load(self):
        try:
            st = self.path.stat()
        except OSError:
            self._stamp, self._record = None, None
            return None
        stamp = (st.st_mtime_ns, st.st_size, st.st_mode)
        if stamp == self._stamp:
            return self._record
        self._stamp, self._record = stamp, None
        self._verified.clear()
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None  # readable by others: refuse rather than trust a leaked hash file
        try:
            rec = json.loads(self.path.read_text())
            record = {"user": str(rec["user"]), "salt": bytes.fromhex(rec["salt"]), "hash": bytes.fromhex(rec["hash"]),
                      "iterations": int(rec["iterations"])}
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if rec.get("algorithm") != "pbkdf2_sha256" or not record["user"] or record["iterations"] < MIN_ITERATIONS or len(record["hash"]) != 32:
            return None
        self._record = record
        return record

    def configured(self) -> bool:
        with self._lock:
            return self._load() is not None

    def check(self, header: Optional[str]) -> bool:
        with self._lock:
            record = self._load()
            if record is None or not header or not header.startswith("Basic "):
                return False
            key = hashlib.sha256(header.encode("utf-8", "replace")).digest()
            if key in self._verified:
                return True
        try:
            user, _, password = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8").partition(":")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return False
        ok_user = hmac.compare_digest(user.encode("utf-8"), record["user"].encode("utf-8"))
        ok_pass = hmac.compare_digest(_derive(password, record["salt"], record["iterations"]), record["hash"])
        if not (ok_user and ok_pass):
            return False
        with self._lock:
            if len(self._verified) >= _MAX_CACHED:
                self._verified.clear()
            self._verified.add(key)
        return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ALMITA web single-user credentials (stored as a hash outside the repo)")
    parser.add_argument("--file", default=None, help=f"credentials file (default: ${AUTH_FILE_ENV} or {DEFAULT_AUTH_FILE})")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-password", help="create/replace the single web user")
    sp.add_argument("--user", required=True)
    sub.add_parser("check", help="report whether the credentials file is valid (reveals nothing else)")
    args = parser.parse_args(argv)
    path = Path(args.file) if args.file else auth_file_path()
    if args.cmd == "set-password":
        password = getpass.getpass("New ALMITA web password: ")
        if password != getpass.getpass("Repeat password: "):
            print("passwords do not match; nothing written", file=sys.stderr)
            return 1
        try:
            write_credentials(path, args.user, password)
        except ValueError as exc:
            print(f"refused: {exc}; nothing written", file=sys.stderr)
            return 1
        print(f"written {path} (mode 0600, PBKDF2-SHA256 hash only)")
        return 0
    ok = Authenticator(path).configured()
    print(f"{path}: {'VALID' if ok else 'NOT CONFIGURED (missing, malformed or readable by group/other) - web access stays closed'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
