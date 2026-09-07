import pytest

from openai_relay import server


def test_prepare_codex_request_removes_claude_desktop_output_cap():
    request = {
        "model": "gpt-5.6-sol",
        "input": [{"role": "user", "content": "hello"}],
        "stream": True,
        "store": True,
        "max_output_tokens": 4096,
        "tools": [{"type": "web_search"}],
    }
    prepared = server.prepare_codex_request(request)
    assert "max_output_tokens" not in prepared
    assert prepared["store"] is False
    assert prepared["model"] == request["model"]
    assert prepared["input"] == request["input"]
    assert prepared["stream"] is True
    assert prepared["tools"] == request["tools"]
    assert request["max_output_tokens"] == 4096
    assert request["store"] is True


def test_prepare_codex_request_preserves_requests_without_output_cap():
    request = {"model": "gpt-5.6-sol", "input": [], "stream": True}
    assert server.prepare_codex_request(request) == {
        "model": "gpt-5.6-sol", "input": [], "stream": True, "store": False,
    }


def test_prepare_codex_request_shortens_long_input_ids_and_preserves_references():
    long_id = "item_" + "a" * 80
    request = {
        "input": [
            {"type": "message", "id": long_id, "content": "hello"},
            {"type": "item_reference", "id": long_id},
        ],
        "stream": True,
    }

    prepared = server.prepare_codex_request(request)

    shortened = prepared["input"][0]["id"]
    assert len(shortened) == 64
    assert shortened.startswith("rs")
    assert shortened == prepared["input"][1]["id"]
    assert shortened == server.prepare_codex_request(request)["input"][0]["id"]
    assert request["input"][0]["id"] == long_id
    assert request["input"][1]["id"] == long_id
    assert prepared["input"][0] is not request["input"][0]


def test_prepare_codex_request_preserves_encrypted_item_ids():
    long_id = "rs_" + "a" * 80
    encrypted = "opaque-encrypted-content"
    request = {
        "input": [{"type": "reasoning", "id": long_id, "encrypted_content": encrypted}],
        "stream": True,
    }

    prepared = server.prepare_codex_request(request)

    assert prepared["input"][0]["id"] == long_id
    assert prepared["input"][0]["encrypted_content"] == encrypted
    assert request["input"][0]["id"] == long_id


    exact_id = "e" * 64
    request = {
        "input": [
            {"id": exact_id, "metadata": {"id": "nested-" + "x" * 80}},
            {"id": 123},
            "not-an-item",
        ],
        "previous_response_id": "response-" + "y" * 80,
    }

    prepared = server.prepare_codex_request(request)

    assert prepared["input"][0]["id"] == exact_id
    assert prepared["input"][0]["metadata"]["id"] == request["input"][0]["metadata"]["id"]
    assert prepared["input"][1]["id"] == 123
    assert prepared["input"][2] == "not-an-item"
    assert prepared["previous_response_id"] == request["previous_response_id"]


def test_account_panel_shows_explicit_unavailable_state(monkeypatch):
    monkeypatch.setattr(server, "get_account_quota", lambda: None)
    html = server.render_account_panel()
    assert "上游 Codex 账号额度" in html
    assert "周期额度暂不可用" in html
    assert "伪造账号额度" in html


@pytest.mark.parametrize("input_value", ["plain text", {"id": "x"}, None])
def test_prepare_codex_request_leaves_non_list_input_unchanged(input_value):
    request = {"input": input_value, "stream": True}

    prepared = server.prepare_codex_request(request)

    assert prepared["input"] == input_value
    assert request["input"] == input_value


def test_account_panel_renders_normalized_quota(monkeypatch):
    monkeypatch.setattr(server, "get_account_quota", lambda: [{
        "label": "5 小时窗口",
        "used_percent": 25,
        "reset": "重置于 09-01 18:39",
    }])
    html = server.render_account_panel()
    assert "5 小时窗口" in html
    assert "75% 剩余" in html
    assert "width:75%" in html
    assert "重置于 09-01 18:39" in html


def test_normalize_account_quota_extracts_safe_windows():
    rows = server.normalize_account_quota({
        "account_id": "must-not-appear",
        "rate_limit": {
            "primary_window": {
                "used_percent": 20,
                "limit_window_seconds": 604800,
                "reset_at": 1788313142,
            },
        },
        "additional_rate_limits": [{
            "limit_name": "GPT-5.3-Codex-Spark",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 25,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 60,
                },
            },
        }],
    })
    assert [row["label"] for row in rows] == ["本周", "GPT-5.3-Codex-Spark · 5小时"]
    assert [row["used_percent"] for row in rows] == [20, 25]
    assert all("account" not in str(row) for row in rows)


def test_account_quota_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "fetch_account_quota", lambda: calls.append(1) or [])
    monkeypatch.setattr(server, "_acct_quota", {"data": None, "ts": 0.0})
    assert server.get_account_quota() == []
    assert server.get_account_quota() == []
    assert len(calls) == 1
