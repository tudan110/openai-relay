#!/usr/bin/env python3
"""SQLite-backed users, quotas, and OpenAI Responses usage accounting."""

import os
import json
import time
import queue
import sqlite3
import secrets
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
DB_PATH = os.environ.get("RELAY_DB", os.path.join(_ROOT, "relay.db"))

# USD per 1,000,000 tokens. Keep this table explicit because provider pricing
# changes; unknown models use the configured fallback and remain estimates.
PRICING = {
    "gpt-5.6-sol": {"in": 4.0, "out": 20.0, "cw": 0.0, "cr": 0.40},
    "gpt-5.6-terra": {"in": 2.0, "out": 12.0, "cw": 0.0, "cr": 0.20},
    "gpt-5.6-luna": {"in": 0.20, "out": 1.20, "cw": 0.0, "cr": 0.02},
    "gpt-5.3-codex": {"in": 1.75, "out": 14.0, "cw": 0.0, "cr": 0.175},
    "gpt-4o": {"in": 2.50, "out": 10.0, "cw": 0.0, "cr": 1.25},
    "gpt-4.1": {"in": 2.0, "out": 8.0, "cw": 0.0, "cr": 0.50},
}
_DEFAULT_PRICE = {"in": 0.0, "out": 0.0, "cw": 0.0, "cr": 0.0}


def price_for(model):
    if not model:
        return _DEFAULT_PRICE
    best = None
    for prefix, p in PRICING.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, p)
    return best[1] if best else _DEFAULT_PRICE


def compute_cost(model, input_tokens, output_tokens, cache_write, cache_read):
    p = price_for(model)
    cached_input = min(max(int(cache_read or 0), 0), max(int(input_tokens or 0), 0))
    regular_input = max(int(input_tokens or 0) - cached_input, 0)
    return round(
        (regular_input * p["in"]
         + int(output_tokens or 0) * p["out"]
         + int(cache_write or 0) * p["cw"]
         + cached_input * p["cr"]) / 1_000_000.0,
        6,
    )


def ts():
    return time.strftime("%H:%M:%S")


def _today():
    return time.strftime("%Y-%m-%d")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    api_key             TEXT PRIMARY KEY,
    username            TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          INTEGER NOT NULL,
    expires_at          INTEGER,
    daily_token_limit   INTEGER,
    daily_request_limit INTEGER,
    rpm_limit           INTEGER,
    note                TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    INTEGER NOT NULL,
    day                   TEXT NOT NULL,
    username              TEXT NOT NULL,
    api_key               TEXT NOT NULL,
    model                 TEXT,
    stream                INTEGER,
    status                INTEGER,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cost_usd              REAL DEFAULT 0,
    latency_ms            INTEGER,
    path                  TEXT,
    error                 TEXT,
    client                TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage(username, ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day);
