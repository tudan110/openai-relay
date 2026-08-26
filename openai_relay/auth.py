#!/usr/bin/env python3
"""Server-side Codex login credential loading."""

import base64
import binascii
import json
import os
import stat
import threading
import time
from pathlib import Path


class CredentialError(RuntimeError):
    pass


class CodexCredentials:
    def __init__(self, access_token, account_id, expires_at, mtime_ns):
        self.access_token = access_token
        self.account_id = account_id
        self.expires_at = expires_at
        self.mtime_ns = mtime_ns


class CodexAuthProvider:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._cached = None

    @staticmethod
    def _expiry(token):
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
            value = payload.get("exp")
            return float(value) if isinstance(value, (int, float)) else None
        except (IndexError, TypeError, ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
            return None

    def _read(self):
        try:
            st = self.path.lstat()
        except OSError as exc:
            raise CredentialError("Codex credentials unavailable") from exc
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise CredentialError("Codex credentials unavailable")
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise CredentialError("Codex credentials permissions are unsafe")
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError, TypeError) as exc:
            raise CredentialError("Codex credentials unavailable") from exc
        if not isinstance(data, dict) or data.get("auth_mode") != "chatgpt":
            raise CredentialError("Codex credentials unavailable")
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            raise CredentialError("Codex credentials unavailable")
        access = tokens.get("access_token")
        account = tokens.get("account_id")
        if not isinstance(access, str) or not access or not isinstance(account, str) or not account:
            raise CredentialError("Codex credentials unavailable")
        return CodexCredentials(access, account, self._expiry(access), st.st_mtime_ns)

    def get(self):
        with self._lock:
            current = self._read()
            if self._cached is None or current.mtime_ns != self._cached.mtime_ns:
                self._cached = current
            if self._cached.expires_at is not None and time.time() >= self._cached.expires_at - 60:
                raise CredentialError("Codex access token expired")
            return self._cached


_DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
_provider = CodexAuthProvider(os.environ.get("CODEX_AUTH_FILE", _DEFAULT_PATH))


def get_credentials():
    return _provider.get()
