# mekoo-op — Tevi local ops runner

Local Python replacement for the Tevi Cowork **scheduled tasks**. Each task runs
as a plain script (ClickHouse → format → Telegram) with **no LLM in the loop**,
so running them on `cron` / Task Scheduler costs **zero tokens** — unlike the
Cowork scheduled tasks, where every run booted a full LLM session.

## Tasks

| Task | Module | Source DB | Bot → group |
|---|---|---|---|
| Payout batch (ID + PH) | `payout.run` | `tevi_fiat` | payout bot → payout groups |
| MCN Mekoo daily report | `tasks.mcn_daily_report` | `tevi_warehouse` | recap bot → Mekoo group |
| MCN leave-watch (deduped) | `tasks.mcn_leave_watch` | `tevi_warehouse` | recap bot → Mekoo group |
| Daily payout recap (ID + PH) | `tasks.daily_recap` | `tevi_warehouse` | recap bot → recap groups |

The **payout** runner and the three **warehouse tasks** use two different
Telegram bots (payout bot vs. recap bot `@mekoo_ops_reporter_bot`) and query two
different databases on the same ClickHouse server.

## Layout

```
payout/        Payout batch runner (self-contained; bills tevi_warehouse.stg_fiat__withdrawals)
common/        Shared helpers for the warehouse tasks
  env.py         Credential + config loader (env files + real env)
  ch.py          Generic ClickHouse query -> list[dict]
  telegram.py    Recap-bot Bot API sender
  check.py       Config diagnostic (python3 -m common.check [--live])
tasks/
  mcn_daily_report.py   MCN health report (4 queries, yesterday + MTD)
  mcn_leave_watch.py    Leave-request alert, deduped via run/state/*.json
  daily_recap.py        Yesterday's closed-payout stats per agency
bot.py          Telegram control bot + scheduler — the "run the project" entry
scheduler.py    Daily scheduler core (times in run/schedule.json, ICT)
webui.py        Optional local web dashboard (stdlib http.server, 127.0.0.1)
scripts/
  run.sh / run.bat      Cross-platform runner (bot|schedule|webui|<task>|check)
.env.example    Credential template
run/            Git-ignored scratch: logs/, state/, out/, schedule.json
```

## Setup

```bash
# 1. Dependencies
pip3 install -r requirements.txt --break-system-packages

# 2. ClickHouse — create ~/.claude/secrets/clickhouse.env
#      CLICKHOUSE_HOST=172.16.0.64
#      CLICKHOUSE_PORT=8123
#      CLICKHOUSE_USER=default
#      CLICKHOUSE_PASSWORD=...
#      CLICKHOUSE_DATABASE=tevi_fiat

# 3. Recap bot — create ~/.claude/secrets/telegram_recap.env
#      TELEGRAM_RECAP_BOT_TOKEN=123456:ABC...
#    (payout bot stays in ~/.claude/secrets/telegram_payout.env, as before)

# 4. Verify wiring (no network), then live:
python3 -m common.check
python3 -m common.check --live
```

Credentials merge in increasing priority: secrets env files → `<project>/.env`
→ real environment variables. Nothing secret is committed.

## Run

```bash
# via the wrapper (recommended — handles the working directory)
scripts/run.sh daily-recap
scripts/run.sh mcn-report
scripts/run.sh leave-watch
scripts/run.sh payout

# or directly
python3 -m tasks.daily_recap
python3 -m tasks.mcn_daily_report
python3 -m tasks.mcn_leave_watch
python3 -m payout.run --market both
```

Useful flags:

- `--no-send` — query the DB and print the message, but don't post to Telegram.
- `--sample` — (report tasks) render the message from built-in sample data, no DB.
- `--reset` — (leave-watch) clear the dedup state file.
- Windows: `scripts\run.bat daily-recap` etc.

### Leave-watch dedup (fixes the hourly spam)

The old scheduled task re-posted every pending request every hour. This version
keeps a state file at `run/state/mcn-leave-watch-seen.json` and alerts a request
only when it is **newly seen** or has **escalated** to a higher urgency bucket
(`new` → `departing_soon` → `overdue`). First run after a reset treats everything
as new; subsequent runs stay silent unless something changes.

## Running it — the control bot

**To "run the project", start the Telegram control bot.** One process both runs
the scheduler (fires tasks on their clock) and lets you manage everything from a
chat — set times, run tasks on demand, read logs. The Windows box just needs to
keep this running:

```bash
scripts/run.sh bot                 # blocks — keep the process alive
# Windows: scripts\run.bat bot
```

### Setup (once)

1. Create a control bot in Telegram via **@BotFather**, put its token in
   `~/.claude/secrets/telegram_recap.env`:
   ```
   TELEGRAM_CONTROL_BOT_TOKEN=123456:ABC...
   ```
   (or omit it to reuse the recap bot token).
2. Start the bot, then **DM it `/start`** — it replies with your chat id. Add
   the allowed chat ids and restart:
   ```
   TELEGRAM_CONTROL_CHAT_IDS=123456789,-1002222
   ```
   Empty allow-list = locked (only `/start` works). The bot can trigger real
   payouts, so keep this tight.

