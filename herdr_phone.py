#!/usr/bin/env python3
"""herdr-phone: talk to your herdr agents from your phone via Telegram.

Runs on the same machine as herdr. It talks to herdr's local socket API and
to Telegram over outbound HTTPS only (long polling), so you don't need to open
ports, set up a tunnel, or keep an SSH session alive on your phone.

  * Pings you when an agent finishes or needs approval (shows up as a normal
    notification banner over Instagram, games, whatever).
  * Reply to a notification to send that agent a prompt.
  * Tap buttons to approve / pick options / interrupt.

Python 3.9+ standard library only. See README.md for setup.
"""

from __future__ import annotations

import html
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- config ----

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_SECONDS = float(os.environ.get("HERDR_PHONE_POLL_SECONDS", "2"))
TAIL_LINES = int(os.environ.get("HERDR_PHONE_TAIL_LINES", "25"))
TELEGRAM_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
# Which transitions ping you. "done" = agent finished a turn, "blocked" = it is
# waiting on an approval/question.
NOTIFY_ON = set(
    s.strip() for s in os.environ.get("HERDR_PHONE_NOTIFY", "blocked,done").split(",") if s.strip()
)

TELEGRAM_MAX = 4096
STATUS_EMOJI = {
    "working": "🔵",
    "blocked": "🟡",
    "done": "✅",
    "idle": "⚪",
    "unknown": "❔",
}


def socket_path() -> str:
    """Same resolution order as the herdr CLI (minus --session)."""
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr"
    session = os.environ.get("HERDR_SESSION")
    if session:
        return str(config / "sessions" / session / "herdr.sock")
    return str(config / "herdr.sock")


# ----------------------------------------------------------------- herdr ----


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Herdr:
    """Minimal newline-delimited JSON client for herdr's socket API."""

    def __init__(self, path: str):
        self.path = path
        self._seq = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        with self._lock:
            self._seq += 1
            req_id = f"phone_{self._seq}"
        req = {"id": req_id, "method": method, "params": params or {}}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(self.path)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf.strip():
            raise HerdrError("no_response", "herdr closed the connection")
        resp = json.loads(buf.split(b"\n", 1)[0])
        if "error" in resp:
            err = resp["error"] or {}
            raise HerdrError(err.get("code", "error"), err.get("message", ""))
        return resp.get("result", {})

    def agents(self) -> list[dict]:
        return self.call("agent.list").get("agents", [])

    def read(self, target: str, lines: int = TAIL_LINES) -> str:
        res = self.call(
            "agent.read", {"target": target, "source": "recent", "lines": lines}
        )
        read = res.get("read", res)
        return read.get("text", "")

    def prompt(self, target: str, text: str) -> None:
        self.call("agent.prompt", {"target": target, "text": text})

    def keys(self, target: str, keys: list[str]) -> None:
        self.call("agent.send_keys", {"target": target, "keys": keys})


def agent_label(a: dict) -> str:
    name = a.get("name") or a.get("display_agent") or a.get("agent") or "agent"
    where = a.get("title") or a.get("terminal_title_stripped") or ""
    cwd = a.get("foreground_cwd") or a.get("cwd") or ""
    extra = where or (Path(cwd).name if cwd else "")
    label = f"{name} · {a['pane_id']}"
    return f"{label} — {extra}" if extra else label


def agent_target(a: dict) -> str:
    # A unique agent name survives pane moves; fall back to the pane id.
    return a.get("name") or a["pane_id"]


# -------------------------------------------------------------- telegram ----


class Telegram:
    def __init__(self, token: str):
        self.base = f"{TELEGRAM_API}/bot{token}/"

    def call(self, method: str, http_timeout: float = 60, **params) -> dict | list:
        data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(
            self.base + method, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            body = json.loads(r.read())
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body}")
        return body["result"]

    def send(self, chat_id, text: str, buttons=None, reply_to=None) -> dict:
        markup = {"inline_keyboard": buttons} if buttons else None
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:TELEGRAM_MAX],
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup,
            reply_to_message_id=reply_to,
        )


