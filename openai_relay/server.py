#!/usr/bin/env python3
"""OpenAI-compatible relay with multi-user quotas and a local dashboard."""

import json
import time
import hmac
import hashlib
import http.server
import http.client
import urllib.request
import urllib.error
import ssl
import socket
import os
import sys
import secrets
import threading
from datetime import datetime, timezone, timedelta
from html import escape
from urllib.parse import urlparse, parse_qs, quote

from openai_relay import __version__
from openai_relay import auth as relay_auth
from openai_relay import db as relay_db

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

LISTEN_PORT = int(os.environ.get("RELAY_PORT", "55302"))
CUSTOM_API_KEY = os.environ.get("RELAY_LEGACY_KEY", "")
_ADMIN_KEY_FROM_ENV = os.environ.get("RELAY_ADMIN_KEY")
ADMIN_KEY = _ADMIN_KEY_FROM_ENV or ("relay-admin-" + secrets.token_hex(12))
STATS_SECRET = ADMIN_KEY.encode()
CLASH_PROXY_HOST = os.environ.get("RELAY_PROXY_HOST", "127.0.0.1")
CLASH_PROXY_PORT = int(os.environ.get("RELAY_PROXY_PORT", "7897"))
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300
CLIENT_TIMEOUT = 180
UPSTREAM_AUTH_MODE = os.environ.get("OPENAI_UPSTREAM_AUTH_MODE", "api_key")
if UPSTREAM_AUTH_MODE not in ("api_key", "codex_auth_file"):
    raise RuntimeError("unsupported upstream auth mode")
_default_host = "chatgpt.com" if UPSTREAM_AUTH_MODE == "codex_auth_file" else "api.openai.com"
_default_prefix = "/backend-api/codex" if UPSTREAM_AUTH_MODE == "codex_auth_file" else "/v1"
UPSTREAM_HOST = os.environ.get("OPENAI_UPSTREAM_HOST", _default_host)
UPSTREAM_PREFIX = os.environ.get("OPENAI_UPSTREAM_PREFIX", _default_prefix)
UPSTREAM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MAX_BODY_BYTES = int(os.environ.get("RELAY_MAX_BODY_BYTES", str(20 * 1024 * 1024)))


MODEL_LIST = tuple(
    model.strip() for model in os.environ.get("RELAY_MODELS", "gpt-5.6-sol").split(",")
    if model.strip()
)


def get_upstream_credentials():
    if UPSTREAM_AUTH_MODE == "codex_auth_file":
        credentials = relay_auth.get_credentials()
        return credentials.access_token, credentials.account_id
    return UPSTREAM_API_KEY, None


def _shorten_codex_input_id(value):
    return "rs_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:61]


def prepare_codex_request(request_data):
    """Normalize only fields unsupported by the Codex backend."""
    prepared = dict(request_data)
    prepared.pop("max_output_tokens", None)
    prepared["store"] = False

    input_items = request_data.get("input")
    if isinstance(input_items, list):
        replacements = {
            item["id"]: _shorten_codex_input_id(item["id"])
            for item in input_items
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and len(item["id"]) > 64
            and "encrypted_content" not in item
        }
        prepared["input"] = [dict(item) if isinstance(item, dict) else item for item in input_items]
        for item in prepared["input"]:
            if isinstance(item, dict) and item.get("id") in replacements:
                item["id"] = replacements[item["id"]]
    return prepared


def ts():
    return time.strftime("%H:%M:%S")


def make_https_conn_via_proxy():
    sock = socket.create_connection((CLASH_PROXY_HOST, CLASH_PROXY_PORT), timeout=CONNECT_TIMEOUT)
    connect_req = f"CONNECT {UPSTREAM_HOST}:443 HTTP/1.1\r\nHost: {UPSTREAM_HOST}:443\r\n\r\n"
    sock.sendall(connect_req.encode())
    resp_data = b""
    while b"\r\n\r\n" not in resp_data:
        part = sock.recv(4096)
        if not part:
            raise ConnectionError("proxy closed CONNECT response")
        resp_data += part
    status_line = resp_data.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if " 200 " not in status_line:
        sock.close()
        raise ConnectionError(f"CONNECT failed: {status_line}")
    ctx = ssl.create_default_context()
    ssl_sock = ctx.wrap_socket(sock, server_hostname=UPSTREAM_HOST)
    ssl_sock.settimeout(READ_TIMEOUT)
    conn = http.client.HTTPSConnection(UPSTREAM_HOST)
    conn.sock = ssl_sock
    return conn


# Idle upstream connections kept alive for reuse (avoids TLS handshake per request).
_conn_pool = []
_conn_pool_lock = threading.Lock()
_MAX_POOL = 32


def get_conn():
    with _conn_pool_lock:
        if _conn_pool:
            return _conn_pool.pop()
    return make_https_conn_via_proxy()


def release_conn(conn):
    with _conn_pool_lock:
        if len(_conn_pool) < _MAX_POOL:
            _conn_pool.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass


def _extract_key(headers):
    key = headers.get("x-api-key", "")
    if key:
        return key
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


class OpenAIRelayHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 so browser-based clients (the OpenAI desktop app / Chromium)
    # get keep-alive + proper response framing. Under the default HTTP/1.0 the
    # desktop's connection-pool reuse hit closed sockets -> ECONNRESET.
    protocol_version = "HTTP/1.1"
    # Don't let a stalled client (stopped reading mid-stream) hang the worker
    # thread forever on wfile.write — bound the client-facing socket.
    timeout = CLIENT_TIMEOUT

    def log_message(self, format, *args):
        pass

    # -- small response helpers (always set Content-Length for keep-alive) --
    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, html, set_cookie=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, location, set_cookie=None):
        self.send_response(303)   # See Other -> browser does a GET, no resubmit
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_form(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        raw = self.rfile.read(n).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _read_json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def check_auth(self):
        """Validate the caller's key against the DB. Sets self.relay_user / .relay_key."""
        key = _extract_key(self.headers)
        user, reason = relay_db.authenticate(key)
        if user is None and reason == "unknown_key" and key and not relay_db.STRICT:
            # Permissive transition: auto-register the key so nobody breaks.
            user = relay_db.ensure_transition_user(key)
        if user is None:
            if reason == "unknown_key":
                self._send_json(401, {"type": "error", "error": {
                    "type": "authentication_error", "message": "无效的 API key"}})
            else:
                self._send_json(403, {"type": "error", "error": {
                    "type": "permission_error",
                    "message": "key 已禁用" if reason == "disabled" else "key 已过期"}})
            return False
        self.relay_user = user
        self.relay_key = key
        return True

    # ------------------------------------------------------------------ local
    def _handle_local(self):
        """Handle local pages/endpoints. Return True if handled."""
        path = urlparse(self.path).path
        if path == "/":
            return self._handle_home()
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return True
        if path in ("/v1/models", "/models") and UPSTREAM_AUTH_MODE == "codex_auth_file":
            if not self.check_auth():
                return True
            self._send_json(200, {
                "object": "list",
                "data": [{
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "openai",
                } for model in MODEL_LIST],
            })
            return True
        if path == "/static/echarts.min.js":
            return self._serve_static("echarts.min.js", "application/javascript")
        if path == "/stats":
            return self._handle_stats()
        if path in ("/admin", "/admin/"):
            return self._handle_admin_page()
        if path.startswith("/admin/"):
            return self._handle_admin(path)
        return False

    def _handle_home(self):
        self._send_html(200, HOME_HTML())
        return True

    def _serve_static(self, name, ctype):
        fp = os.path.join(_STATIC_DIR, os.path.basename(name))
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(body)
        return True

    # -- cookie-session helpers (log in once, refresh stays logged in) --
    @staticmethod
    def _sign(payload):
        return hmac.new(STATS_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]

    def _make_cookie(self, scope):
        payload = scope  # "ALL" or "u:<username>"
        val = f"{payload}.{self._sign(payload)}"
        # 7-day session; HttpOnly so JS can't read it. Path=/ so the same login
        # works for both /stats and /admin (admin = scope "ALL").
        return f"relaystats={val}; Path=/; Max-Age=604800; HttpOnly; SameSite=Lax"

    def _scope_from_cookie(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("relaystats="):
                val = part[len("relaystats="):]
                if "." in val:
                    payload, sig = val.rsplit(".", 1)
                    if hmac.compare_digest(sig, self._sign(payload)):
                        return payload
        return None

    def _scope_for_key(self, key):
        """Return 'ALL' (admin), 'u:<name>' (valid user), or None."""
        if not key:
            return None
        if key == ADMIN_KEY:
            return "ALL"
        user, _ = relay_db.authenticate(key)
        return f"u:{user['username']}" if user else None

    @staticmethod
    def _scope_username(scope):
        return None if scope == "ALL" else scope[2:]  # strip "u:"

    def _handle_stats(self):
        # POST = login submit -> set cookie, then redirect to GET (PRG pattern so
        # a refresh is a plain GET, not a form resubmit).
        if self.command == "POST":
            key = self._read_form().get("key", "").strip()
            scope = self._scope_for_key(key)
            if scope is None:
                self._send_redirect("/stats?err=1")
                return True
            self._send_redirect("/stats", set_cookie=self._make_cookie(scope))
            return True
        # GET: cookie -> direct; ?key=/header -> script fallback; else login form
        q = parse_qs(urlparse(self.path).query)
        scope = self._scope_from_cookie()
        if scope is None:
            qkey = q.get("key", [""])[0] or _extract_key(self.headers)
            scope = self._scope_for_key(qkey)
        if scope is None:
            self._send_html(200, LOGIN_FORM_HTML(error="err" in q))
            return True
        dr = q.get("range", ["period"])[0]
        if dr not in ("today", "7", "30", "period"):
            dr = "period"
        self._send_html(200, render_stats_html(self._scope_username(scope), dr))
        return True

    def _handle_admin(self, path):
        if _extract_key(self.headers) != ADMIN_KEY:
            self._send_json(403, {"error": "需要管理员 key"})
            return True
        action = path[len("/admin/"):]
        if self.command == "GET" and action == "users":
            self._send_json(200, {"users": relay_db.list_users()})
            return True
        if self.command == "POST":
            body = self._read_json_body()
            if action == "users":  # create
                uname = body.get("username")
                if not uname:
                    self._send_json(400, {"error": "username 必填"})
                    return True
                key = relay_db.add_user(
                    uname,
                    daily_token_limit=body.get("daily_token_limit"),
                    daily_request_limit=body.get("daily_request_limit"),
                    rpm_limit=body.get("rpm_limit"),
                    expires_at=body.get("expires_at"),
                    note=body.get("note"),
                )
                relay_db.reload_users()
                self._send_json(200, {"username": uname, "api_key": key})
                return True
            if action in ("disable", "enable"):
                n = relay_db.set_enabled(api_key=body.get("api_key"),
                                         username=body.get("username"),
                                         enabled=1 if action == "enable" else 0)
                relay_db.reload_users()
                self._send_json(200, {"updated": n})
                return True
            if action == "update":
                fields = {k: v for k, v in body.items()
                          if k in ("daily_token_limit", "daily_request_limit",
                                   "rpm_limit", "expires_at", "note")}
                n = relay_db.update_limits(api_key=body.get("api_key"),
                                           username=body.get("username"), **fields)
                relay_db.reload_users()
                self._send_json(200, {"updated": n})
                return True
            if action == "delete":
                n = relay_db.delete_user(api_key=body.get("api_key"),
                                         username=body.get("username"))
                relay_db.reload_users()
                self._send_json(200, {"deleted": n})
                return True
            if action == "reload":
                self._send_json(200, {"users_loaded": relay_db.reload_users()})
                return True
        self._send_json(404, {"error": "未知管理接口"})
        return True

    # ----------------------------------------------------------- admin web UI
    def _handle_admin_page(self):
        """Server-rendered admin panel at /admin (cookie auth, scope 'ALL')."""
        q = parse_qs(urlparse(self.path).query)
        if "logout" in q:
            self._send_html(200, ADMIN_LOGIN_HTML(),
                            set_cookie="relaystats=; Path=/; Max-Age=0")
            return True
        if self.command == "POST":
            form = self._read_form()
            action = form.get("action", "")
            if not action:  # login submit
                if form.get("key", "").strip() == ADMIN_KEY:
                    self._send_redirect("/admin", set_cookie=self._make_cookie("ALL"))
                else:
                    self._send_redirect("/admin?err=1")
                return True
            if self._scope_from_cookie() != "ALL":
                self._send_redirect("/admin?err=1")
                return True
            msg = self._do_admin_action(action, form)
            self._send_redirect(f"/admin?msg={quote(msg)}")
            return True
        # GET
        if self._scope_from_cookie() != "ALL":
            self._send_html(200, ADMIN_LOGIN_HTML(error="err" in q))
            return True
        self._send_html(200, render_admin_html(q.get("msg", [""])[0]))
        return True

    def _do_admin_action(self, action, form):
        def _int(name):
            v = form.get(name, "").strip()
            return int(v) if v.isdigit() else None
        uname = form.get("username", "").strip()
        if action == "create":
            if not uname:
                return "用户名不能为空"
            exp = None
            days = form.get("days", "").strip()
            if days.isdigit() and int(days) > 0:
                exp = int(time.time() * 1000) + int(days) * 86400 * 1000
            relay_db.add_user(uname,
                              daily_token_limit=_int("daily_token_limit"),
                              daily_request_limit=_int("daily_request_limit"),
                              rpm_limit=_int("rpm"), expires_at=exp)
            relay_db.reload_users()
            return f"已创建用户 {uname}"
        if action == "disable":
            relay_db.set_enabled(username=uname, enabled=0); relay_db.reload_users()
            return f"已禁用 {uname}"
        if action == "enable":
            relay_db.set_enabled(username=uname, enabled=1); relay_db.reload_users()
            return f"已启用 {uname}"
        if action == "delete":
            relay_db.delete_user(username=uname); relay_db.reload_users()
            return f"已删除 {uname}"
        if action == "setlimit":
            # Full-replace of the two fields the form shows: empty box -> None ->
            # unlimited. daily_request_limit isn't in the form, so leave it alone.
            relay_db.update_limits(username=uname,
                                   daily_token_limit=_int("daily_token_limit"),
                                   daily_request_limit=_int("daily_request_limit"),
                                   rpm_limit=_int("rpm"))
            relay_db.reload_users()
            return f"已更新 {uname} 的限额"
        return "未知操作"

    # ------------------------------------------------------------------ verbs
    def do_GET(self):
        if self._handle_local():
            return
        if not self.check_auth():
            return
        self._proxy_request("GET")

    def do_POST(self):
        if self._handle_local():
            return
        if not self.check_auth():
            return
        self._proxy_request("POST")

    def do_PUT(self):
        if not self.check_auth():
            return
        self._proxy_request("PUT")

    def do_PATCH(self):
        if not self.check_auth():
            return
        self._proxy_request("PATCH")

    def do_DELETE(self):
        if not self.check_auth():
            return
        self._proxy_request("DELETE")

    def do_OPTIONS(self):
        # CORS preflight - respond directly
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        # Explicit zero-length body so HTTP/1.1 keep-alive clients don't wait.
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _proxy_request(self, method):
        start = time.time()
        user = self.relay_user
        uname = user["username"]
        qreason = relay_db.admit_request(user)
        if qreason:
            self._send_json(429, {"type": "error", "error": {
                "type": "rate_limit_error", "message": qreason}})
            return

        path = urlparse(self.path).path
        allowed = ("/v1/responses", "/responses") if UPSTREAM_AUTH_MODE == "codex_auth_file" else (
            "/v1/responses", "/v1/chat/completions", "/responses", "/chat/completions",
            "/v1/models", "/models",
        )
        if path not in allowed:
            self._send_json(404, {"error": {"message": "unsupported endpoint"}})
            return
        upstream_path = self._upstream_path(self.path)
        content_len = int(self.headers.get("Content-Length", 0) or 0)
        if content_len > MAX_BODY_BYTES:
            self._send_json(413, {"error": {"message": "request body too large"}})
            return
        body = self.rfile.read(content_len) if content_len else None
        is_stream = False
        model = ""
        if body and method == "POST":
            try:
                request_data = json.loads(body)
                if not isinstance(request_data, dict):
                    self._send_json(400, {"error": {"message": "request body must be a JSON object"}})
                    return
                if UPSTREAM_AUTH_MODE == "codex_auth_file":
                    request_data = prepare_codex_request(request_data)
                    if not request_data.get("stream"):
                        self._send_json(400, {"error": {"message": "Codex upstream requires stream=true"}})
                        return
                    body = json.dumps(request_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                is_stream = bool(request_data.get("stream", False))
                model = str(request_data.get("model", ""))
            except (TypeError, ValueError):
                pass
        client = relay_db.classify_client(self.headers.get("User-Agent", ""))
        print(f"[{ts()}] {uname} {method} {path} model={model} stream={is_stream}", flush=True)

        try:
            token, account_id = get_upstream_credentials()
        except relay_auth.CredentialError:
            self._send_json(503, {"error": {"message": "upstream credentials are unavailable"}})
            relay_db.record_usage(uname, self.relay_key, model, is_stream, 503,
                                  0, 0, 0, 0, int((time.time() - start) * 1000), path,
                                  error="upstream credentials unavailable", client=client)
            return
        if not token:
            self._send_json(503, {"error": {"message": "upstream API credentials are not configured"}})
            relay_db.record_usage(uname, self.relay_key, model, is_stream, 503,
                                  0, 0, 0, 0, int((time.time() - start) * 1000), path,
                                  error="upstream credentials unavailable", client=client)
            return
        fwd_headers = {
            "Authorization": f"Bearer {token}",
            "Host": UPSTREAM_HOST,
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "text/event-stream" if is_stream else "application/json"),
        }
        if account_id:
            fwd_headers["ChatGPT-Account-Id"] = account_id

        conn = None
        headers_sent = False
        try:
            conn = get_conn()
            conn.request(method, upstream_path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            in_t = out_t = cw = cr = 0
            if is_stream and 200 <= resp.status < 300:
                self.send_response(resp.status)
                headers_sent = True
                for hdr, val in resp.getheaders():
                    if hdr.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(hdr, val)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                stream_text = ""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    stream_text = (stream_text + chunk.decode("utf-8", "replace"))[-4 * 1024 * 1024:]
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                in_t, out_t, cw, cr = relay_db.parse_stream_usage(stream_text)
            else:
                resp_body = resp.read()
                self.send_response(resp.status)
                headers_sent = True
                for hdr, val in resp.getheaders():
                    if hdr.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(hdr, val)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                if 200 <= resp.status < 300:
                    in_t, out_t, cw, cr = relay_db.parse_nonstream_usage(resp_body)

            relay_db.record_usage(uname, self.relay_key, model, is_stream, resp.status,
                                  in_t, out_t, cw, cr, int((time.time() - start) * 1000),
                                  path, client=client)
            if getattr(resp, "will_close", True):
                conn.close()
            else:
                release_conn(conn)
        except BrokenPipeError:
            if conn:
                conn.close()
        except Exception as exc:
            if conn:
                conn.close()
            relay_db.record_usage(uname, self.relay_key, model, is_stream, 502,
                                  0, 0, 0, 0, int((time.time() - start) * 1000),
                                  path, error=str(exc)[:200], client=client)
            if not headers_sent:
                self._send_json(502, {"error": {"message": "upstream request failed"}})

    @staticmethod
    def _upstream_path(request_target):
        parsed = urlparse(request_target)
        suffix = parsed.path[3:] if parsed.path.startswith("/v1") else parsed.path
        path = UPSTREAM_PREFIX.rstrip("/") + suffix
        return path + (("?" + parsed.query) if parsed.query else "")


def LOGIN_FORM_HTML(error=False):
    err = (
        '<p style="color:#e53e3e;margin-top:16px;font-size:14px;text-align:center">'
        "key 不对,请重试</p>"
        if error else ""
    )
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OpenAI Relay · 用量</title>"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "html{font-size:16px}"
        "body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;"
        "background:linear-gradient(165deg,#f9f7f3 0%,#f0ede6 100%);min-height:100vh;"
        "display:flex;align-items:center;justify-content:center;color:#2d2d35}"
        ".card{background:#fff;border-radius:16px;padding:48px 40px 40px;max-width:420px;"
        "width:100%;box-shadow:0 1px 3px rgba(0,0,0,.06),0 4px 14px rgba(0,0,0,.04),"
        "0 8px 32px rgba(0,0,0,.03)}"
        "h1{font-size:22px;font-weight:650;letter-spacing:-.02em;margin-bottom:4px}"
        ".sub{color:#8a8a93;font-size:14px;line-height:1.5;margin-bottom:28px}"
        "input{width:100%;padding:12px 16px;font-size:14px;border:1.5px solid #e2e2e8;"
        "border-radius:10px;outline:none;transition:border-color .15s;font-family:inherit}"
        "input:focus{border-color:#0d9488;box-shadow:0 0 0 3px rgba(13,148,136,.1)}"
        "button{width:100%;margin-top:16px;padding:12px 0;font-size:14px;font-weight:600;"
        "font-family:inherit;color:#fff;background:#0d9488;border:none;border-radius:10px;"
        "cursor:pointer;transition:background .15s}"
        "button:hover{background:#0f766e}"
        ".hint{margin-top:24px;font-size:12px;color:#bcbcbf;text-align:center;line-height:1.5}"
        "</style>"
        "<div class=card>"
        "<h1>OpenAI Relay</h1>"
        "<p class=sub>输入你的 key 查看用量<br>管理 key 可看全员数据</p>"
        f"{err}"
        "<form method=post action=/stats>"
        "<input name=key type=password placeholder='粘贴你的 API key' autofocus>"
        "<button type=submit>登录</button>"
        "</form>"
        "<p class=hint>登录一次会记住 · 7 天内不用重复输入</p>"
        "</div>"
    )


_acct_quota = {"data": None, "ts": 0.0}
_acct_quota_lock = threading.Lock()
_ACCOUNT_QUOTA_TTL = 60


def _format_quota_reset(value):
    if value is None:
        return "重置时间未知"
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.fromtimestamp(float(value), timezone.utc)
        return "重置于 " + dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "重置时间未知"


def _quota_window_label(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "周期窗口"
    if 4 * 3600 <= seconds <= 6 * 3600:
        return "5小时"
    if 6 * 86400 <= seconds <= 8 * 86400:
        return "本周"
    if seconds >= 86400:
        return f"{round(seconds / 86400)} 天窗口"
    if seconds >= 3600:
        return f"{round(seconds / 3600)} 小时窗口"
    return "周期窗口"


def normalize_account_quota(payload):
    """Return only display-safe quota values from Codex's usage response."""
    if not isinstance(payload, dict):
        return []

    rows = []

    def add_windows(prefix, rate_limit):
        if not isinstance(rate_limit, dict):
            return
        for key in ("primary_window", "secondary_window"):
            window = rate_limit.get(key)
            if not isinstance(window, dict):
                continue
            try:
                used = min(max(float(window.get("used_percent", 0)), 0), 100)
            except (TypeError, ValueError):
                continue
            reset = window.get("reset_at")
            if reset is None and window.get("reset_after_seconds") is not None:
                try:
                    reset = time.time() + float(window["reset_after_seconds"])
                except (TypeError, ValueError):
                    pass
            label = _quota_window_label(window.get("limit_window_seconds"))
            rows.append({
                "label": f"{prefix} · {label}" if prefix else label,
                "used_percent": used,
                "reset": _format_quota_reset(reset),
            })

    add_windows("", payload.get("rate_limit") or payload.get("rateLimits"))
    for limit in payload.get("additional_rate_limits") or payload.get("additionalRateLimits") or []:
        if isinstance(limit, dict) and isinstance(limit.get("limit_name"), str):
            add_windows(limit["limit_name"], limit.get("rate_limit") or limit.get("rateLimit"))
    return rows


def fetch_account_quota():
    if UPSTREAM_AUTH_MODE != "codex_auth_file":
        return None
    try:
        token, account_id = get_upstream_credentials()
        conn = make_https_conn_via_proxy()
        try:
            conn.request("GET", "/backend-api/wham/usage", headers={
                "Authorization": f"Bearer {token}",
                "ChatGPT-Account-Id": account_id,
                "OpenAI-Beta": "codex-1",
                "originator": "Codex Desktop",
                "Accept": "application/json",
                "Host": UPSTREAM_HOST,
            })
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                return None
            return normalize_account_quota(json.loads(body))
        finally:
            conn.close()
    except (relay_auth.CredentialError, OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
        return None


def get_account_quota():
    now = time.time()
    if _acct_quota["data"] is not None and now - _acct_quota["ts"] < _ACCOUNT_QUOTA_TTL:
        return _acct_quota["data"]
    with _acct_quota_lock:
        if _acct_quota["data"] is not None and time.time() - _acct_quota["ts"] < _ACCOUNT_QUOTA_TTL:
            return _acct_quota["data"]
        quota = fetch_account_quota()
        if quota is not None:
            _acct_quota["data"] = quota
            _acct_quota["ts"] = time.time()
        return _acct_quota["data"]


def render_account_panel():
    quota = get_account_quota()
    title = ("<h2>上游 Codex 账号额度 "
             "<span class=acct-hint>所有 relay 用户共享同一上游账号</span></h2>")
    if not quota:
        return (title + "<div class=acct-empty><strong>周期额度暂不可用</strong>"
                "当前 Codex 上游未返回可验证的 5 小时、周周期或重置时间。"
                "这里不会用 relay 用量推测或伪造账号额度。</div>")

    def color(used):
        return "#dc2626" if used >= 80 else ("#d97706" if used >= 50 else "#0d9488")

    cards = []
    for item in quota:
        label = escape(str(item.get("label", "未命名周期")))
        used = min(max(float(item.get("used_percent", 0)), 0), 100)
        remaining = 100 - used
        reset = escape(str(item.get("reset", "重置时间未知")))
        fill = color(used)
        cards.append(
            "<div class=acct>"
            f"<div class=acct-top><span class=acct-label>{label}</span>"
            f"<span class=acct-pct style='color:{fill}'>{remaining:.0f}% 剩余</span></div>"
            f"<div class=acct-track><div class=acct-fill "
            f"style='width:{remaining:.0f}%;background:{fill}'></div></div>"
            f"<div class=acct-reset>{reset}</div></div>"
        )
    return title + "<div class=acctwrap>" + "".join(cards) + "</div>"


CSS = (
    "*{margin:0;padding:0;box-sizing:border-box}"
    "html{font-size:16px}"
    "body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;"
    "background:#f6f5f1;color:#2d2d35;line-height:1.5}"
    ".wrap{max-width:1040px;margin:0 auto;padding:32px 24px 48px}"
    ".topbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:28px}"
    ".topbar h1{font-size:24px;font-weight:700;letter-spacing:-.02em}"
    ".topbar .meta{color:#8a8a93;font-size:13px}"
    ".rangesel{display:flex;gap:3px;background:#f0efe9;padding:4px;border-radius:10px}"
    ".rs{font-size:13px;font-weight:500;padding:6px 14px;border-radius:7px;text-decoration:none;"
    "color:#6b6b6e;transition:background .12s,color .12s;white-space:nowrap}"
    ".rs:hover{background:#e8e6de;color:#2d2d35}"
    ".rs.active{background:#fff;color:#2d2d35;box-shadow:0 1px 3px rgba(0,0,0,.1)}"
    ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:36px}"
    ".cards .s{background:#fff;border-radius:12px;padding:20px 22px;"
    "box-shadow:0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.03);"
    "display:flex;flex-direction:column;gap:6px}"
    ".cards .s .label{font-size:12px;color:#8a8a93;text-transform:uppercase;letter-spacing:.05em}"
    ".cards .s .val{font-size:28px;font-weight:700;letter-spacing:-.01em;line-height:1.1}"
    ".cards .s .val.cost{color:#0d9488}"
    ".acct-hint{font-size:12px;font-weight:400;color:#a8a29e;margin-left:4px}"
    ".acctwrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-bottom:8px}"
    ".acct{background:#fff;border-radius:12px;padding:16px 18px;"
    "box-shadow:0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.03)}"
    ".acct-top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:9px}"
    ".acct-label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:600;color:#44403c}"
    ".acct-pct{flex:none;white-space:nowrap;font-size:19px;font-weight:700;font-variant-numeric:tabular-nums}"
    ".acct-track{height:8px;background:#f0efe9;border-radius:5px;overflow:hidden}"
    ".acct-fill{height:100%;border-radius:5px}"
    ".acct-reset{font-size:11px;color:#a8a29e;margin-top:7px}"
    ".acct-empty{background:#fff;border:1px dashed #d6d3d1;border-radius:12px;padding:16px 18px;margin-bottom:8px;color:#78716c;font-size:13px}"
    ".acct-empty strong{display:block;color:#44403c;margin-bottom:3px}"
    ".chartbox{background:#fff;border-radius:12px;padding:16px 18px 14px;"
    "box-shadow:0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.03);margin-bottom:14px}"
    ".charttitle{font-size:13px;font-weight:600;color:#44403c;margin-bottom:6px}"
    ".charts2{display:grid;grid-template-columns:1fr 1fr;gap:14px}"
    "@media(max-width:680px){.charts2{grid-template-columns:1fr}}"
    ".chead{display:flex;align-items:center;justify-content:space-between;margin:32px 0 14px 2px}"
    ".chead h2{margin:0}"
    ".toggle{display:inline-flex;background:#ece9e1;border-radius:9px;padding:3px}"
    ".toggle button{border:none;background:none;font-family:inherit;font-size:12.5px;font-weight:600;"
    "color:#8a8a93;padding:6px 16px;border-radius:7px;cursor:pointer}"
    ".toggle button.active{background:#fff;color:#0d9488;box-shadow:0 1px 2px rgba(0,0,0,.08)}"
    "h2{font-size:17px;font-weight:650;margin:32px 0 14px 2px;letter-spacing:-.01em}"
    "table{width:100%;border-collapse:separate;border-spacing:0;"
    "background:#fff;border-radius:12px;overflow:hidden;"
    "box-shadow:0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.03);"
    "font-size:13px;margin-bottom:6px}"
    "th{text-align:left;padding:10px 14px;font-weight:600;font-size:11px;color:#8a8a93;"
    "text-transform:uppercase;letter-spacing:.05em;background:#fafaf9;border-bottom:1px solid #f0f0ee}"
    "td{padding:10px 14px;border-bottom:1px solid #f5f4f0;white-space:nowrap}"
    "tr:last-child td{border-bottom:none}"
    ".num,.costcell{text-align:right;font-variant-numeric:tabular-nums}"
    ".costbar{display:flex;align-items:center;gap:8px}"
    ".costbar .barwrap{flex:1;min-width:48px;height:5px;background:#f0efe9;border-radius:4px;overflow:hidden}"
    ".costbar .bar{height:100%;border-radius:4px;background:#0d9488}"
    ".badge{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:5px;"
    "letter-spacing:.02em}"
    ".badge.cli{background:#eef2ff;color:#4338ca}"
    ".badge.desktop{background:#ecfdf5;color:#047857}"
    ".badge.other{background:#f5f5f4;color:#78716c;cursor:help;border-bottom:1px dotted #c5c4c0}"
    ".utag{display:inline-block;font-size:10px;font-weight:600;padding:1px 6px;border-radius:4px;"
    "margin-left:7px;vertical-align:middle;cursor:help}"
    ".utag.transition{background:#fff7ed;color:#c2410c}"
    ".utag.test{background:#f5f5f4;color:#a8a29e}"
    ".modelid{max-width:180px;overflow:hidden;text-overflow:ellipsis;display:inline-block;vertical-align:middle}"
    ".mlink{cursor:pointer;color:#6366f1}"
    ".mlink:hover{text-decoration:underline}"
    ".mcost{display:none}"
    ".metricbox.show-cost .mtok{display:none}"
    ".metricbox.show-cost .mcost{display:inline}"
    ".toggle.sm button{padding:4px 12px;font-size:12px}"
    ".ft{margin-top:40px;text-align:center;font-size:11px;color:#c5c4c0}"
    "#toTop{position:fixed;right:24px;bottom:28px;width:40px;height:40px;border-radius:50%;"
    "background:#0d9488;color:#fff;border:none;cursor:pointer;font-size:18px;line-height:40px;"
    "text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.18);opacity:0;pointer-events:none;"
    "transition:opacity .25s}"
    "#toTop.vis{opacity:1;pointer-events:auto}"
    "#toTop:hover{background:#0f766e}"
    ".logout{font-size:12px;color:#0d9488;text-decoration:none;margin-left:12px}"
    ".logout:hover{text-decoration:underline}"
    ".ulink{cursor:pointer;font-weight:600;color:#0d9488}"
    ".ulink:hover{text-decoration:underline}"
    ".dlink{cursor:pointer;font-weight:600;color:#6366f1}"
    ".dlink:hover{text-decoration:underline}"
    ".modal-ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.42);z-index:1000;"
    "align-items:center;justify-content:center}"
    ".modal-ov.open{display:flex}"
    ".modal-card{background:#fff;border-radius:14px;padding:22px 22px 18px;"
    "width:min(860px,94vw);max-height:88vh;overflow-y:auto;position:relative;"
    "box-shadow:0 12px 48px rgba(0,0,0,.22)}"
    "#dayHourModal .modal-card{width:min(1200px,96vw)}"
    "#dayHourTable table{overflow:visible}"
    "#dayHourTable th:first-child{position:sticky;left:0;z-index:2;background:#fafaf9;box-shadow:1px 0 0 #e2e2e8}"
    "#dayHourTable td:first-child{position:sticky;left:0;z-index:1;background:#fff;box-shadow:1px 0 0 #e2e2e8}"
    "#todayHourTable table{overflow:visible}"
    "#todayHourTable th:first-child{position:sticky;left:0;z-index:2;background:#fafaf9;box-shadow:1px 0 0 #e2e2e8}"
    "#todayHourTable td:first-child{position:sticky;left:0;z-index:1;background:#fff;box-shadow:1px 0 0 #e2e2e8}"
    ".modal-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}"
    ".modal-title{font-size:15px;font-weight:700;color:#1c1917}"
    ".modal-close{background:none;border:none;font-size:22px;line-height:1;"
    "cursor:pointer;color:#8a8a93;padding:0 2px}"
    ".modal-close:hover{color:#1c1917}"
)


CHART_JS = """
(function(){
 if(typeof echarts==='undefined'||typeof STATS==='undefined')return;
 var teal='#0d9488';
 var palette=['#0d9488','#6366f1','#f59e0b','#ef4444','#8b5cf6','#10b981','#ec4899','#0ea5e9','#f97316','#64748b'];
 var money=function(v){v=+v;return '$'+(v>=10?v.toFixed(2):v.toFixed(4));};
 var tok=function(v){v=+v;if(v>=1e6)return (v/1e6).toFixed(2)+'M';if(v>=1e3)return (v/1e3).toFixed(1)+'K';return ''+v;};
 function mk(id,opt){var el=document.getElementById(id);if(!el)return null;
   var c=echarts.getInstanceByDom(el)||echarts.init(el);c.setOption(opt,true);return c;}
 function line(name,data,color,area){return {name:name,type:'line',smooth:true,data:data,
   itemStyle:{color:color},lineStyle:{color:color,width:2},symbol:'circle',symbolSize:4,
   areaStyle:area?{color:area}:undefined};}
 function txt(id,s){var el=document.getElementById(id);if(el)el.textContent=s;}

 function render(metric){
   var isTok=(metric==='tok'), fmt=isTok?tok:money;
   // ── day × user line chart (含合计) ──
   txt('tDayUser', isTok?'每日 Token 趋势':'每日费用趋势');
   if(STATS.days&&STATS.days.length){
     var du=STATS.dayUsers;
     var nD=STATS.days.length, zs=nD>7?Math.round((nD-7)/nD*100):0;
     var userSeries=(du&&du.users&&du.users.length>0)?du.users.map(function(u){
       return {name:u,type:'line',smooth:true,symbol:'circle',symbolSize:4,
               data:du[isTok?'tok':'cost'][u],emphasis:{focus:'series'}};
     }):[];
     mk('cDayUser',{
       grid:{left:60,right:18,top:38,bottom:55},
       tooltip:{trigger:'axis',valueFormatter:fmt},
       legend:{top:6,textStyle:{fontSize:11}},
       xAxis:{type:'category',data:STATS.days,
              axisLabel:{fontSize:10,color:'#8a8a93',rotate:STATS.days.length>14?30:0},
              axisLine:{lineStyle:{color:'#e6e2d6'}}},
       yAxis:{type:'value',axisLabel:{formatter:fmt,fontSize:10,color:'#8a8a93'},
              splitLine:{lineStyle:{color:'#f0efe9'}}},
       dataZoom:[{type:'inside',xAxisIndex:0},{type:'slider',xAxisIndex:0,
                  start:zs,end:100,height:18,bottom:5,textStyle:{fontSize:10}}],
       color:palette,
       series:[{name:'合计',type:'line',smooth:true,symbol:'circle',symbolSize:5,
                lineStyle:{width:4},
                data:isTok?STATS.dayTok.total:STATS.dayCost,
                emphasis:{focus:'series'}}
              ].concat(userSeries)
     });
   }
   // ── per user (sort by active metric, biggest on top) ──
   txt('tUser', isTok?'按用户 Token':'按用户费用');
   var us=STATS.users.slice().sort(function(a,b){return a[metric]-b[metric];});
   if(us.length)mk('cUser',{
     grid:{left:74,right:30,top:8,bottom:18},
     tooltip:{trigger:'axis',axisPointer:{type:'shadow'},valueFormatter:fmt},
     xAxis:{type:'value',axisLabel:{formatter:fmt,fontSize:10,color:'#8a8a93'},splitLine:{lineStyle:{color:'#f0efe9'}}},
     yAxis:{type:'category',data:us.map(function(u){return u.name;}),axisLabel:{fontSize:11,color:'#44403c'},axisLine:{lineStyle:{color:'#e6e2d6'}}},
     series:[{type:'bar',data:us.map(function(u){return u[metric];}),itemStyle:{color:teal,borderRadius:[0,4,4,0]},barWidth:'58%'}]
   });
   // ── per model (donut) ──
   txt('tModel', isTok?'按模型 Token 占比':'按模型费用占比');
   var ms=STATS.models.filter(function(m){return m[metric]>0;}).sort(function(a,b){return b[metric]-a[metric];});
   if(ms.length)mk('cModel',{
     tooltip:{trigger:'item',valueFormatter:fmt},
     legend:{type:'scroll',bottom:0,textStyle:{fontSize:11}},
     color:palette,
     series:[{type:'pie',radius:['42%','66%'],center:['50%','44%'],
       data:ms.map(function(m){return {name:m.name,value:m[metric]};}),
       label:{fontSize:10,formatter:'{b} {d}%'},itemStyle:{borderColor:'#fff',borderWidth:2}}]
   });
 }

 var bT=document.getElementById('tgTok'), bC=document.getElementById('tgCost');
 function setMetric(m){
   render(m);
   if(bT)bT.classList.toggle('active', m==='tok');
   if(bC)bC.classList.toggle('active', m==='cost');
 }
 if(bT)bT.onclick=function(){setMetric('tok');};
 if(bC)bC.onclick=function(){setMetric('cost');};
 setMetric('cost');

 window.addEventListener('resize',function(){['cDayUser','cUser','cModel'].forEach(function(id){
   var el=document.getElementById(id);if(el){var c=echarts.getInstanceByDom(el);if(c)c.resize();}});});

 // ── user detail modal ──
 var _ov=document.getElementById('userModal');
 if(_ov&&STATS.userModelDays){
   var _mt=document.getElementById('modalTitle');
   var _mTok=document.getElementById('mTok'),_mCost=document.getElementById('mCost');
   var _mUser=null;
   function _mRender(u,metric){
     _mUser=u;
     var udm=STATS.userModelDays[u]; if(!udm)return;
     var isTok=metric==='tok', fmt=isTok?tok:money;
     var fams=Object.keys(udm).filter(function(f){
       return udm[f][metric].some(function(v){return v>0;});});
     var nD=STATS.days?STATS.days.length:0, zs=nD>7?Math.round((nD-7)/nD*100):0;
     mk('cUserModal',{
       grid:{left:60,right:18,top:38,bottom:55},
       tooltip:{trigger:'axis',valueFormatter:fmt},
       legend:{top:6,textStyle:{fontSize:11}},
       xAxis:{type:'category',data:STATS.days,
              axisLabel:{fontSize:10,color:'#8a8a93'},axisLine:{lineStyle:{color:'#e6e2d6'}}},
       yAxis:{type:'value',axisLabel:{formatter:fmt,fontSize:10,color:'#8a8a93'},
              splitLine:{lineStyle:{color:'#f0efe9'}}},
       dataZoom:[{type:'inside',xAxisIndex:0},
                 {type:'slider',xAxisIndex:0,start:zs,end:100,height:18,bottom:5,textStyle:{fontSize:10}}],
       color:palette,
       series:fams.map(function(f){
         return {name:f,type:'line',smooth:true,symbol:'circle',symbolSize:4,
                 data:udm[f][metric],emphasis:{focus:'series'}};})
     });
   }
   document.querySelectorAll('.ulink').forEach(function(el){
     el.onclick=function(e){
       e.stopPropagation();
       var u=el.getAttribute('data-u');
       _mt.textContent=u;
       _ov.classList.add('open');
       _mCost.classList.add('active');_mTok.classList.remove('active');
       _mRender(u,'cost');
     };
   });
   document.getElementById('modalClose').onclick=function(){_ov.classList.remove('open');};
   _ov.onclick=function(e){if(e.target===_ov)_ov.classList.remove('open');};
   _mTok.onclick=function(){_mTok.classList.add('active');_mCost.classList.remove('active');_mRender(_mUser,'tok');};
   _mCost.onclick=function(){_mCost.classList.add('active');_mTok.classList.remove('active');_mRender(_mUser,'cost');};
 }

 // ── day detail modal (hourly table by user) ──
 var _dov=document.getElementById('dayHourModal');
 if(_dov&&STATS.hourlyDays){
   var _dTitle=document.getElementById('dayHourTitle');
   var _dTbl=document.getElementById('dayHourTable');
   var _dDay=null;
   var _dTok=document.getElementById('dhTok'),_dCost=document.getElementById('dhCost');
   function _dRender(day,metric){
     _dDay=day;
     var hd=STATS.hourlyDays[day];if(!hd)return;
     var isTok=metric==='tok',fmt=isTok?tok:money;
     var hds=hd.h.map(function(h){return '<th class=num>'+h+':00</th>';}).join('');
     var html='<div style="overflow-x:auto"><table>'
       +'<thead><tr><th>用户</th>'+hds+'<th class=num>合计</th></tr></thead><tbody>';
     var sumCols=hd.h.map(function(h,i){
       var total=hd.u.reduce(function(s,u){return s+((hd[metric][u]||[])[i]||0);},0);
       return '<td class=num>'+(total?fmt(total):'—')+'</td>';
     }).join('');
     var grand=hd.u.reduce(function(s,u){
       return s+(hd[metric][u]||[]).reduce(function(a,b){return a+b;},0);},0);
     html+='<tr><td><b>合计</b></td>'+sumCols+'<td class=num><b>'+fmt(grand)+'</b></td></tr>';
     hd.u.forEach(function(u){
       var vals=hd[metric][u]||[];
       var tds=vals.map(function(v){return '<td class=num>'+(v?fmt(v):'—')+'</td>';}).join('');
       var rt=vals.reduce(function(a,b){return a+b;},0);
       html+='<tr><td>'+u+'</td>'+tds+'<td class=num><b>'+(rt?fmt(rt):'—')+'</b></td></tr>';
     });
     html+='</tbody></table></div>';
     _dTbl.innerHTML=html;
   }
   document.querySelectorAll('.dlink').forEach(function(el){
     el.onclick=function(e){
       e.stopPropagation();
       var day=el.getAttribute('data-day');
       _dTitle.textContent=day+' 每小时(按用户)';
       _dov.classList.add('open');
       _dCost.classList.add('active');_dTok.classList.remove('active');
       _dRender(day,'cost');
     };
   });
   document.getElementById('dayHourClose').onclick=function(){_dov.classList.remove('open');};
   _dov.onclick=function(e){if(e.target===_dov)_dov.classList.remove('open');};
   _dTok.onclick=function(){_dTok.classList.add('active');_dCost.classList.remove('active');_dRender(_dDay,'tok');};
   _dCost.onclick=function(){_dCost.classList.add('active');_dTok.classList.remove('active');_dRender(_dDay,'cost');};
 }
 // ── model detail modal (per-user line chart) ──
 var _mmov=document.getElementById('modelModal');
 if(_mmov&&STATS.modelUserDays){
   var _mmTitle=document.getElementById('modelModalTitle');
   var _mmTok=document.getElementById('mmTok'),_mmCost=document.getElementById('mmCost');
   var _mmModel=null;
   function _mmRender(model,metric){
     _mmModel=model;
     var mdu=STATS.modelUserDays[model];if(!mdu)return;
     var isTok=metric==='tok',fmt=isTok?tok:money;
     var users=Object.keys(mdu).filter(function(u){
       return mdu[u][metric].some(function(v){return v>0;});});
     var nD=STATS.days?STATS.days.length:0,zs=nD>7?Math.round((nD-7)/nD*100):0;
     mk('cModelModal',{
       grid:{left:60,right:18,top:38,bottom:55},
       tooltip:{trigger:'axis',valueFormatter:fmt},
       legend:{top:6,textStyle:{fontSize:11}},
       xAxis:{type:'category',data:STATS.days,
              axisLabel:{fontSize:10,color:'#8a8a93'},axisLine:{lineStyle:{color:'#e6e2d6'}}},
       yAxis:{type:'value',axisLabel:{formatter:fmt,fontSize:10,color:'#8a8a93'},
              splitLine:{lineStyle:{color:'#f0efe9'}}},
       dataZoom:[{type:'inside',xAxisIndex:0},
                 {type:'slider',xAxisIndex:0,start:zs,end:100,height:18,bottom:5,textStyle:{fontSize:10}}],
       color:palette,
       series:users.map(function(u){
         return {name:u,type:'line',smooth:true,symbol:'circle',symbolSize:4,
                 data:mdu[u][metric],emphasis:{focus:'series'}};})
     });
   }
   document.querySelectorAll('.mlink').forEach(function(el){
     el.onclick=function(e){
       e.stopPropagation();
       var m=el.getAttribute('data-m');
       _mmTitle.textContent=m+' · 各用户用量';
       _mmov.classList.add('open');
       _mmCost.classList.add('active');_mmTok.classList.remove('active');
       _mmRender(m,'cost');
     };
   });
   document.getElementById('modelModalClose').onclick=function(){_mmov.classList.remove('open');};
   _mmov.onclick=function(e){if(e.target===_mmov)_mmov.classList.remove('open');};
   _mmTok.onclick=function(){_mmTok.classList.add('active');_mmCost.classList.remove('active');_mmRender(_mmModel,'tok');};
   _mmCost.onclick=function(){_mmCost.classList.add('active');_mmTok.classList.remove('active');_mmRender(_mmModel,'cost');};
 }
 document.addEventListener('keydown',function(e){
   if(e.key==='Escape'){
     document.querySelectorAll('.modal-ov.open').forEach(function(m){m.classList.remove('open');});
   }
 });
})();
"""


def user_tag(name):
    """Small marker for non-person system accounts in the stats tables."""
    if name == "legacy":
        return '<span class="utag transition" title="共享过渡 key,尚未换独立 key 的请求都归这里">过渡</span>'
    if name.startswith("auto-"):
        return '<span class="utag test" title="宽松模式下自动注册的未知 key(多为测试)">测试</span>'
    return ""


def render_stats_html(scope_username, date_range="period"):
    conn = relay_db.connect(readonly=True)
    _today_str = time.strftime("%Y-%m-%d")
    _wparts, params = [], []
    if scope_username:
        _wparts.append("username=?")
        params.append(scope_username)
    if date_range == "today":
        _wparts.append("day=?")
        params.append(_today_str)
    elif date_range == "7":
        _wparts.append("day>=?")
        params.append(time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400)))
    elif date_range == "period":
        _wparts.append("day>=?")
        params.append(time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400)))
    else:
        _wparts.append("day>=?")
        params.append(time.strftime("%Y-%m-%d", time.localtime(time.time() - 29 * 86400)))
    where = ("WHERE " + " AND ".join(_wparts)) if _wparts else ""
    try:
        per_user = conn.execute(
            f"SELECT username, COUNT(*) reqs,"
            f" SUM(input_tokens) it, SUM(output_tokens) ot,"
            f" SUM(cache_creation_tokens) ct, SUM(cache_read_tokens) rt,"
            f" SUM(cost_usd) cost FROM usage {where} GROUP BY username ORDER BY cost DESC",
            params).fetchall()
        by_day = conn.execute(
            f"SELECT day, COUNT(*) reqs, SUM(input_tokens) it, SUM(output_tokens) ot,"
            f" SUM(input_tokens+output_tokens) tok,"
            f" SUM(cost_usd) cost FROM usage {where} GROUP BY day ORDER BY day DESC LIMIT 30",
            params).fetchall()
        by_model = conn.execute(
            f"SELECT model, COUNT(*) reqs, SUM(input_tokens) it, SUM(output_tokens) ot,"
            f" SUM(cost_usd) cost FROM usage {where} GROUP BY model ORDER BY cost DESC",
            params).fetchall()
        by_client = conn.execute(
            f"SELECT username, COALESCE(client,'other') client, COUNT(*) reqs,"
            f" SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage {where} GROUP BY username, COALESCE(client,'other')"
            f" ORDER BY username, cost DESC",
            params).fetchall()
        by_user_model = conn.execute(
            f"SELECT username, COALESCE(model,'-') model, COUNT(*) reqs,"
            f" SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage {where} GROUP BY username, COALESCE(model,'-')"
            f" ORDER BY username, cost DESC",
            params).fetchall()
        by_day_user = conn.execute(
            f"SELECT day, username, COUNT(*) reqs,"
            f" SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage {where} GROUP BY day, username"
            f" ORDER BY day DESC, cost DESC",
            params).fetchall()
        by_day_user_model = conn.execute(
            f"SELECT day, username, COALESCE(model,'-') model,"
            f" SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage {where} GROUP BY day, username, COALESCE(model,'-')"
            f" ORDER BY day, username, cost DESC",
            params).fetchall()
        _h_cond = "day=?"
        _h_params = [_today_str]
        if scope_username:
            _h_cond += " AND username=?"
            _h_params.append(scope_username)
        by_hour = conn.execute(
            f"SELECT strftime('%H', datetime(ts/1000, 'unixepoch', 'localtime')) hour,"
            f" username, COUNT(*) reqs,"
            f" SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage WHERE {_h_cond}"
            f" GROUP BY hour, username ORDER BY hour, cost DESC",
            _h_params).fetchall()
        by_hour_all = conn.execute(
            f"SELECT strftime('%Y-%m-%d', datetime(ts/1000, 'unixepoch', 'localtime')) day,"
            f" strftime('%H', datetime(ts/1000, 'unixepoch', 'localtime')) hour,"
            f" username, SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage {where}"
            f" GROUP BY day, hour, username ORDER BY day, hour",
            params).fetchall()
        by_hour_agg = conn.execute(
            f"SELECT strftime('%H', datetime(ts/1000, 'unixepoch', 'localtime')) hour,"
            f" SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage {where} GROUP BY hour ORDER BY hour",
            params).fetchall()
    except Exception as e:
        conn.close()
        return f"<h3 style='padding:40px;text-align:center;color:#8a8a93'>暂无数据或查询出错: {e}</h3>"
    conn.close()

    # Aggregates for summary cards
    total_reqs = sum(r["reqs"] or 0 for r in per_user)
    total_tok = sum((r["it"] or 0) + (r["ot"] or 0) + (r["ct"] or 0) + (r["rt"] or 0) for r in per_user)
    total_cost = sum(r["cost"] or 0 for r in per_user)
    today_cost = sum(r["cost"] or 0 for r in by_day if len(by_day) > 0 and r["day"] == by_day[0]["day"])

    def num(v):
        if (v or 0) >= 1_000_000:
            return f"{v/1_000_000:.2f}M"
        if (v or 0) >= 1_000:
            return f"{int(v):,}"
        return str(int(v or 0))

    def money(v):
        vv = v or 0
        if vv >= 10:
            return f"${vv:,.2f}"
        return f"${vv:.4f}"

    def bars(r, total_ref, field):
        """Render a cost/token cell with a percentage bar."""
        val = r[field] or 0
        pct = min(val / (total_ref or 1) * 100, 100)
        return (
            f"<td class=costcell><div class=costbar>"
            f"<span>{money(val) if field == 'cost' else num(val)}</span>"
            f"<div class=barwrap><div class=bar style='width:{pct:.0f}%'></div></div>"
            f"</div></td>"
        )

    def badge(client):
        labels = {"cli": ("CLI", "badge cli"), "desktop": ("桌面版", "badge desktop")}
        if client in labels:
            label, cls = labels[client]
            return f'<span class="{cls}">{label}</span>'
        tip = "未识别客户端:含早期未记录类型的请求,以及 UA 既非 CLI 也非桌面版的请求"
        return f'<span class="badge other" title="{tip}">{client}</span>'

    def metric_cell(tv, cv, bold=False):
        if not tv and not cv:
            return "<td class=num>—</td>"
        inner = (f"<span class=mtok>{num(tv)}</span>"
                 f"<span class=mcost>{money(cv)}</span>")
        if bold:
            inner = f"<b>{inner}</b>"
        return f"<td class=num data-tok='{int(tv or 0)}' data-cost='{float(cv or 0)}'>{inner}</td>"

    max_cost = max((r["cost"] or 0 for r in per_user), default=1)

    title_scope = scope_username or "全部用户"
    now = time.strftime("%Y-%m-%d %H:%M")

    parts = [
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>OpenAI Relay · {title_scope}</title>",
        "<style>", CSS, "</style>",
        "<div class=wrap>",
        # ── header ──
        "<div class=topbar>",
        f"<div><h1>OpenAI Relay · {title_scope}</h1><span class=meta>{now} &nbsp;刷新页面更新</span></div>",
        "<div class=rangesel>"
        + "".join(
            f"<a href='/stats?range={r}' class='rs{' active' if date_range==r else ''}'>{lbl}</a>"
            for r, lbl in (("period","本周期"),("today","今日"),("7","近7天"),("30","近30天"))
        )
        + "</div>",
        "</div>",
        # ── summary cards ──
        "<div class=cards>",
        f"<div class=s><span class=label>总请求</span><span class=val>{total_reqs:,}</span></div>",
        f"<div class=s><span class=label>总 Token</span><span class=val>{num(total_tok)}</span></div>",
        f"<div class=s><span class=label>期间总费用</span><span class='val cost'>{money(total_cost)}</span></div>",
        f"<div class=s><span class=label>今日费用</span><span class='val cost'>{money(today_cost)}</span></div>",
        "</div>",
        render_account_panel(),
    ]

    # ── charts (ECharts, served locally) ──
    days_rev = list(reversed(by_day))

    def io_tok(r):  # 输入+输出(口径与每日趋势"总量"一致,不含缓存)
        return int((r["it"] or 0) + (r["ot"] or 0))

    # Build per-user × model × day data for user detail modal
    _udm_raw = {}
    for r in by_day_user_model:
        u = r["username"]; model = r["model"] or "未知模型"; d = r["day"]
        _udm_raw.setdefault(u, {}).setdefault(model, {})[d] = {
            "tok": int(r["tok"] or 0), "cost": round(r["cost"] or 0, 4)}
    chart_udm = {
        u: {model: {
            "tok":  [_udm_raw[u][model].get(d["day"], {}).get("tok",  0) for d in days_rev],
            "cost": [_udm_raw[u][model].get(d["day"], {}).get("cost", 0) for d in days_rev],
        } for model in models}
        for u, models in _udm_raw.items()
    }

    # Build day×user stacked chart data (admin view only)
    _du_tok = {}; _du_cost = {}
    for r in by_day_user:
        d, u = r["day"], r["username"]
        _du_tok.setdefault(d, {})[u] = (r["tok"] or 0)
        _du_cost.setdefault(d, {})[u] = round(r["cost"] or 0, 4)
    _du_users = sorted(
        {r["username"] for r in by_day_user},
        key=lambda u: sum(_du_cost.get(d, {}).get(u, 0) for d in _du_cost),
        reverse=True
    ) if not scope_username else []
    chart_du = {
        "users": _du_users,
        "tok":  {u: [_du_tok.get(d["day"], {}).get(u, 0)  for d in days_rev] for u in _du_users},
        "cost": {u: [round(_du_cost.get(d["day"], {}).get(u, 0), 4) for d in days_rev] for u in _du_users},
    }

    _mdu_raw = {}
    for r in by_day_user_model:
        m = r["model"]; u = r["username"]; d = r["day"]
        _mdu_raw.setdefault(m, {}).setdefault(u, {})[d] = {
            "tok": int(r["tok"] or 0), "cost": round(r["cost"] or 0, 4)}
    chart_mdu = {
        m: {u: {
            "tok":  [_mdu_raw[m][u].get(d["day"], {}).get("tok",  0) for d in days_rev],
            "cost": [_mdu_raw[m][u].get(d["day"], {}).get("cost", 0) for d in days_rev],
        } for u in _mdu_raw[m]}
        for m in _mdu_raw
    }

    _hd_raw = {}
    for r in by_hour_all:
        d = r["day"]; h = int(r["hour"]); u = r["username"]
        _hd_raw.setdefault(d, {}).setdefault(u, {})[h] = {
            "tok": int(r["tok"] or 0), "cost": round(r["cost"] or 0, 6)}
    chart_hd = {}
    for _hd_day, _hd_udata in _hd_raw.items():
        _hd_ucosts = {u: sum(v["cost"] for v in hrs.values()) for u, hrs in _hd_udata.items()}
        _hd_users = sorted(_hd_udata, key=lambda u: _hd_ucosts[u], reverse=True)
        _hd_hours = sorted(set(h for hrs in _hd_udata.values() for h in hrs))
        chart_hd[_hd_day] = {
            "u": _hd_users,
            "h": _hd_hours,
            "tok":  {u: [_hd_udata[u].get(h, {}).get("tok",  0) for h in _hd_hours] for u in _hd_users},
            "cost": {u: [_hd_udata[u].get(h, {}).get("cost", 0) for h in _hd_hours] for u in _hd_users},
        }

    chart_ha = {"tok": [0]*24, "cost": [0.0]*24}
    for r in by_hour_agg:
        h = int(r["hour"])
        chart_ha["tok"][h]  = int(r["tok"] or 0)
        chart_ha["cost"][h] = round(r["cost"] or 0, 4)

    chart_data = json.dumps({
        "days": [r["day"] for r in days_rev],
        "dayTok": {"total": [int(r["tok"] or 0) for r in days_rev],
                   "inp": [int(r["it"] or 0) for r in days_rev],
                   "out": [int(r["ot"] or 0) for r in days_rev]},
        "dayCost": [round(r["cost"] or 0, 4) for r in days_rev],
        "users": [{"name": r["username"], "tok": io_tok(r), "cost": round(r["cost"] or 0, 4)}
                  for r in per_user],
        "models": [{"name": (r["model"] or "-"), "tok": io_tok(r), "cost": round(r["cost"] or 0, 4)}
                   for r in by_model],
        "dayUsers": chart_du,
        "userModelDays": chart_udm,
        "modelUserDays": chart_mdu,
        "hourlyDays": chart_hd,
        "hourAgg": chart_ha,
    }, ensure_ascii=False).replace("<", "\\u003c")
    user_h = max(180, len(per_user) * 30 + 30)
    has_charts = len(by_day) > 0 or len(per_user) > 0 or len(by_model) > 0
    if has_charts:
        parts.append(
            "<div class=chead><h2>图表</h2>"
            "<div class=toggle><button id=tgCost>费用</button>"
            "<button id=tgTok>Token</button></div></div>"
        )
        parts.append(
            "<div class=chartbox><div class=charttitle id=tDayUser>每日趋势</div>"
            "<div id=cDayUser style='height:320px'></div></div>"
            "<div class=charts2>"
            "<div class=chartbox><div class=charttitle id=tUser>按用户</div>"
            f"<div id=cUser style='height:{user_h}px'></div></div>"
            "<div class=chartbox><div class=charttitle id=tModel>按模型占比</div>"
            "<div id=cModel style='height:280px'></div></div>"
            "</div>"
        )

    # ── today by hour ──
    if by_hour:
        h_tok = {}; h_cost = {}; h_reqs = {}; h_users = {}
        for r in by_hour:
            h = r["hour"]; u = r["username"]
            h_tok.setdefault(h, {})[u] = (r["tok"] or 0)
            h_cost.setdefault(h, {})[u] = (r["cost"] or 0)
            h_reqs.setdefault(h, {})[u] = (r["reqs"] or 0)
            h_users[u] = h_users.get(u, 0) + (r["cost"] or 0)
        today_label = time.strftime("%m-%d")
        today_str_h = time.strftime("%Y-%m-%d")
        if scope_username:
            all_hours = sorted(h_tok.keys())
            parts.append(f"<h2>今日({today_label})每小时</h2>")
            parts.append("<table><thead><tr><th>时段</th><th class=num>请求</th>"
                         "<th class=num>Token</th><th class=num>估算费用</th></tr></thead><tbody>")
            for h in all_hours:
                ht = sum(h_tok[h].values())
                hc = sum(h_cost[h].values())
                hr = sum(h_reqs[h].values())
                parts.append(
                    f"<tr><td>{h}:00</td>"
                    f"<td class=num>{hr}</td>"
                    f"<td class=num>{num(ht)}</td>"
                    f"<td class=num>{money(hc)}</td></tr>"
                )
            parts.append("</tbody></table>")
        else:
            ucols_h = sorted(h_users, key=lambda u: h_users[u], reverse=True)
            all_hours = sorted(h_tok.keys())
            parts.append(
                f"<div class=metricbox id=todayHourTable><div class=chead>"
                f"<h2>今日({today_label})每小时(按用户)</h2>"
                "<div class='toggle sm'><button class='mb-cost active'>费用</button>"
                "<button class=mb-tok>Token</button></div></div>"
            )
            hhead = "".join(f"<th class=num>{h}:00</th>" for h in all_hours)
            parts.append(
                "<div style='overflow-x:auto'>"
                f"<table><thead><tr><th>用户</th>{hhead}"
                "<th class=num>合计</th></tr></thead><tbody>"
            )
            sum_tds = "".join(
                metric_cell(sum(h_tok[h].get(u, 0) for u in ucols_h),
                            sum(h_cost[h].get(u, 0) for u in ucols_h))
                for h in all_hours
            )
            grand_tok  = sum(sum(h_tok[h].get(u, 0)  for u in ucols_h) for h in all_hours)
            grand_cost = sum(sum(h_cost[h].get(u, 0) for u in ucols_h) for h in all_hours)
            parts.append(
                f"<tr><td><b>合计</b></td>{sum_tds}"
                f"{metric_cell(grand_tok, grand_cost, bold=True)}</tr>"
            )
            for u in ucols_h:
                safe_u = escape(str(u), quote=True)
                tds = "".join(metric_cell(h_tok[h].get(u, 0), h_cost[h].get(u, 0)) for h in all_hours)
                u_tok  = sum(h_tok[h].get(u, 0)  for h in all_hours)
                u_cost = sum(h_cost[h].get(u, 0) for h in all_hours)
                parts.append(
                    f"<tr><td><span class=ulink data-u='{safe_u}'>{safe_u}</span>{user_tag(u)}</td>"
                    f"{tds}{metric_cell(u_tok, u_cost, bold=True)}</tr>"
                )
            parts.append("</tbody></table></div></div>")

    # ── by day ──
    _day_title = {"today": "今日", "7": "按天(近7天)", "period": "按天(本周期)", "30": "按天(近30天)"}.get(date_range, "按天(近30天)")
    parts.append(f"<h2>{_day_title}</h2>")
    parts.append("<table><thead><tr><th>日期</th><th class=num>请求</th>"
                 "<th class=num>Token</th><th class=num>估算费用</th></tr></thead><tbody>")
    day_costs = [r["cost"] or 0 for r in by_day]
    max_day = max(day_costs, default=1)
    for d in by_day:
        parts.append(
            f"<tr><td><span class=dlink data-day='{d['day']}'>{d['day']}</span></td>"
            f"<td class=num>{num(d['reqs'])}</td>"
            f"<td class=num>{num(d['tok'])}</td>"
            f"{bars(d, max_day, 'cost')}"
            "</tr>"
        )
    parts.append("</tbody></table>")

    # ── per-user ──
    if len(per_user) > 1 or scope_username is None:
        parts.append("<h2>按用户</h2>")
        parts.append("<table><thead><tr><th>用户</th><th class=num>请求</th>"
                     "<th class=num>输入</th><th class=num>输出</th>"
                     "<th class=num>缓存读</th><th class=num>估算费用</th></tr></thead><tbody>")
        for r in per_user:
            name = escape(str(r["username"]), quote=True)
            parts.append(
                f"<tr><td><span class=ulink data-u='{name}'>{name}</span>"
                f"{user_tag(r['username'])}</td>"
                f"<td class=num>{num(r['reqs'])}</td>"
                f"<td class=num>{num(r['it'])}</td>"
                f"<td class=num>{num(r['ot'])}</td>"
                f"<td class=num>{num(r['rt'])}</td>"
                f"{bars(r, max_cost, 'cost')}"
                "</tr>"
            )
        parts.append("</tbody></table>")

    # ── by model ──
    parts.append("<h2>按模型</h2>")
    parts.append("<table><thead><tr><th>模型</th><th class=num>请求</th>"
                 "<th class=num>输入</th><th class=num>输出</th>"
                 "<th class=num>估算费用</th></tr></thead><tbody>")
    model_costs = [r["cost"] or 0 for r in by_model]
    max_m = max(model_costs, default=1)
    for r in by_model:
        m = escape(str(r["model"] or "-"), quote=True)
        parts.append(
            f"<tr><td><span class='modelid mlink' data-m='{m}' title='{m}'>{m}</span></td>"
            f"<td class=num>{num(r['reqs'])}</td>"
            f"<td class=num>{num(r['it'])}</td>"
            f"<td class=num>{num(r['ot'])}</td>"
            f"{bars(r, max_m, 'cost')}"
            "</tr>"
        )
    parts.append("</tbody></table>")

    def pivot_table(title, rows, col_key, col_order, col_label):
        """One-row-per-user matrix: columns are the buckets in col_order (only
        those with data), plus a 合计 column. Each cell carries both io-tokens
        and cost and follows the page-wide Token/费用 toggle. Keeps the cross-tab
        compact instead of one row per (user, bucket) pair."""
        tokc, costc, tot_tok, tot_cost = {}, {}, {}, {}
        for r in rows:
            u = r["username"]
            b = col_key(r)
            tokc.setdefault(u, {}); costc.setdefault(u, {})
            tokc[u][b] = tokc[u].get(b, 0) + (r["tok"] or 0)
            costc[u][b] = costc[u].get(b, 0) + (r["cost"] or 0)
            tot_tok[u] = tot_tok.get(u, 0) + (r["tok"] or 0)
            tot_cost[u] = tot_cost.get(u, 0) + (r["cost"] or 0)
        cols = [c for c in col_order if any(tokc[u].get(c, 0) for u in tokc)]
        users = sorted(tokc, key=lambda u: tot_cost.get(u, 0), reverse=True)
        parts.append(
            f"<div class=metricbox><div class=chead><h2>{title}</h2>"
            "<div class='toggle sm'><button class='mb-cost active'>费用</button>"
            "<button class=mb-tok>Token</button></div></div>"
        )
        head = "".join(f"<th class=num>{col_label(c)}</th>" for c in cols)
        parts.append(f"<table><thead><tr><th>用户</th>{head}"
                     "<th class=num>合计</th></tr></thead><tbody>")
        for u in users:
            safe_u = escape(str(u), quote=True)
            tds = "".join(metric_cell(tokc[u].get(c, 0), costc[u].get(c, 0)) for c in cols)
            parts.append(
                f"<tr><td><span class=ulink data-u='{safe_u}'>{safe_u}</span>{user_tag(u)}</td>{tds}"
                f"{metric_cell(tot_tok.get(u, 0), tot_cost.get(u, 0), bold=True)}</tr>"
            )
        parts.append("</tbody></table></div>")

    # ── by user × model (pivot) ──
    pivot_table("按用户 × 模型", by_user_model,
                lambda r: relay_db.model_family(r["model"]),
                relay_db.MODEL_FAMILIES, lambda c: c)

    # ── by user × client (pivot) ──
    _cli_lbl = {"cli": "CLI", "desktop": "桌面版", "other": "其他"}
    pivot_table("按用户 × 客户端", by_client,
                lambda r: r["client"] if r["client"] in ("cli", "desktop") else "other",
                ["cli", "desktop", "other"], lambda c: _cli_lbl.get(c, c))

    # ── user detail modal ──
    parts.append(
        "<div class=modal-ov id=userModal>"
        "<div class=modal-card>"
        "<div class=modal-hd>"
        "<span class=modal-title id=modalTitle></span>"
        "<div style='display:flex;align-items:center;gap:12px'>"
        "<div class='toggle sm'>"
        "<button class='mb-cost active' id=mCost>费用</button>"
        "<button class=mb-tok id=mTok>Token</button>"
        "</div>"
        "<button class=modal-close id=modalClose>&#x2715;</button>"
        "</div></div>"
        "<div id=cUserModal style='height:340px'></div>"
        "</div></div>"
        "<div class=modal-ov id=dayHourModal>"
        "<div class=modal-card>"
        "<div class=modal-hd>"
        "<span class=modal-title id=dayHourTitle></span>"
        "<div style='display:flex;align-items:center;gap:12px'>"
        "<div class='toggle sm'>"
        "<button class='mb-cost active' id=dhCost>费用</button>"
        "<button class=mb-tok id=dhTok>Token</button>"
        "</div>"
        "<button class=modal-close id=dayHourClose>&#x2715;</button>"
        "</div></div>"
        "<div id=dayHourTable></div>"
        "</div></div>"
        "<div class=modal-ov id=modelModal>"
        "<div class=modal-card>"
        "<div class=modal-hd>"
        "<span class=modal-title id=modelModalTitle></span>"
        "<div style='display:flex;align-items:center;gap:12px'>"
        "<div class='toggle sm'>"
        "<button class='mb-cost active' id=mmCost>费用</button>"
        "<button class=mb-tok id=mmTok>Token</button>"
        "</div>"
        "<button class=modal-close id=modelModalClose>&#x2715;</button>"
        "</div></div>"
        "<div id=cModelModal style='height:340px'></div>"
        "</div></div>"
    )

    parts.append(
        "<button id=toTop title='回到顶部' onclick='window.scrollTo({top:0,behavior:\"smooth\"})'>&#8679;</button>"
        "<script>(function(){var b=document.getElementById('toTop');"
        "window.addEventListener('scroll',function(){b.classList.toggle('vis',window.scrollY>300);});"
        "})();</script>"
    )
    # ── footer ──
    parts.append(
        "<div class=ft>OpenAI Relay v4 · API 等价估算费用（非订阅实际扣费）"
        "<br><span style=font-size:10px>"
        "要退出登录请清除本站 Cookie / 访问 /stats?logout=1</span></div>"
    )
    parts.append("</div>")
    # per-table Token/费用 toggles (independent of charts; runs without echarts)
    parts.append(
        "<script>(function(){"
        "document.querySelectorAll('.metricbox').forEach(function(b){"
        "var t=b.querySelector('.mb-tok'),c=b.querySelector('.mb-cost');"
        "if(!t||!c)return;"
        "function sortRows(metric){"
        "var body=b.querySelector('tbody');if(!body)return;"
        "var rows=Array.prototype.slice.call(body.querySelectorAll('tr'));"
        "var sortable=rows.filter(function(r){return r.querySelector('.ulink');});"
        "sortable.sort(function(a,z){"
        "var ac= a.querySelector('td:last-child'),zc=z.querySelector('td:last-child');"
        "return Number(zc.dataset[metric]||0)-Number(ac.dataset[metric]||0);"
        "});"
        "sortable.forEach(function(r){body.appendChild(r);});"
        "}"
        "b.classList.add('show-cost');"
        "sortRows('cost');"
        "t.onclick=function(){b.classList.remove('show-cost');sortRows('tok');"
        "t.classList.add('active');c.classList.remove('active');};"
        "c.onclick=function(){b.classList.add('show-cost');sortRows('cost');"
        "c.classList.add('active');t.classList.remove('active');};"
        "});})();</script>"
    )
    # charts: data + local echarts + init (after the chart divs exist in DOM)
    if has_charts:
        parts.append(f"<script>var STATS={chart_data};</script>")
        parts.append("<script src='/static/echarts.min.js'></script>")
        parts.append(f"<script>{CHART_JS}</script>")
    return "".join(parts)


