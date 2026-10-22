"""Phone shell for Herdr HUD (https://github.com/finna/herdr-hud).

The desktop HUD hosts web/ inside a native WebView and answers its ops
(ready, refresh, output, prompt, preferences, ...) from Swift or C#. This module
answers the same ops over HTTP so that the unchanged UI runs in a phone browser:

  POST api/op      {op, requestID, ...}      -> 204, reply arrives on the stream
  GET  api/events  text/event-stream          -> {type, data} messages for receive()

Access is by a secret link. Opening /?token=... sets a cookie, and every other
request needs that cookie. POSTs must also carry the X-HUD-Client id of a live
event stream, which a cross-site page can't send without a CORS preflight.

Python standard library only.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import queue
import secrets
import socket
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB_ROOT = Path(__file__).with_name("web")
COOKIE = "herdr_hud"
ROSTER_SECONDS = float(os.environ.get("HUD_ROSTER_SECONDS", "2"))
OUTPUT_LINES = 180  # same window the desktop HUD reads
MAX_PROMPT_BYTES = 60000

mimetypes.add_type("application/manifest+json", ".webmanifest")


def agent_key(machine_id: str, a: dict) -> str:
    """Identity the HUD uses for an agent; changes if the pane is reused."""
    return json.dumps(
        [machine_id, a.get("pane_id", ""), a.get("terminal_id", ""), a.get("agent_session")],
        sort_keys=True,
        separators=(",", ":"),
    )


class HUDError(Exception):
    pass


class HudBackend:
    """Turns HUD ops into herdr socket calls. One local herdr machine."""

    def __init__(self, herdr, machine_label: str | None = None, prefs_path: Path | None = None):
        self.herdr = herdr
        self.machine = {"id": "local", "label": machine_label or socket.gethostname(), "session": os.environ.get("HERDR_SESSION", "default")}
        self.bindings: dict[str, dict] = {}
        self.cached: list[dict] = []
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()  # sends are serialized, like the desktop HUD
        self.prefs_path = prefs_path
        self.prefs = self._load_prefs()

    # -- preferences -------------------------------------------------------

    def _load_prefs(self) -> dict:
        try:
            return json.loads(self.prefs_path.read_text()) if self.prefs_path else {}
        except (OSError, ValueError):
            return {}

    def save_prefs(self, update: dict):
        allowed = {k: v for k, v in update.items() if k in ("selectedAgent", "mode", "rosterWidth")}
        with self.lock:
            self.prefs.update(allowed)
            if self.prefs_path:
                try:
                    tmp = self.prefs_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self.prefs))
                    os.replace(tmp, self.prefs_path)  # never leave a half-written file
                except OSError:
                    pass

    # -- roster ------------------------------------------------------------

    def _labels(self, method: str, key: str, id_field: str) -> dict:
        try:
            rows = self.herdr.call(method).get(key, [])
        except Exception:
            return {}
        return {r.get(id_field): r.get("label") for r in rows if r.get(id_field)}

    def roster(self) -> dict:
        m = self.machine
        try:
            agents = self.herdr.agents()
        except Exception as e:
            # Keep the last cards on screen, marked offline, like the desktop HUD.
            with self.lock:
                stale = [dict(a, online=False) for a in self.cached]
            return {"agents": stale, "machines": [dict(m, online=False, error=str(e))], "discoveryError": ""}
        spaces = self._labels("workspace.list", "workspaces", "workspace_id")
        tabs = self._labels("tab.list", "tabs", "tab_id")
        rows, bindings = [], {}
        for a in agents:
            key = agent_key(m["id"], a)
            row = dict(a)
            row.update(
                id=key,
                machine_id=m["id"],
                machine_label=m["label"],
                online=True,
                workspace_label=spaces.get(a.get("workspace_id")) or a.get("workspace_id"),
                tab_label=tabs.get(a.get("tab_id")) or a.get("tab_id"),
            )
            rows.append(row)
            bindings[key] = a
        with self.lock:
            self.bindings, self.cached = bindings, rows
        return {"agents": rows, "machines": [dict(m, online=True, count=len(rows))], "discoveryError": ""}

    # -- read / send ---------------------------------------------------------

    def resolve(self, key: str) -> dict:
        """Re-read the agent and make sure it's still the one the phone selected."""
        with self.lock:
            expected = self.bindings.get(key)
        if not expected:
            raise HUDError("Agent is offline or changed. Refresh and select it again.")
        try:
            current = self.herdr.call("agent.get", {"target": expected["pane_id"]}).get("agent", {})
        except Exception:
            raise HUDError("Agent is offline or changed. Refresh and select it again.")
        if agent_key(self.machine["id"], current) != key or any(
            current.get(f) != expected.get(f) for f in ("workspace_id", "agent")
        ):
            raise HUDError("The selected agent was replaced. Select its new session.")
        return current

    def output(self, key: str) -> dict:
        a = self.resolve(key)
        res = self.herdr.call(
            "agent.read",
            {"target": a["pane_id"], "source": "recent_unwrapped", "lines": OUTPUT_LINES},
        )
        text = res.get("read", res).get("text", "")
        return {"id": key, "provider": a.get("agent", ""), "text": text}

    def prompt(self, key: str, message: str) -> dict:
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message.encode()) > MAX_PROMPT_BYTES
            or "\0" in message
        ):
            raise HUDError("Enter a prompt under 60 KB.")
        with self.send_lock:
            a = self.resolve(key)
            if a.get("agent_status") not in ("idle", "done"):
                raise HUDError("This agent is busy or needs input. Answer native questions in Herdr.")
            try:
                res = self.herdr.call("agent.prompt", {"target": a["pane_id"], "text": message})
            except Exception:
                raise HUDError("Delivery uncertain or refused. Inspect Herdr before sending again.")
            ack = res.get("agent", {})
            if res.get("type") != "agent_prompted" or ack.get("terminal_id", a.get("terminal_id")) != a.get("terminal_id"):
                raise HUDError("Delivery uncertain. Inspect Herdr before sending again.")
        return {"ok": True, "id": key}