def pre(text: str, budget: int = 3300) -> str:
    text = text.rstrip()
    if len(text) > budget:
        text = "…" + text[-budget:]
    return f"<pre>{html.escape(text) or '(empty)'}</pre>"


def action_buttons(target: str) -> list[list[dict]]:
    """Quick-reply buttons. callback_data must stay under 64 bytes."""

    def b(label, action):
        return {"text": label, "callback_data": f"{action}|{target}"[:64]}

    return [
        [b("1", "k:1"), b("2", "k:2"), b("3", "k:3"), b("⏎ Enter", "k:enter")],
        [b("✅ y", "k:y"), b("❌ n", "k:n"), b("⎋ Esc", "k:esc"), b("⛔ Ctrl-C", "k:ctrl+c")],
        [b("📜 Show more", "read"), b("🎯 Talk to this one", "use")],
    ]


# ------------------------------------------------------------------- bot ----


class Bridge:
    def __init__(self, herdr: Herdr, tg: Telegram, chat_id: str):
        self.herdr = herdr
        self.tg = tg
        self.chat_id = chat_id
        self.current: str | None = None  # agent target plain messages go to
        self.msg_target: dict[int, str] = {}  # telegram message id -> agent target
        self.muted = False
        self.last_status: dict[str, str] = {}
        self.herdr_down_since: float | None = None

    # -- outgoing ---------------------------------------------------------

    def say(self, text: str, buttons=None, target: str | None = None, reply_to=None):
        try:
            msg = self.tg.send(self.chat_id, text, buttons, reply_to)
        except Exception as e:  # never let a send failure kill a loop
            print(f"[herdr-phone] send failed: {e}", file=sys.stderr)
            return
        if target:
            self.msg_target[msg["message_id"]] = target
            if len(self.msg_target) > 500:
                for k in list(self.msg_target)[:250]:
                    del self.msg_target[k]

    def notify(self, a: dict, status: str):
        target = agent_target(a)
        head = {
            "blocked": "🟡 <b>needs you</b>",
            "done": "✅ <b>finished</b>",
        }.get(status, f"{STATUS_EMOJI.get(status, '')} <b>{status}</b>")
        try:
            tail = self.herdr.read(target)
        except HerdrError as e:
            tail = f"(couldn't read output: {e})"
        self.say(
            f"{head} · {html.escape(agent_label(a))}\n{pre(tail)}\n"
            "<i>Reply to this message to answer it.</i>",
            buttons=action_buttons(target),
            target=target,
        )

    # -- watcher ----------------------------------------------------------

    def watch_forever(self):
        first = True
        while True:
            try:
                agents = self.herdr.agents()
                if self.herdr_down_since is not None:
                    self.say("🟢 herdr is reachable again.")
                    self.herdr_down_since = None
            except (OSError, HerdrError, ValueError) as e:
                if self.herdr_down_since is None:
                    self.herdr_down_since = time.time()
                    if not first:
                        self.say(f"🔴 lost contact with herdr: {html.escape(str(e))}")
                first = False
                time.sleep(max(POLL_SECONDS, 5))
                continue

            seen = set()
            for a in agents:
                pane = a["pane_id"]
                seen.add(pane)
                status = a.get("agent_status", "unknown")
                prev = self.last_status.get(pane)
                self.last_status[pane] = status
                if first or prev == status or self.muted:
                    continue
                # "done" only matters coming out of real work; idle->done flaps
                # aren't interesting.
                if status == "done" and prev not in ("working", "blocked"):
                    continue
                if status in NOTIFY_ON:
                    self.notify(a, status)
            for gone in set(self.last_status) - seen:
                del self.last_status[gone]
            first = False
            time.sleep(POLL_SECONDS)

    # -- incoming ---------------------------------------------------------

    def resolve(self, wanted: str | None) -> str | None:
        """Turn user input (name, pane id, list index) into an agent target."""
        agents = self.herdr.agents()
        if not wanted:
            if self.current:
                return self.current
            if len(agents) == 1:
                return agent_target(agents[0])
            return None
        if wanted.isdigit() and 1 <= int(wanted) <= len(agents):
            return agent_target(agents[int(wanted) - 1])
        for a in agents:
            if wanted in (a.get("name"), a["pane_id"]):
                return agent_target(a)
        return None

    def cmd_agents(self):
        agents = self.herdr.agents()
        if not agents:
            self.say("No agents running in herdr right now.")
            return
        lines, buttons = [], []
        for i, a in enumerate(agents, 1):
            st = a.get("agent_status", "unknown")
            mark = " ← talking to" if agent_target(a) == self.current else ""
            lines.append(f"{i}. {STATUS_EMOJI.get(st, '')} {html.escape(agent_label(a))} <i>{st}</i>{mark}")
            buttons.append(
                [{"text": f"{i}. {agent_label(a)}"[:60], "callback_data": f"use|{agent_target(a)}"[:64]}]
            )
        self.say("<b>Agents</b>\n" + "\n".join(lines), buttons=buttons)

    def cmd_read(self, target: str | None, lines: int = TAIL_LINES):
        if not target:
            self.say("Which agent? Send /agents and tap one first.")
            return
        self.say(
            f"📜 <b>{html.escape(target)}</b>\n{pre(self.herdr.read(target, lines))}",
            buttons=action_buttons(target),
            target=target,
        )

    def send_prompt(self, target: str | None, text: str, reply_to=None):
        if not target:
            self.say("Not sure which agent you mean. Send /agents and tap one, then type again.")
            return
        try:
            self.herdr.prompt(target, text)
        except HerdrError as e:
            if e.code == "agent_blocked":
                self.say(
                    f"🟡 <b>{html.escape(target)}</b> is waiting on a question/approval, "
                    "so I didn't type over it. Use a button:",
                    buttons=action_buttons(target),
                    target=target,
                )
                return
            raise
        self.current = target
        self.say(f"➡️ sent to <b>{html.escape(target)}</b>", target=target, reply_to=reply_to)

    def handle_message(self, msg: dict):
        text = (msg.get("text") or "").strip()
        if not text:
            return
        reply = msg.get("reply_to_message") or {}
        replied_target = self.msg_target.get(reply.get("message_id"))

        if not text.startswith("/"):
            self.send_prompt(replied_target or self.resolve(None), text, msg["message_id"])
            return

        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help"):
            self.say(HELP)
        elif cmd in ("/agents", "/a", "/list"):
            self.cmd_agents()
        elif cmd == "/use":
            t = self.resolve(arg)
            if t:
                self.current = t
                self.say(f"🎯 Now talking to <b>{html.escape(t)}</b>. Just type.", target=t)
            else:
                self.cmd_agents()
        elif cmd in ("/read", "/r"):
            parts = arg.split()
            n = int(parts[-1]) if parts and parts[-1].isdigit() and len(parts[-1]) > 1 else TAIL_LINES
            who = parts[0] if parts and not (parts[0].isdigit() and len(parts[0]) > 1) else None
            self.cmd_read(replied_target or self.resolve(who), min(n, 400))
        elif cmd in ("/to", "/p"):
            who, _, body = arg.partition(" ")
            self.send_prompt(self.resolve(who), body.strip(), msg["message_id"])
        elif cmd in ("/keys", "/k"):
            t = replied_target or self.resolve(None)
            if not t or not arg:
                self.say("Usage: /keys enter  ·  /keys esc  ·  /keys 2 enter")
                return
            self.herdr.keys(t, arg.split())
            self.say(f"⌨️ sent <code>{html.escape(arg)}</code> to <b>{html.escape(t)}</b>", target=t)
        elif cmd in ("/stop", "/esc"):
            t = replied_target or self.resolve(arg or None)
            if t:
                self.herdr.keys(t, ["esc"])
                self.say(f"⎋ sent Esc to <b>{html.escape(t)}</b>", target=t)
        elif cmd == "/mute":
            self.muted = True
            self.say("🔕 Muted. I'll stay quiet until /unmute.")
        elif cmd == "/unmute":
            self.muted = False
            self.say("🔔 Notifications back on.")
        else:
            self.say("Unknown command. /help")

    def handle_callback(self, cb: dict):
        data = cb.get("data", "")
        action, _, target = data.partition("|")
        note = ""
        if action == "use":
            self.current = target
            note = f"Talking to {target}"
            self.say(f"🎯 Now talking to <b>{html.escape(target)}</b>. Just type.", target=target)
        elif action == "read":
            self.cmd_read(target, 80)
        elif action.startswith("k:"):
            key = action[2:]
            self.herdr.keys(target, [key])
            note = f"Sent {key} → {target}"
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=cb["id"], text=note or None)
        except Exception:
            pass

    def poll_forever(self):
        offset = None
        while True:
            try:
                updates = self.tg.call(
                    "getUpdates",
                    timeout=40,  # Telegram long-poll seconds
                    offset=offset,
                    allowed_updates=["message", "callback_query"],
                )
            except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as e:
                print(f"[herdr-phone] telegram poll error: {e}", file=sys.stderr)
                time.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self.dispatch(u)

    def dispatch(self, u: dict):
        msg = u.get("message")
        cb = u.get("callback_query")
        chat = (msg or (cb or {}).get("message") or {}).get("chat", {})
        if str(chat.get("id")) != str(self.chat_id):
            # Only the owner can drive agents. Tell strangers nothing useful.
            if msg and msg.get("text", "").startswith("/start"):
                print(
                    f"[herdr-phone] ignored chat {chat.get('id')} — set TELEGRAM_CHAT_ID to allow it",
                    file=sys.stderr,
                )
            return
        try:
            if msg:
                self.handle_message(msg)
            elif cb:
                self.handle_callback(cb)
        except (OSError, HerdrError) as e:
            self.say(f"⚠️ {html.escape(str(e))}")