"""


def connect(readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # migration: add the client column to pre-existing usage tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(usage)")}
        if "client" not in cols:
            conn.execute("ALTER TABLE usage ADD COLUMN client TEXT")
        conn.commit()
    finally:
        conn.close()


def classify_client(ua):
    """Map a User-Agent to a coarse Codex/OpenAI client type."""
    value = (ua or "").lower()
    if "desktop" in value or "codex.app" in value:
        return "desktop"
    if "codex" in value or "openai-python" in value or "openai-node" in value:
        return "cli"
    return "other"


MODEL_FAMILIES = ["GPT", "Codex", "其他"]


def model_family(model):
    m = (model or "").lower()
    if "codex" in m:
        return "Codex"
    if "gpt" in m:
        return "GPT"
    return "其他"


def gen_key(username):
    return f"sk-relay-{username}-{secrets.token_hex(12)}"


# ---------------------------------------------------------------------------
# User management (used by manage.py and the /admin endpoints)
# ---------------------------------------------------------------------------

def add_user(username, daily_token_limit=None, daily_request_limit=None,
             rpm_limit=None, expires_at=None, note=None, api_key=None):
    key = api_key or gen_key(username)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO users(api_key, username, enabled, created_at, expires_at,"
            " daily_token_limit, daily_request_limit, rpm_limit, note)"
            " VALUES(?,?,1,?,?,?,?,?,?)",
            (key, username, int(time.time() * 1000), expires_at,
             daily_token_limit, daily_request_limit, rpm_limit, note),
        )
        conn.commit()
    finally:
        conn.close()
    return key


def set_enabled(api_key=None, username=None, enabled=1):
    conn = connect()
    try:
        if api_key:
            cur = conn.execute("UPDATE users SET enabled=? WHERE api_key=?", (enabled, api_key))
        else:
            cur = conn.execute("UPDATE users SET enabled=? WHERE username=?", (enabled, username))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_limits(api_key=None, username=None, **fields):
    # A field PRESENT in `fields` is written as-is — including None, which sets
    # the column to NULL (= unlimited / never-expires). A field ABSENT is left
    # unchanged. So callers clear a limit back to unlimited by passing it as None.
    cols = {k: v for k, v in fields.items()
            if k in ("daily_token_limit", "daily_request_limit", "rpm_limit",
                     "expires_at", "note")}
    if not cols:
        return 0
    sets = ", ".join(f"{k}=?" for k in cols)
    vals = list(cols.values())
    conn = connect()
    try:
        if api_key:
            vals.append(api_key)
            cur = conn.execute(f"UPDATE users SET {sets} WHERE api_key=?", vals)
        else:
            vals.append(username)
            cur = conn.execute(f"UPDATE users SET {sets} WHERE username=?", vals)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_user(api_key=None, username=None):
    conn = connect()
    try:
        if api_key:
            cur = conn.execute("DELETE FROM users WHERE api_key=?", (api_key,))
        else:
            cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_users():
    conn = connect(readonly=True)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY username")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# In-memory user cache + per-day quota counters
# ---------------------------------------------------------------------------

USERS = {}                       # api_key -> dict(user row)
_users_lock = threading.Lock()   # guards reload swap (writers only)

_counter_lock = threading.Lock()
usage_today = {}                 # username -> {"day", "tokens", "requests", "times": [..]}


def reload_users():
    rows = {}
    for u in list_users():
        rows[u["api_key"]] = u
    global USERS
    with _users_lock:
        USERS = rows
    return len(rows)


# Transition mode: when off (default), unknown keys are auto-registered so the
# switch to per-key auth doesn't break anyone who's on an old/arbitrary key.
# Set RELAY_STRICT=1 to reject unknown keys instead.
STRICT = bool(int(os.environ.get("RELAY_STRICT", "0")))

_ensure_lock = threading.Lock()


def ensure_transition_user(api_key):
    """Auto-register an unknown key as a user (permissive mode). Cached after first."""
    u = USERS.get(api_key)
    if u:
        return u
    with _ensure_lock:
        u = USERS.get(api_key)
        if u:
            return u
        uname = "auto-" + (api_key[-6:] if len(api_key) >= 6 else api_key or "anon")
        try:
            add_user(uname, api_key=api_key, note="auto (transition)")
        except Exception:
            pass  # racing insert / duplicate — fine, just reload
        reload_users()
        return USERS.get(api_key)


def _counter(username):
    c = usage_today.get(username)
    today = _today()
    if c is None or c["day"] != today:
        c = {"day": today, "tokens": 0, "requests": 0, "times": []}
        usage_today[username] = c
    return c


def backfill_today():
    """Restore today's per-user totals from the DB so a restart keeps quotas."""
    today = _today()
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT username, COUNT(*) reqs,"
            " SUM(input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens) toks"
            " FROM usage WHERE day=? GROUP BY username", (today,)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    with _counter_lock:
        for r in rows:
            c = _counter(r["username"])
            c["requests"] = r["reqs"] or 0
            c["tokens"] = r["toks"] or 0


def authenticate(api_key):
    """Return (user_dict, None) if allowed, or (None, reason) if not."""
    u = USERS.get(api_key)
    if not u:
        return None, "unknown_key"
    if not u.get("enabled"):
        return None, "disabled"
    exp = u.get("expires_at")
    if exp and int(time.time() * 1000) > exp:
        return None, "expired"
    return u, None


def admit_request(user):
    """Reserve one request slot and return a quota error, if any."""
    name = user["username"]
    with _counter_lock:
        c = _counter(name)
        dtl = user.get("daily_token_limit")
        drl = user.get("daily_request_limit")
        rpm = user.get("rpm_limit")
        if drl and c["requests"] >= drl:
            return f"今日请求数已达上限({drl})"
        if dtl and c["tokens"] >= dtl:
            return f"今日 token 额度已用尽({dtl})"
        now = time.time()
        if rpm:
            c["times"] = [t for t in c["times"] if now - t < 60]
            if len(c["times"]) >= rpm:
                return f"请求过于频繁(每分钟上限 {rpm})"
            c["times"].append(now)
        c["requests"] += 1
    return None


