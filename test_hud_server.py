"""Tests for the phone HUD server against a fake herdr socket.

Run: python3 -m unittest -v test_hud_server
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

os.environ["HUD_ROSTER_SECONDS"] = "0.2"
os.environ["HUD_TOKEN"] = "test-token-0123456789abcdef"
os.environ["HUD_PUBLIC_URL"] = ""

import herdr_phone as hp  # noqa: E402
import hud_server  # noqa: E402
from test_herdr_phone import FakeHerdr  # noqa: E402

TOKEN = os.environ["HUD_TOKEN"]


class Stream:
    """Reads the server-sent event stream on a background thread."""

    def __init__(self, port, cookie):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.conn.request("GET", "/api/events", headers={"Cookie": cookie})
        self.resp = self.conn.getresponse()
        self.client = None
        self.events = []
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        self.wait(lambda: self.client)

    def _read(self):
        kind = None
        try:
            for raw in self.resp:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    with self.cv:
                        if kind == "hello":
                            self.client = data["client"]
                        else:
                            self.events.append(data)
                        self.cv.notify_all()
                    kind = None
        except Exception:
            pass

    def wait(self, pred, timeout=5):
        with self.cv:
            ok = self.cv.wait_for(pred, timeout)
        assert ok, f"timed out; events={self.events}"
        return ok

    def first(self, type_, pred=lambda d: True):
        self.wait(lambda: any(e["type"] == type_ and pred(e["data"]) for e in self.events))
        return next(e["data"] for e in self.events if e["type"] == type_ and pred(e["data"]))

    def close(self):
        self.conn.close()


class HudServerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHerdr()
        self.state = Path(tempfile.mkdtemp())
        self.server, self.links = hud_server.start(hp.Herdr(self.fake.path), "127.0.0.1", 0, self.state)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fake.close()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def login(self):
        r, _ = self.req("GET", f"/?token={TOKEN}")
        self.assertEqual(r.status, 303)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        return cookie.split(";", 1)[0]

    def op(self, cookie, stream, data):
        return self.req(
            "POST",
            "/api/op",
            json.dumps(data),
            {"Cookie": cookie, "X-HUD-Client": stream.client, "Content-Type": "application/json"},
        )[0]

    def test_links_carry_token(self):
        self.assertTrue(self.links and all(TOKEN in link for link in self.links))

    def test_requires_token(self):
        self.assertEqual(self.req("GET", "/")[0].status, 401)
        self.assertEqual(self.req("GET", "/app.js")[0].status, 401)
        self.assertEqual(self.req("GET", "/?token=wrong")[0].status, 401)
        self.assertEqual(self.req("GET", "/", headers={"Cookie": "herdr_hud=wrong"})[0].status, 401)
        self.assertEqual(self.req("POST", "/api/op", b"{}")[0].status, 401)

    def test_home_screen_app_signs_itself_in(self):
        r, body = self.req("GET", "/")
        self.assertEqual(r.status, 401)
        self.assertIn(b"<form", body)
        anon = json.loads(self.req("GET", "/manifest.webmanifest")[1])
        self.assertNotIn(TOKEN, anon["start_url"])
        self.assertEqual(json.loads(self.req("GET", "/manifest.webmanifest?t=wrong")[1])["start_url"], "./")
        cookie = self.login()
        page = self.req("GET", "/", headers={"Cookie": cookie})[1]
        self.assertIn(f"manifest.webmanifest?t={TOKEN}".encode(), page)
        signed = json.loads(self.req("GET", f"/manifest.webmanifest?t={TOKEN}")[1])
        self.assertEqual(signed["start_url"], f"./?token={TOKEN}")
        pasted = f"https://example.ts.net:8443/?token={TOKEN}"
        self.assertEqual(self.req("GET", "/?" + urlencode({"token": pasted}))[0].status, 303)

    def test_serves_ui_and_blocks_traversal(self):
        cookie = self.login()
        r, body = self.req("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(r.status, 200)
        self.assertIn(b"Herdr HUD", body)
        self.assertEqual(self.req("GET", "/../hud_server.py", headers={"Cookie": cookie})[0].status, 404)
        self.assertEqual(self.req("GET", "/%2e%2e/hud_server.py", headers={"Cookie": cookie})[0].status, 404)

    def test_post_needs_live_stream(self):
        cookie = self.login()
        r, _ = self.req("POST", "/api/op", b'{"op":"refresh"}', {"Cookie": cookie, "X-HUD-Client": "nope"})
        self.assertEqual(r.status, 409)

    def test_ready_read_and_prompt(self):
        cookie = self.login()
        s = Stream(self.port, cookie)
        try:
            self.assertEqual(self.op(cookie, s, {"op": "ready", "requestID": "1"}).status, 204)
            roster = s.first("roster")
            self.assertTrue(s.first("visibility")["open"])
            agents = roster["agents"]
            self.assertEqual([a["pane_id"] for a in agents], ["w1:p1", "w1:p2"])
            self.assertEqual(agents[0]["workspace_label"], "api")
            self.assertEqual(agents[0]["tab_label"], "main")
            claude, reviewer = agents

            self.op(cookie, s, {"op": "output", "requestID": "2", "id": claude["id"]})
            out = s.first("output", lambda d: d.get("id") == claude["id"])
            self.assertEqual(out["text"], "output of w1:p1")
            read = [c for c in self.fake.calls if c["method"] == "agent.read"][-1]
            self.assertEqual(read["params"]["source"], "recent_unwrapped")

            # claude is working: the HUD must refuse, like the desktop app.
            self.op(cookie, s, {"op": "prompt", "requestID": "3", "id": claude["id"], "message": "hi"})
            self.assertIn("busy", s.first("prompt", lambda d: d.get("requestID") == "3")["error"])

            self.op(cookie, s, {"op": "prompt", "requestID": "4", "id": reviewer["id"], "message": "ship it"})
            self.assertTrue(s.first("prompt", lambda d: d.get("requestID") == "4")["ok"])
            self.assertEqual(self.fake.prompts[-1], {"target": "w1:p2", "text": "ship it"})

            # The pane gets a new terminal: the old identity must not receive prompts.
            self.fake.agents[1]["terminal_id"] = "t2-new"
            self.op(cookie, s, {"op": "prompt", "requestID": "5", "id": reviewer["id"], "message": "again"})
            self.assertIn("replaced", s.first("prompt", lambda d: d.get("requestID") == "5")["error"])
            self.assertEqual(len(self.fake.prompts), 1)
        finally:
            s.close()

    def test_roster_pushes_status_changes(self):
        cookie = self.login()
        s = Stream(self.port, cookie)
        try:
            self.op(cookie, s, {"op": "ready"})
            s.first("roster")
            self.fake.agents[0]["agent_status"] = "blocked"
            s.first("roster", lambda d: d["agents"][0]["agent_status"] == "blocked")
        finally:
            s.close()

    def test_preferences_persist(self):
        cookie = self.login()
        s = Stream(self.port, cookie)
        try:
            self.op(cookie, s, {"op": "preferences", "mode": "terminal", "evil": 1})
            s.wait(lambda: (self.state / "hud-prefs.json").exists())
            self.assertEqual(json.loads((self.state / "hud-prefs.json").read_text()), {"mode": "terminal"})
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