### Commands

| Command | Does |
|---|---|
| `/status` | scheduler alive? + schedule + last-run times |
| `/schedule` | current fire times per task |
| `/settime <task> <08:00,15:00>` | set a task's ICT times (`off` to disable) |
| `/run <task> [nosend]` | run now; replies with the log tail when done |
| `/log <task> [N]` | last N lines of the task's log |
| `/tasks` | list task keys |

Default schedule (ICT), editable live via `/settime` (persisted to
`run/schedule.json`, applied on the next tick — no restart):

| Time (ICT) | Tasks |
|---|---|
| 08:00 | mcn-report · daily-recap · leave-watch · payout |
| 11:00, 15:00, 18:00, 22:00 | payout |

Each fire launches a subprocess; output → `run/logs/<task>.log`. Missed fires
while the process was down are not back-run. Keep the bot alive across reboots
with a process manager (systemd/NSSM/launchd), or use the OS scheduler below.

### Scheduler / web UI without the bot (optional)

```bash
scripts/run.sh schedule            # scheduler only (no bot)
python3 -m scheduler --list | --dry-run 08:00 | --once payout
scripts/run.sh webui               # local dashboard → http://127.0.0.1:8787 (binds 127.0.0.1)
```

### OS scheduler (alternative)

Run the wrappers directly from cron / Task Scheduler instead of the bot:

### macOS / Linux — crontab

```cron
# edit with: crontab -e   (paths must be absolute)
# minute hour * * *   command
0  8 * * *  /Users/you/Desktop/TEVI/SRC/mekoo-op/scripts/run.sh daily-recap   >> ~/mekoo-op.log 2>&1
3  8 * * *  /Users/you/Desktop/TEVI/SRC/mekoo-op/scripts/run.sh mcn-report    >> ~/mekoo-op.log 2>&1
0  8,12,15 * * *  /Users/you/Desktop/TEVI/SRC/mekoo-op/scripts/run.sh payout  >> ~/mekoo-op.log 2>&1
0  */2 * * *  /Users/you/Desktop/TEVI/SRC/mekoo-op/scripts/run.sh leave-watch >> ~/mekoo-op.log 2>&1
```

Cron uses a minimal `PATH`; if `python3` isn't found, set `PYTHON=/usr/bin/python3`
at the top of the crontab or export it before the command.

### Windows — Task Scheduler

Register once per task (run in an elevated PowerShell or cmd). Adjust the project
path:

```bat
schtasks /Create /TN "mekoo-daily-recap" /TR "\"C:\TEVI\SRC\mekoo-op\scripts\run.bat\" daily-recap" /SC DAILY /ST 08:00
schtasks /Create /TN "mekoo-mcn-report"  /TR "\"C:\TEVI\SRC\mekoo-op\scripts\run.bat\" mcn-report"  /SC DAILY /ST 08:03
schtasks /Create /TN "mekoo-payout-am"   /TR "\"C:\TEVI\SRC\mekoo-op\scripts\run.bat\" payout"      /SC DAILY /ST 08:00
schtasks /Create /TN "mekoo-payout-noon" /TR "\"C:\TEVI\SRC\mekoo-op\scripts\run.bat\" payout"      /SC DAILY /ST 12:00
schtasks /Create /TN "mekoo-payout-pm"   /TR "\"C:\TEVI\SRC\mekoo-op\scripts\run.bat\" payout"      /SC DAILY /ST 15:00
schtasks /Create /TN "mekoo-leave-watch" /TR "\"C:\TEVI\SRC\mekoo-op\scripts\run.bat\" leave-watch" /SC HOURLY /MO 2
```

Unlike Cowork tasks, these run whether or not the Claude app is open — they only
need the machine powered on and network access to ClickHouse + Telegram.

## Business rules (payout)

Reproduces the `payout-id` / `payout-ph` skill logic:

- **Source is the FINAL staging view** `tevi_warehouse.stg_fiat__withdrawals` —
  never the raw `wd_tx` (which keeps stale `Open` rows for already-`Done`
  requests and would nearly double the batch).
- **Local amount is authoritative; USDT is derived.** `net_amount` (IDR/PHP) is
  the exact transferred figure; USDT = `net_amount / rate` (ID fixed at 17100;
  PH falls back to the warehouse `amount_usd`).
- **Only `Open` requests are billed, minus Fail credits.** A request that later
  flips to `Fail` is a prefund never disbursed = a credit deducted from the next
  transfer (cutoff = the prior PDF's timestamp). Excess credit rolls forward in
  `run/state/carryover_<PREFIX>.json`.
- **VAI credit pre-flight.** Before billing, flags `Done` requests Tevi hasn't
  credited STAR/VAI for (`vai_tx_id` empty) — a leak. Advisory, never blocks.
- **Flags never block.** VAI leak / DUP / suspicious-name / missing-data are all
  advisory; the reviewer decides per line.

See `CLAUDE.md` for architecture notes.