HELP = """<b>herdr-phone</b> 📱 → 🐑

<b>Just type</b> — goes to the agent you're talking to.
<b>Reply</b> to any notification — goes to that agent.

/agents — list agents, tap one to talk to it
/use 2 — talk to agent #2 (or a name / pane id like w1:p2)
/read — last lines of output  (/read 100 for more)
/to 2 fix the test — one-off message to agent #2
/keys esc — send keys (enter, esc, up, 1, ctrl+c…)
/stop — send Esc (interrupt Claude/Codex)
/mute · /unmute — pause notifications

You'll get pinged when an agent 🟡 needs you or ✅ finishes."""


# ------------------------------------------------------------------ main ----


def find_chat_id(tg: Telegram):
    """Setup helper: print the chat id of whoever messages the bot."""
    print("Send any message to your bot in Telegram now… (Ctrl-C to quit)")
    offset = None
    while True:
        for u in tg.call("getUpdates", timeout=40, offset=offset):
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat")
            if chat:
                who = chat.get("username") or chat.get("first_name") or ""
                print(f"\nTELEGRAM_CHAT_ID={chat['id']}   ({who})")
                print("Put that in your .env and run herdr-phone again.")
                return


def load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main(argv: list[str]) -> int:
    global BOT_TOKEN, CHAT_ID
    load_dotenv(Path(__file__).with_name(".env"))
    load_dotenv(Path.cwd() / ".env")
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN (from @BotFather). See README.md.", file=sys.stderr)
        return 2
    tg = Telegram(BOT_TOKEN)
    if "--whoami" in argv or not CHAT_ID:
        find_chat_id(tg)
        return 0

    herdr = Herdr(socket_path())
    try:
        herdr.call("ping", timeout=3)
        print(f"[herdr-phone] connected to herdr at {herdr.path}")
    except (OSError, HerdrError) as e:
        print(f"[herdr-phone] herdr not reachable yet ({e}); will keep trying", file=sys.stderr)

    bridge = Bridge(herdr, tg, CHAT_ID)
    threading.Thread(target=bridge.watch_forever, daemon=True).start()
    bridge.say("📱 herdr-phone is online. Send /agents to see who's working.")
    try:
        bridge.poll_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
