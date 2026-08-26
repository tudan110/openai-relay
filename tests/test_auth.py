import json
import os
import stat

import pytest

from openai_relay.auth import CodexAuthProvider, CredentialError


def write_auth(path, token="header.eyJleHAiOjQxMDAwMDAwMDB9.signature"):
    path.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": token,
            "refresh_token": "refresh-value",
            "account_id": "account-value",
            "id_token": "id-value",
        },
        "last_refresh": "2026-08-25T00:00:00Z",
    }))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_loads_codex_credentials(tmp_path):
    path = tmp_path / "auth.json"
    write_auth(path)
    credentials = CodexAuthProvider(path).get()
    assert credentials.access_token.startswith("header.")
    assert credentials.account_id == "account-value"


def test_rejects_wrong_mode(tmp_path):
    path = tmp_path / "auth.json"
    write_auth(path)
    data = json.loads(path.read_text())
    data["auth_mode"] = "apikey"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises(CredentialError):
        CodexAuthProvider(path).get()


def test_rejects_insecure_permissions(tmp_path):
    path = tmp_path / "auth.json"
    write_auth(path)
    path.chmod(0o644)
    with pytest.raises(CredentialError):
        CodexAuthProvider(path).get()


def test_reloads_when_codex_rotates_file(tmp_path):
    path = tmp_path / "auth.json"
    write_auth(path, "header.eyJleHAiOjQxMDAwMDAwMDB9.first")
    provider = CodexAuthProvider(path)
    assert provider.get().access_token.endswith("first")
    write_auth(path, "header.eyJleHAiOjQxMDAwMDAwMDB9.second")
    assert provider.get().access_token.endswith("second")
