"""Tests for herdr_phone using a fake herdr socket and a stub Telegram client.

Run: python3 -m unittest -v test_herdr_phone
"""

import json
import os
import socket
import tempfile
import threading
import unittest

import herdr_phone as hp


class FakeHerdr:
    """Speaks herdr's newline-delimited JSON protocol on a unix socket."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "herdr.sock")
        self.agents = [
            {"pane_id": "w1:p1", "terminal_id": "t1", "workspace_id": "w1", "tab_id": "w1:t1",
             "focused": True, "agent": "claude", "agent_status": "working", "cwd": "/src/api"},
            {"pane_id": "w1:p2", "terminal_id": "t2", "workspace_id": "w1", "tab_id": "w1:t1",
             "focused": False, "agent": "codex", "name": "reviewer", "agent_status": "idle"},
        ]
        self.calls = []
        self.prompts = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile().readline())
                self.calls.append(req)
                conn.sendall((json.dumps(self._handle(req)) + "\n").encode())

    def _handle(self, req):
        m, p, rid = req["method"], req["params"], req["id"]
        if m == "agent.list":
            return {"id": rid, "result": {"type": "agent_list", "agents": self.agents}}
        if m == "agent.get":
            a = next((a for a in self.agents if p["target"] in (a["pane_id"], a.get("name"))), None)
            if not a:
                return {"id": rid, "error": {"code": "not_found", "message": "agent not found"}}
            return {"id": rid, "result": {"type": "agent_info", "agent": a}}
        if m == "workspace.list":
            return {"id": rid, "result": {"type": "workspace_list", "workspaces": [{"workspace_id": "w1", "label": "api"}]}}
        if m == "tab.list":
            return {"id": rid, "result": {"type": "tab_list", "tabs": [{"tab_id": "w1:t1", "label": "main"}]}}
        if m == "agent.read":
            return {"id": rid, "result": {"type": "pane_read", "read": {"text": f"output of {p['target']}"}}}
        if m == "agent.prompt":
            st = next(a for a in self.agents if p["target"] in (a["pane_id"], a.get("name")))
            if st["agent_status"] == "blocked":
                return {"id": rid, "error": {"code": "agent_blocked", "message": "blocked"}}
            self.prompts.append(p)
            return {"id": rid, "result": {"type": "agent_prompted", "agent": st}}
        if m in ("agent.send_keys", "ping"):
            return {"id": rid, "result": {"type": "ok"}}
        return {"id": rid, "error": {"code": "unknown_method", "message": m}}

    def close(self):
        self.srv.close()


class StubTelegram:
    def __init__(self):
        self.sent = []
        self.n = 100

    def send(self, chat_id, text, buttons=None, reply_to=None):
        self.n += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "id": self.n})
        return {"message_id": self.n}

    def call(self, method, **params):
        return True


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHerdr()
        self.tg = StubTelegram()
        self.bridge = hp.Bridge(hp.Herdr(self.fake.path), self.tg, "42")

    def tearDown(self):
        self.fake.close()

    def methods(self, name):
        return [c for c in self.fake.calls if c["method"] == name]

    def msg(self, text, reply_to=None, chat=42):
        m = {"message_id": 1, "chat": {"id": chat}, "text": text}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        return {"update_id": 1, "message": m}

    def test_ignores_strangers(self):
        self.bridge.dispatch(self.msg("rm -rf /", chat=999))
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.methods("agent.prompt"), [])

    def test_agents_list(self):
        self.bridge.dispatch(self.msg("/agents"))
        text = self.tg.sent[-1]["text"]
        self.assertIn("claude · w1:p1", text)
        self.assertIn("reviewer · w1:p2", text)

    def test_use_then_type(self):
        self.bridge.dispatch(self.msg("/use 2"))
        self.bridge.dispatch(self.msg("please review the diff"))
        call = self.methods("agent.prompt")[-1]
        self.assertEqual(call["params"], {"target": "reviewer", "text": "please review the diff"})

    def test_reply_routes_to_notified_agent(self):
        self.bridge.current = "reviewer"
        self.fake.agents[0]["agent_status"] = "done"
        self.bridge.notify(self.fake.agents[0], "done")
        note = self.tg.sent[-1]
        self.assertIn("finished", note["text"])
        self.assertIn("output of w1:p1", note["text"])
        self.bridge.dispatch(self.msg("now add tests", reply_to=note["id"]))
        self.assertEqual(self.methods("agent.prompt")[-1]["params"]["target"], "w1:p1")

    def test_blocked_agent_gets_buttons_not_text(self):
        self.fake.agents[0]["agent_status"] = "blocked"
        self.bridge.dispatch(self.msg("/to 1 hello"))
        self.assertIn("waiting on a question", self.tg.sent[-1]["text"])
        self.assertTrue(self.tg.sent[-1]["buttons"])

    def test_button_sends_key(self):
        cb = {"update_id": 2, "callback_query": {
            "id": "cb", "data": "k:enter|w1:p1", "message": {"chat": {"id": 42}}}}
        self.bridge.dispatch(cb)
        self.assertEqual(self.methods("agent.send_keys")[-1]["params"], {"target": "w1:p1", "keys": ["enter"]})

    def test_watcher_notifies_on_transition(self):
        # Run one watcher pass manually by stubbing sleep to stop the loop.
        passes = {"n": 0}

        def fake_sleep(_):
            passes["n"] += 1
            if passes["n"] == 1:
                self.fake.agents[0]["agent_status"] = "blocked"  # working -> blocked
                self.fake.agents[1]["agent_status"] = "done"     # idle -> done: ignored
            else:
                raise StopIteration

        orig = hp.time.sleep
        hp.time.sleep = fake_sleep
        try:
            with self.assertRaises(StopIteration):
                self.bridge.watch_forever()
        finally:
            hp.time.sleep = orig
        notes = [s["text"] for s in self.tg.sent]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("needs you", notes[0])
        self.assertIn("w1:p1", notes[0])


class SelfCheckTest(unittest.TestCase):
    def test_check_runs_clean_and_sends_nothing(self):
        import io
        from contextlib import redirect_stdout

        fake = FakeHerdr()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = hp.self_check(hp.Herdr(fake.path))
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertNotIn("❌", buf.getvalue())
            self.assertEqual([c for c in fake.calls if c["method"] in ("agent.prompt", "agent.send_keys")], [])
        finally:
            fake.close()

    def test_check_reports_unreachable(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = hp.self_check(hp.Herdr("/nonexistent/herdr.sock"))
        self.assertEqual(rc, 1)
        self.assertIn("Is herdr running", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
