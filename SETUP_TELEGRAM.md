# Telegram + Scheduler Setup

## 1. Find your chat ID

Open Telegram, message `@userinfobot`. Reply contains `Id: <number>`. That's your `TELEGRAM_ALLOWED_CHAT_ID`.

Alt: start orchestrator with `TELEGRAM_ALLOWED_CHAT_ID=0` (open mode), DM your bot `/start`, it replies with your chat ID. Then set the env var and restart.

## 2. Launch orchestrator with bot enabled

```fish
set -x TELEGRAM_BOT_TOKEN "<telegram-bot-token>"
set -x TELEGRAM_ALLOWED_CHAT_ID <telegram-chat-id>
set -x ORCHESTRATOR_API_KEY "your-existing-or-fresh-key"
./run.fish
```

Should print `[bot] telegram polling started`.

## 3. Bot commands

| Command | What |
|---|---|
| `/start` | Greet + show your chat ID |
| `/help` | List commands |
| `/new [cwd]` | Create new session, becomes active |
| `/sessions` | List active sessions |
| `/use <sid>` | Switch active session for this chat |
| `/stop <sid>` | Kill session |
| `/clear` | Forget active session (does not kill it) |
| `/schedule "<cron>" <prompt>` | Schedule recurring job |
| `/jobs` | List your jobs |
| `/unschedule <id>` | Remove job |
| *plain text* | Send as prompt to active session |

Examples:
```
/new /workspace/myproject
read README.md and summarize
/schedule "0 9 * * *" Summarize yesterday's git activity in /workspace/myproject
/schedule "*/15 9-17 * * 1-5" Check /var/log/syslog for errors since last 15min
/jobs
/unschedule 3
```

## 4. Enable scheduled jobs via cron

The orchestrator stores jobs in SQLite (`orchestrator.db`). System cron pokes the `/jobs/tick` endpoint every minute, and the server runs anything due.

Edit your crontab:
```fish
crontab -e
```

Add (replace API key):
```
* * * * * /usr/bin/curl -fsS -X POST -H "X-API-Key: YOUR_API_KEY" http://localhost:8765/jobs/tick > /dev/null 2>&1
```

Verify:
```fish
crontab -l
```

## 5. Cron syntax reference

5 fields: `minute hour day month weekday`

| Pattern | Meaning |
|---|---|
| `0 9 * * *` | Every day at 09:00 |
| `*/10 * * * *` | Every 10 minutes |
| `0 9-17 * * 1-5` | Hourly 9am–5pm Mon–Fri |
| `30 22 * * 0` | Sundays 22:30 |

Test before scheduling: https://crontab.guru/

## Architecture

```
[Telegram] ←→ [bot.py polling]
                    ↓ uses
              [sessions.py shared pool] ←→ [Web UI / HTTP API]
                    ↑ reads jobs from
                [SQLite db.py]
                    ↑ polled by
              [POST /jobs/tick] ← cron every minute
```

Single process. Bot, web UI, cron handler all share the same `SESSIONS` dict.

## Security notes

- **Subscription quota**: scheduled jobs eat your Claude Code limits. Heavy crons = throttled fast.
- **Bot whitelist**: `TELEGRAM_ALLOWED_CHAT_ID=0` means anyone who finds your bot can use it. Always set it.
- **Permission mode**: jobs default to `acceptEdits`. Auto-runs can write files. Set `permission_mode` lower for risky prompts.
- **Cron URL**: don't put the API key in shell history — store crontab line with the key directly, or use a wrapper script with restricted perms.
- **Public bot exposure**: a chat-ID whitelist is the only gate. Don't share your bot link.