def HOME_HTML():
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OpenAI Relay</title>"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;"
        "background:linear-gradient(165deg,#f9f7f3,#eceae3);min-height:100vh;"
        "display:flex;align-items:center;justify-content:center;color:#2d2d35;padding:24px}"
        ".box{text-align:center;max-width:480px}"
        ".logo{width:56px;height:56px;border-radius:16px;background:#0d9488;margin:0 auto 24px;"
        "display:flex;align-items:center;justify-content:center;color:#fff;font-size:26px;font-weight:700;"
        "box-shadow:0 8px 24px rgba(13,148,136,.25)}"
        "h1{font-size:30px;font-weight:750;letter-spacing:-.03em;margin-bottom:10px}"
        "p{color:#8a8a93;font-size:15px;line-height:1.6;margin-bottom:36px}"
        ".btns{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}"
        "a{text-decoration:none;font-size:15px;font-weight:600;padding:13px 28px;border-radius:11px;"
        "transition:transform .12s,box-shadow .12s}"
        "a:hover{transform:translateY(-2px)}"
        "a.primary{background:#0d9488;color:#fff;box-shadow:0 4px 14px rgba(13,148,136,.3)}"
        "a.ghost{background:#fff;color:#2d2d35;border:1.5px solid #e2e2e8}"
        ".ft{margin-top:48px;font-size:12px;color:#bcbcbf}"
        "</style>"
        "<div class=box>"
        "<div class=logo>C</div>"
        "<h1>OpenAI Relay</h1>"
        "<p>团队共享的 OpenAI 中转服务<br>查看你的用量,或管理用户</p>"
        "<div class=btns>"
        "<a class=primary href=/stats>📊 查看用量</a>"
        "<a class=ghost href=/admin>⚙️ 管理后台</a>"
        "</div>"
        "<div class=ft>v4 · 用自己的 key 看个人用量,管理 key 看全员</div>"
        "</div>"
    )


