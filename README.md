# herdr-phone 📱 → 🐑

Message your [herdr](https://github.com/ogulcancelik/herdr) agents from your phone while you're scrolling Instagram or playing a game.

herdr's own phone setup means opening an SSH app and using the full terminal UI. That's fine at a desk, but you can't do it in between Reels. herdr-phone is a small Telegram bot that runs next to herdr on your computer:

- **It pings you** when an agent ✅ **finishes** or 🟡 **needs you** (an approval or a question). The ping is a normal notification banner, so it shows up on top of Instagram or a game, and you can answer from the banner.
- **Reply to a ping** to send that agent your next prompt.
- **Tap buttons** to approve, pick option 1/2/3, press Enter or Esc, or Ctrl‑C.
- **Just type** to talk to whichever agent you picked last.

```
🟡 needs you · claude · w1:p1 — api
┌──────────────────────────────────┐
│ Allow edit to src/auth.rs?       │
│ ❯ 1. Yes                         │
│   2. Yes, don't ask again        │
│   3. No, tell Claude what to do  │
└──────────────────────────────────┘
[ 1 ] [ 2 ] [ 3 ] [ ⏎ Enter ]
[ ✅ y ] [ ❌ n ] [ ⎋ Esc ] [ ⛔ Ctrl-C ]
[ 📜 Show more ] [ 🎯 Talk to this one ]
```

There's nothing to install: it's one Python file that uses only the standard library. It also doesn't open any ports or need a tunnel. The bot only makes outbound HTTPS calls to Telegram and talks to herdr through herdr's local socket.

## Setup (about 3 minutes)

1. **Create your bot.** In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token it gives you.

2. **On the computer where herdr runs:**

   ```bash
   git clone https://github.com/Saj8292008/Vibe-coding-.git herdr-phone
   cd herdr-phone
   cp .env.example .env
   # paste your token into .env as TELEGRAM_BOT_TOKEN=...
   ```

3. **Find your chat id.** Only your chat id is allowed to control your agents.

   ```bash
   python3 herdr_phone.py --whoami
   ```

   Send your bot any message in Telegram. The script prints `TELEGRAM_CHAT_ID=123456`. Put that line in `.env`.

4. **Run it.** Start herdr first, as you normally do, and then run:

   ```bash
   python3 herdr_phone.py
   ```

   Your phone gets the message "📱 herdr-phone is online". Send `/agents` to check it's working.

To keep it running after you close the terminal, use one of the options in [Run it in the background](#run-it-in-the-background).

## Commands

| You send | What happens |
| --- | --- |
| *any text* | Sends that text as a prompt to the agent you're talking to |
| *reply to a ping* | Sends your text as a prompt to the agent in that ping |
| `/agents` | Lists your agents and their status. Tap one to talk to it |
| `/use 2` | Talk to agent #2. You can also use a name or a pane id like `w1:p2` |
| `/read` | Shows the agent's last 25 lines. `/read 100` shows more |
| `/to 2 run the tests` | Sends one message to agent #2 without switching to it |
| `/keys esc` | Sends keys. Some examples: `enter`, `esc`, `up`, `2 enter`, `ctrl+c` |
| `/stop` | Sends Esc, which interrupts Claude Code or Codex |
| `/mute` / `/unmute` | Pauses or resumes notifications |

When an agent is waiting on an approval, herdr-phone **won't type over the prompt**. It shows you the buttons instead. This matches herdr's own `agent_blocked` safety check.

## Settings (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | The token from @BotFather (required) |
| `TELEGRAM_CHAT_ID` | – | Your chat id (required). Messages from any other chat are ignored |
| `HERDR_PHONE_NOTIFY` | `blocked,done` | Which status changes ping you. Add `working` if you want a ping when an agent starts |
| `HERDR_PHONE_TAIL_LINES` | `25` | How many output lines a ping includes |
| `HERDR_PHONE_POLL_SECONDS` | `2` | How often the bot checks herdr for status changes |
| `HERDR_SESSION` / `HERDR_SOCKET_PATH` | herdr default | Which herdr session to use. This works the same way as in the herdr CLI |

## Run it in the background

**tmux / a herdr pane:** Run it in a herdr pane of its own. That's simple and it's enough for most people.

**Linux (systemd user service):**

```bash
mkdir -p ~/.config/systemd/user
cp herdr-phone.service ~/.config/systemd/user/
# edit WorkingDirectory in the file if you cloned somewhere other than ~/herdr-phone
systemctl --user enable --now herdr-phone
journalctl --user -u herdr-phone -f
```

**macOS:**

```bash
nohup python3 herdr_phone.py > ~/herdr-phone.log 2>&1 &
```

## Tips

- In Telegram, turn notifications **on** for your bot and switch on **Show preview**. Then a ping shows the agent's question right in the banner.
- To answer from the banner without leaving your game, long-press the notification (iOS) or tap **Reply** (Android).
- If you run herdr on a server, run herdr-phone on that server too. It only needs outbound internet access.

## Security

Whoever controls the bot can type into your terminals. Keep these points in mind:

- The bot answers **one** chat id only. Messages from anyone else are dropped without a reply.
- Keep your bot token secret. `.env` is already git-ignored. If the token leaks, send `/revoke` to @BotFather.
- Telegram can read your bot messages, because bot chats aren't end-to-end encrypted. Don't use herdr-phone for agents that print secrets.

## Development

```bash
python3 -m unittest -v test_herdr_phone
```

The tests use a fake herdr socket server and a stub Telegram client, so they don't need herdr or a network connection.
