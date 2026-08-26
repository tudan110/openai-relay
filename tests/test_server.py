from openai_relay import server


def test_account_panel_shows_explicit_unavailable_state(monkeypatch):
    monkeypatch.setattr(server, "get_account_quota", lambda: None)
    html = server.render_account_panel()
    assert "上游 Codex 账号额度" in html
    assert "周期额度暂不可用" in html
    assert "伪造账号额度" in html


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