def ADMIN_LOGIN_HTML(error=False):
    err = ('<p style="color:#e53e3e;margin-top:16px;font-size:14px;text-align:center">'
           "管理员 key 不对,请重试</p>" if error else "")
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OpenAI Relay · 管理后台</title>"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;"
        "background:linear-gradient(165deg,#f9f7f3,#f0ede6);min-height:100vh;"
        "display:flex;align-items:center;justify-content:center;color:#2d2d35;padding:24px}"
        ".card{background:#fff;border-radius:16px;padding:44px 40px 36px;max-width:420px;width:100%;"
        "box-shadow:0 1px 3px rgba(0,0,0,.06),0 8px 32px rgba(0,0,0,.04)}"
        "h1{font-size:21px;font-weight:680;margin-bottom:4px}"
        ".sub{color:#8a8a93;font-size:14px;margin-bottom:26px}"
        "input{width:100%;padding:12px 16px;font-size:14px;border:1.5px solid #e2e2e8;border-radius:10px;"
        "outline:none;transition:border-color .15s;font-family:inherit}"
        "input:focus{border-color:#0d9488;box-shadow:0 0 0 3px rgba(13,148,136,.1)}"
        "button{width:100%;margin-top:16px;padding:12px;font-size:14px;font-weight:600;color:#fff;"
        "background:#0d9488;border:none;border-radius:10px;cursor:pointer;font-family:inherit}"
        "button:hover{background:#0f766e}"
        "</style>"
        "<div class=card><h1>⚙️ 管理后台</h1>"
        "<p class=sub>需要管理员 key</p>"
        f"{err}"
        "<form method=post action=/admin>"
        "<input name=key type=password placeholder='管理员 key' autofocus>"
        "<button type=submit>登录</button></form></div>"
    )


