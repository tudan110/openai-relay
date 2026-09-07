"""Tests for OpenAI relay accounting and Responses usage parsing."""

import importlib

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_DB", str(tmp_path / "relay.db"))
    from openai_relay import db as relay_db
    relay_db = importlib.reload(relay_db)
    relay_db.init_db()
    return relay_db


def test_responses_json_usage(db):
    body = b'{"usage":{"input_tokens":10,"output_tokens":20,"input_tokens_details":{"cached_tokens":5}}}'
    assert db.parse_nonstream_usage(body) == (10, 20, 0, 5)


def test_responses_sse_usage_across_events(db):
    text = (
        'event: response.created\n\n'
        'data: {"type":"response.in_progress"}\n\n'
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":1200,"output_tokens":456,"input_tokens_details":'
        '{"cached_tokens":300}}}}\n\n'
        'data: [DONE]\n\n'
    )
    assert db.parse_stream_usage(text[:80], text[80:]) == (1200, 456, 0, 300)


def test_chat_completions_usage_compatibility(db):
    body = b'{"usage":{"prompt_tokens":7,"completion_tokens":8,"prompt_tokens_details":{"cached_tokens":2}}}'
    assert db.parse_nonstream_usage(body) == (7, 8, 0, 2)


def test_standard_api_equivalent_pricing(db):
    million = 1_000_000
    expected = {
        "gpt-6-astra": 51.0,
        "gpt-5.6-sol": 20.4,
        "gpt-5.6-terra": 12.2,
        "gpt-5.6-luna": 1.22,
        "gpt-5.3-codex": 14.175,
    }
    for model, cost in expected.items():
        assert db.compute_cost(model, million, million, 0, million) == cost


def test_pricing_prefix_and_unknown_model(db):
    assert db.compute_cost("gpt-5.6-sol-2026-08-01", 1, 0, 0, 0) == 0.000004
    assert db.compute_cost("not-a-real-model", 1_000_000, 1_000_000, 0, 1_000_000) == 0


def test_historical_reprice_updates_known_rows_only(db):
    conn = db.connect()
    conn.executemany(
        "INSERT INTO usage(ts, day, username, api_key, model, input_tokens, output_tokens, "
        "cache_creation_tokens, cache_read_tokens, cost_usd, status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "2026-08-25", "alice", "key", "gpt-5.6-sol", 1_000_000, 1_000_000, 0, 0, 0, 200),
            (2, "2026-08-25", "alice", "key", "unknown-model", 1_000_000, 1_000_000, 0, 0, 0, 200),
            (3, "2026-08-25", "alice", "key", "gpt-5.6-sol", 0, 0, 0, 0, 0, 500),
        ],
    )
    conn.commit()
    conn.close()

    preview = db.reprice_usage()
    assert preview == {"eligible": 1, "changed": 1, "before": 0, "after": 24.0, "applied": False}
    assert db.reprice_usage(apply=True)["changed"] == 1
    assert db.reprice_usage()["changed"] == 0
    conn = db.connect(readonly=True)
    costs = [r["cost_usd"] for r in conn.execute("SELECT cost_usd FROM usage ORDER BY id")]
    conn.close()
    assert costs == [24.0, 0.0, 0.0]


    key = db.add_user("alice", daily_request_limit=2, rpm_limit=2)
    db.reload_users()
    user, reason = db.authenticate(key)
    assert user["username"] == "alice" and reason is None
    assert db.admit_request(user) is None
    assert db.admit_request(user) is None
    assert db.admit_request(user) is not None


def test_disabled_and_strict_auth(db):
    key = db.add_user("alice")
    db.reload_users()
    db.set_enabled(username="alice", enabled=0)
    db.reload_users()
    assert db.authenticate(key)[1] == "disabled"
    assert db.authenticate("unknown")[1] == "unknown_key"


def test_model_family_and_client(db):
    assert db.model_family("gpt-5-codex") == "Codex"
    assert db.model_family("gpt-4.1") == "GPT"
    assert db.model_family("other") == "其他"
    assert db.classify_client("codex-cli/0.131") == "cli"
    assert db.classify_client("Codex Desktop") == "desktop"