def check_quota(user):
    """Compatibility wrapper that checks without reserving a request."""
    name = user["username"]
    with _counter_lock:
        c = _counter(name)
        if user.get("daily_request_limit") and c["requests"] >= user["daily_request_limit"]:
            return f"今日请求数已达上限({user['daily_request_limit']})"
        if user.get("daily_token_limit") and c["tokens"] >= user["daily_token_limit"]:
            return f"今日 token 额度已用尽({user['daily_token_limit']})"
    return None


def note_request_started(username):
    with _counter_lock:
        _counter(username)["requests"] += 1


def add_tokens(username, tokens):
    with _counter_lock:
        _counter(username)["tokens"] += tokens


# ---------------------------------------------------------------------------
# Background usage writer
# ---------------------------------------------------------------------------

_write_q = queue.Queue(maxsize=10000)
_writer_started = False


def _writer_loop():
    conn = connect()
    while True:
        rec = _write_q.get()
        if rec is None:
            break
        batch = [rec]
        try:
            while len(batch) < 200:
                batch.append(_write_q.get_nowait())
        except queue.Empty:
            pass
        try:
            conn.executemany(
                "INSERT INTO usage(ts, day, username, api_key, model, stream, status,"
                " input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
                " cost_usd, latency_ms, path, error, client)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
            conn.commit()
        except Exception as e:
            print(f"[{ts()}] usage writer error: {e}", flush=True)


def start_writer():
    global _writer_started
    if _writer_started:
        return
    _writer_started = True
    threading.Thread(target=_writer_loop, daemon=True).start()


def record_usage(username, api_key, model, stream, status, input_tokens,
                 output_tokens, cache_creation_tokens, cache_read_tokens,
                 latency_ms, path, error=None, client="other"):
    cost = compute_cost(model, input_tokens, output_tokens,
                        cache_creation_tokens, cache_read_tokens)
    total = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    if total:
        add_tokens(username, total)
    rec = (int(time.time() * 1000), _today(), username, api_key, model,
           1 if stream else 0, status, input_tokens, output_tokens,
           cache_creation_tokens, cache_read_tokens, cost, latency_ms, path, error, client)
    try:
        _write_q.put_nowait(rec)
    except queue.Full:
        pass  # never block the request path on accounting
    return cost


def reprice_usage(apply=False):
    """Recompute known-model usage estimates from the current price table."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, model, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens, cost_usd "
            "FROM usage WHERE COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) "
            "+ COALESCE(cache_creation_tokens, 0) + COALESCE(cache_read_tokens, 0) > 0"
        ).fetchall()
        eligible = []
        for row in rows:
            if not row["model"] or price_for(row["model"]) is _DEFAULT_PRICE:
                continue
            cost = compute_cost(
                row["model"], row["input_tokens"], row["output_tokens"],
                row["cache_creation_tokens"], row["cache_read_tokens"],
            )
            eligible.append((row, cost))
        before = sum(row["cost_usd"] or 0 for row, _ in eligible)
        after = sum(cost for _, cost in eligible)
        changed = sum(1 for row, cost in eligible if (row["cost_usd"] or 0) != cost)
        if apply and eligible:
            conn.executemany(
                "UPDATE usage SET cost_usd=? WHERE id=?",
                [(cost, row["id"]) for row, cost in eligible],
            )
            conn.commit()
        return {
            "eligible": len(eligible),
            "changed": changed,
            "before": round(before, 6),
            "after": round(after, 6),
            "applied": bool(apply and eligible),
        }
    finally:
        conn.close()



def _usage_values(usage):
    usage = usage or {}
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    return (
        int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
        int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
        0,
        int(details.get("cached_tokens") or usage.get("cached_tokens") or 0),
    )


def parse_nonstream_usage(body_bytes):
    try:
        return _usage_values((json.loads(body_bytes).get("usage") or {}))
    except (TypeError, ValueError, AttributeError):
        return (0, 0, 0, 0)


def parse_stream_usage(*texts):
    """Extract the latest Responses usage object from complete SSE text."""
    result = (0, 0, 0, 0)
    for line in "".join(texts).splitlines():
        if not line.lstrip().startswith("data:"):
            continue
        payload = line.split(":", 1)[1].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except (TypeError, ValueError):
            continue
        usage = event.get("usage")
        if usage is None and isinstance(event.get("response"), dict):
            usage = event["response"].get("usage")
        if usage:
            result = _usage_values(usage)
    return result