def render_admin_html(msg=""):
    users = relay_db.list_users()
    total = len(users)
    enabled = sum(1 for u in users if u["enabled"])

    def fmt_exp(u):
        e = u.get("expires_at")
        if not e:
            return "永久"
        return time.strftime("%Y-%m-%d", time.localtime(e / 1000))

    def lim(v):
        return f"{int(v):,}" if v else "—"

    banner = (f"<div class=banner>{msg}</div>" if msg else "")

    parts = [
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
        "<title>OpenAI Relay · 管理后台</title>",
        "<style>", CSS, ADMIN_CSS, "</style>",
        "<div class=wrap>",
        "<div class=topbar><div><h1>⚙️ 管理后台</h1>"
        f"<span class=meta>共 {total} 个用户 · {enabled} 个启用中</span></div>"
        "<div><a class=navlink href=/stats>📊 用量</a>"
        "<a class=navlink href='/admin?logout=1'>退出</a></div></div>",
        banner,
        # ── create user ──
        "<h2>新建用户</h2>",
        "<p class=explain>每日 Token 上限 = 一天能用的总 token 数(输入+输出+缓存,0 点重置);"
        "rpm = 每分钟最多请求次数。<b>都留空 = 不限</b>。</p>",
        "<form method=post action=/admin class=createbox>"
        "<input type=hidden name=action value=create>"
        "<input name=username placeholder='用户名(必填)' required>"
        "<input name=daily_token_limit type=number placeholder='每日 token 上限 · 个/天(空=不限)'>"
        "<input name=daily_request_limit type=number placeholder='每日请求上限 · 次/天(空=不限)'>"
        "<input name=rpm type=number placeholder='速率 · 次/分钟(空=不限)'>"
        "<input name=days type=number placeholder='有效期 · N 天(空=永久)'>"
        "<button type=submit>+ 创建</button>"
        "</form>",
        # ── users table ──
        "<h2>用户列表</h2>",
        "<table><thead><tr><th>用户</th><th>状态</th><th>API Key</th>"
        "<th class=num>每日Token上限<br><span class=unit>个/天</span></th>"
        "<th class=num>每日请求上限<br><span class=unit>次/天</span></th>"
        "<th class=num>速率<br><span class=unit>次/分</span></th>"
        "<th>有效期</th><th>操作</th></tr></thead><tbody>",
    ]
    for u in users:
        raw_name = str(u["username"])
        name = escape(raw_name, quote=True)
        st = ("<span class='badge on'>启用</span>" if u["enabled"]
              else "<span class='badge off'>禁用</span>")
        toggle_action = "disable" if u["enabled"] else "enable"
        toggle_label = "禁用" if u["enabled"] else "启用"
        parts.append(
            f"<tr><td><b>{name}</b></td><td>{st}</td>"
            f"<td><code class=key>{u['api_key']}</code></td>"
            f"<td class=num>{lim(u['daily_token_limit'])}</td>"
            f"<td class=num>{lim(u['daily_request_limit'])}</td>"
            f"<td class=num>{u['rpm_limit'] or '—'}</td>"
            f"<td>{fmt_exp(u)}</td>"
            "<td class=ops>"
            f"<form method=post action=/admin><input type=hidden name=action value={toggle_action}>"
            f"<input type=hidden name=username value='{name}'>"
            f"<button class='btn'>{toggle_label}</button></form>"
            f"<form method=post action=/admin onsubmit=\"return confirm('确定删除 {name}?此操作不可撤销')\">"
            f"<input type=hidden name=action value=delete><input type=hidden name=username value='{name}'>"
            "<button class='btn danger'>删除</button></form>"
            "</td></tr>"
        )
        # inline setlimit row
        parts.append(
            "<tr class=editrow><td colspan=8>"
            f"<form method=post action=/admin class=limform>"
            f"<span class=lbl>改限额 · {name}</span>"
            f"<input type=hidden name=action value=setlimit><input type=hidden name=username value='{name}'>"
            f"<input name=daily_token_limit type=number placeholder='每日token 个/天' "
            f"value='{u['daily_token_limit'] or ''}'>"
            f"<input name=daily_request_limit type=number placeholder='每日请求 次/天' "
            f"value='{u['daily_request_limit'] or ''}'>"
            f"<input name=rpm type=number placeholder='速率 次/分' value='{u['rpm_limit'] or ''}'>"
            "<button class='btn'>保存</button>"
            "<span class=tip>留空 = 不限,填数字 = 设上限(保存即按当前框内值生效)</span>"
            "</form></td></tr>"
        )
    parts.append("</tbody></table>")
    parts.append(
        "<div class=ft>建好用户后,把对应 API Key 发给本人,配到 cc switch / 桌面版即可</div>")
    parts.append("</div>")
    return "".join(parts)