class Hub:
    """Event streams for connected phones, plus the roster poller."""

    def __init__(self, backend: HudBackend):
        self.backend = backend
        self.clients: dict[str, queue.Queue] = {}
        self.lock = threading.Lock()
        self.last_roster = ""
        threading.Thread(target=self._poll, daemon=True).start()

    def open(self) -> tuple[str, queue.Queue]:
        cid, q = secrets.token_urlsafe(12), queue.Queue(maxsize=200)
        with self.lock:
            self.clients[cid] = q
        return cid, q

    def close(self, cid: str):
        with self.lock:
            self.clients.pop(cid, None)

    def has(self, cid: str) -> bool:
        with self.lock:
            return cid in self.clients

    def push(self, cid: str | None, type_: str, data: dict):
        msg = json.dumps({"type": type_, "data": data})
        with self.lock:
            targets = [self.clients[cid]] if cid in self.clients else ([] if cid else list(self.clients.values()))
        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass  # a stuck phone will reconnect and re-sync

    def _poll(self):
        while True:
            time.sleep(ROSTER_SECONDS)
            with self.lock:
                if not self.clients:
                    continue
            roster = self.backend.roster()
            signature = json.dumps(roster, sort_keys=True)
            if signature != self.last_roster:
                self.last_roster = signature
                self.push(None, "roster", roster)

    def handle(self, cid: str, op: dict):
        kind, rid = op.get("op"), op.get("requestID")
        b = self.backend
        if kind == "ready":
            self.push(cid, "preferences", b.prefs)
            self.push(cid, "visibility", {"open": True})
            self.push(cid, "roster", b.roster())
        elif kind == "refresh":
            self.push(cid, "roster", b.roster())
        elif kind == "output":
            try:
                self.push(cid, "output", b.output(str(op.get("id", ""))))
            except Exception as e:
                self.push(cid, "output", {"id": op.get("id"), "error": str(e)})
        elif kind == "prompt":
            try:
                self.push(cid, "prompt", dict(b.prompt(str(op.get("id", "")), op.get("message")), requestID=rid))
            except Exception as e:
                self.push(cid, "prompt", {"requestID": rid, "error": str(e)})
        elif kind == "preferences":
            b.save_prefs(op)
        # badge, alert, alertPreview, close, hide: desktop window chrome; nothing to do on a phone.