ADMIN_CSS = (
    ".navlink{font-size:13px;color:#0d9488;text-decoration:none;margin-left:16px;font-weight:600}"
    ".navlink:hover{text-decoration:underline}"
    ".explain{font-size:12.5px;color:#8a8a93;margin:-6px 0 14px 2px;line-height:1.6}"
    ".explain b{color:#0d9488}"
    "th .unit{font-size:9.5px;color:#bcbcbf;font-weight:500;letter-spacing:0;text-transform:none}"
    ".banner{background:#ecfdf5;color:#047857;padding:11px 16px;border-radius:10px;"
    "font-size:14px;margin-bottom:20px;border:1px solid #a7f3d0}"
    ".createbox{display:flex;gap:10px;flex-wrap:wrap;background:#fff;padding:18px;border-radius:12px;"
    "box-shadow:0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.03);margin-bottom:8px}"
    ".createbox input{flex:1;min-width:140px;padding:10px 12px;font-size:13px;border:1.5px solid #e6e6ea;"
    "border-radius:8px;outline:none;font-family:inherit}"
    ".createbox input:focus{border-color:#0d9488}"
    ".createbox button,.btn{font-family:inherit;cursor:pointer;border:none;border-radius:8px;font-weight:600}"
    ".createbox button{background:#0d9488;color:#fff;padding:10px 22px;font-size:13px}"
    ".createbox button:hover{background:#0f766e}"
    "code.key{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:#6b7280;"
    "background:#f5f5f4;padding:3px 7px;border-radius:5px;user-select:all}"
    ".badge.on{background:#ecfdf5;color:#047857}"
    ".badge.off{background:#fef2f2;color:#b91c1c}"
    ".ops{display:flex;gap:6px;white-space:nowrap}"
    ".ops form{display:inline}"
    ".btn{background:#f5f5f4;color:#44403c;padding:5px 12px;font-size:12px}"
    ".btn:hover{background:#e7e5e4}"
    ".btn.danger{background:#fef2f2;color:#b91c1c}"
    ".btn.danger:hover{background:#fee2e2}"
    ".editrow td{padding:0 14px 12px;border-bottom:1px solid #f5f4f0}"
    ".limform{display:flex;align-items:center;gap:8px;flex-wrap:wrap}"
    ".limform .lbl{font-size:12px;color:#8a8a93;font-weight:600;min-width:120px}"
    ".limform input{width:120px;padding:6px 10px;font-size:12px;border:1.5px solid #e6e6ea;"
    "border-radius:7px;outline:none;font-family:inherit}"
    ".limform input:focus{border-color:#0d9488}"
    ".limform .tip{font-size:11px;color:#bcbcbf}"
)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    # Default socketserver backlog is 5; too small for bursts of simultaneous
    # client connections (causes connection resets / tail latency under load).
    request_queue_size = 128
    daemon_threads = True


def main():
    relay_db.init_db()
    # Seed the optional legacy/transition key as a no-limit user so an existing
    # shared key keeps working during migration. Skipped when RELAY_LEGACY_KEY
    # is unset (CUSTOM_API_KEY == "").
    if CUSTOM_API_KEY and CUSTOM_API_KEY not in {u["api_key"] for u in relay_db.list_users()}:
        relay_db.add_user("legacy", api_key=CUSTOM_API_KEY, note="transition key")
    relay_db.reload_users()
    relay_db.backfill_today()
    relay_db.start_writer()

    server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), OpenAIRelayHandler)
    print(f"OpenAI Relay v{__version__} (multi-user) on 0.0.0.0:{LISTEN_PORT}", flush=True)
    print(f"  Upstream: https://{UPSTREAM_HOST}{UPSTREAM_PREFIX}", flush=True)
    if _ADMIN_KEY_FROM_ENV:
        print(f"  Users loaded: {len(relay_db.USERS)}; admin key from RELAY_ADMIN_KEY", flush=True)
    else:
        print(f"  Users loaded: {len(relay_db.USERS)}; RELAY_ADMIN_KEY unset -> generated admin key: {ADMIN_KEY}", flush=True)
    print(f"  Stats: GET /stats (user key=self, admin key=all)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