def make_handler(hub: Hub, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "herdr-hud-mobile"

        def log_message(self, fmt, *args):  # quiet; the phone polls a lot
            pass

        def _authed(self) -> bool:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            got = jar[COOKIE].value if COOKIE in jar else ""
            return bool(got) and hmac.compare_digest(got, token)

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            given = parse_qs(url.query).get("token", [""])[0]
            if given:
                if hmac.compare_digest(given, token):
                    # Swap the token in the URL for a cookie so it doesn't sit in history.
                    c = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                    return self._send(303, extra=[("Set-Cookie", c), ("Location", "./")])
                return self._send(401, b"That link is out of date. Use the latest link from herdr-phone.")
            if not self._authed():
                # The manifest and icon are fetched without cookies by some browsers.
                if url.path in ("/manifest.webmanifest", "/icon.svg", "/icon-180.png"):
                    return self._static(url.path)
                return self._send(
                    401,
                    b"Open the link printed by herdr-phone (or send /hud to your Telegram bot).",
                )
            if url.path == "/api/events":
                return self._events()
            return self._static(url.path)

        def do_HEAD(self):
            self.do_GET()

        def _static(self, path: str):
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB_ROOT / name).resolve()
            if WEB_ROOT.resolve() not in target.parents or not target.is_file():
                return self._send(404, b"not found")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _events(self):
            cid, q = hub.open()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(f"event: hello\ndata: {json.dumps({'client': cid})}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f"data: {msg}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.close(cid)

        def do_POST(self):
            if urlparse(self.path).path != "/api/op":
                return self._send(404)
            if not self._authed():
                return self._send(401)
            cid = self.headers.get("X-HUD-Client", "")
            if not hub.has(cid):
                return self._send(409, b"stream not open")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 600_000:
                return self._send(413)
            try:
                op = json.loads(self.rfile.read(length) or b"{}")
                assert isinstance(op, dict)
            except (ValueError, AssertionError):
                return self._send(400)
            self._send(204)
            threading.Thread(target=hub.handle, args=(cid, op), daemon=True).start()

    return Handler


def load_token(path: Path) -> str:
    env = os.environ.get("HUD_TOKEN", "").strip()
    if env:
        return env
    try:
        saved = path.read_text().strip()
        if len(saved) >= 20:
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def guess_phone_hosts() -> list[str]:
    """Addresses a phone could reach: Tailscale (100.x) first, then LAN."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 53))  # Tailscale's resolver; no packet is sent
            ip = s.getsockname()[0]
            if ip.startswith("100."):
                found.append(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found


def start(herdr, host: str, port: int, state_dir: Path) -> tuple[ThreadingHTTPServer, list[str]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    token = load_token(state_dir / "hud-token")
    backend = HudBackend(herdr, os.environ.get("HUD_MACHINE_LABEL"), state_dir / "hud-prefs.json")
    hub = Hub(backend)
    server = ThreadingHTTPServer((host, port), make_handler(hub, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    public = os.environ.get("HUD_PUBLIC_URL", "").rstrip("/")
    bases = [public] if public else [f"http://{h}:{port}" for h in guess_phone_hosts() or ["localhost"]]
    return server, [f"{b}/?token={token}" for b in bases]
