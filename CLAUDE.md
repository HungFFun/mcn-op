# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`mekoo-op` is the local runner that replaces the Tevi Cowork **scheduled tasks**.
Everything here is pure Python (ClickHouse → format → Telegram) with **no LLM in
the loop**, so it runs on `cron` / Task Scheduler at zero token cost — unlike the
Cowork tasks, where every run booted a full LLM session.

Four tasks live side by side, split across **two Telegram bots** and **two
databases on the same ClickHouse server**:

| Task | Module | DB | Bot → group |
|---|---|---|---|
| Payout batch (ID + PH) | `payout.run` | `tevi_warehouse` (+ `tevi_fiat` for VAI check) | payout bot → payout groups |
| MCN Mekoo daily report | `tasks.mcn_daily_report` | `tevi_warehouse` | recap bot → Mekoo group |
| MCN leave-watch (deduped) | `tasks.mcn_leave_watch` | `tevi_warehouse` | recap bot → Mekoo group |
| Daily payout recap (ID + PH) | `tasks.daily_recap` | `tevi_warehouse` | recap bot → recap groups |

Every module is a port of a Tevi skill: `payout/` ↔ `payout-id` / `payout-ph`;
`tasks/` ↔ `mekoo-mcn-daily-report`, `mcn-leave-watch`, `tevi-daily-recap`. The
warehouse SQL in `tasks/` is carried over verbatim; `payout/` reproduces the
payout-id logic (see the Payout section below).

## Architecture: self-contained, no MCP

The runner connects to ClickHouse and Telegram **directly** — there is no MCP
connector and no orchestrator in the loop. (Some docstrings still mention an
"MCP round-trip" / "orchestrator"; that is the earlier design and is stale — the
`ch_client` / `telegram_client` / `run.py` path replaced it.)

The two families do the same thing through **parallel, separate plumbing** — do
not try to merge them:

- **`payout/`** is self-contained. `payout/settings.py` loads config,
  `payout/ch_client.py` queries the warehouse staging view (and raw `tevi_fiat`
  for the VAI check), `payout/telegram_client.py` posts via the **payout bot**.
- **`common/` + `tasks/`** share the warehouse plumbing. `common/env.py` loads
  config, `common/ch.py` runs SQL → `list[dict]`, `common/telegram.py` posts via
  the **recap bot** (`@mekoo_ops_reporter_bot`).

`payout/settings.py` and `common/env.py` are two separate loaders on purpose
(different bots/tokens). Both merge env sources in the same increasing priority:
`~/.claude/secrets/*.env` files → `<project>/.env` → real environment variables
(real env always wins). Nothing secret is committed.

Run `python3 -m common.check [--live]` to verify wiring before anything else.

## Payout flow (mirrors the payout-id skill)

`payout.run` runs, per market: **VAI pre-flight → fetch Open → resolve cutoff →
fetch Fail credits → load carryover → build PDF + summary → send → persist
surplus**. `payout.build` is the offline, file-based half (Open-only, no
reconciliation) kept so the core is testable without a DB.

1. **VAI pre-flight** (`vai_preflight` + `queries.vai_leaks_sql`, advisory, never
   blocks): flags `Done` requests with empty `vai_tx_id` — Tevi paid the user's
   fiat but never credited STAR/VAI back to the agency = a leak (the 2026-07-02
   settlement bug). Reads **raw `tevi_fiat.wd_tx` FINAL** on purpose — the
   warehouse view backfills `vai_tx_id` and would hide a live leak.
2. **Fetch + parse.** `ch_client.fetch_open` → `parse.parse_rows` → `Record`s.
   Beneficiary field selection is by shape: e-wallet uses
   `account_name`/`phone_number`/`wallet_name`, bank uses
   `holder_name`/`account_number`/`bank` (see ClickHouse notes). If no Open rows,
   the market is skipped (no empty PDF).
3. **Analyze** (`analyze.py`): `totals` (USDT via the market's fixed
   `usdt_rate`, or the warehouse `amount_usd` sum if the rate is None);
   `find_duplicates` groups by the **(account, holder) pair**; `find_suspicious`
   flags empty / brand-as-name / holder==phone / too-short holders;
   `find_missing_details` catches rows with no account at all.
4. **Reconcile** (`reconcile.py`): `resolve_cutoff` = timestamp of the most
   recent prior `payout_<market>_*.pdf` (7-day fallback); Fail credits since the
   cutoff plus any carryover surplus are deducted → `net_transfer`. If credits
   exceed the Open total, the surplus rolls forward in
   `run/state/carryover_<PREFIX>.json` (atomic write, only when >0; cleared once
   consumed).
5. **Build + send.** `pdf_report.build_pdf` renders the table (Method/brand,
   Account/Phone, Account Name, Net, USDT; DUP/CHK highlighted) plus a
   Reconciliation block. `telegram_client.send_payout` posts **one message**: the
   PDF with a caption (USDT headline, net-transfer branch when credits apply, a
   `Batch ID` line Marcus copies into the VAI transfer note). No separate flags
   message — the flags live in the PDF.

## Business rules (payout — do not silently change)

- **Local amount is authoritative; USDT is derived.** `net_amount` (IDR/PHP) is
  the exact transferred figure. USDT = `net_amount / usdt_rate` (ID rate fixed at
  **17100**, per the skill). PH has no fixed rate yet, so it falls back to the
  warehouse `amount_usd` — set `MARKETS["ph"]["usdt_rate"]` once confirmed.
- **Only `Open` requests are billed; Fail credits are deducted.** A request that
  later flips to `Fail` was a prefund never disbursed = a credit owed back,
  subtracted from the next transfer via the cutoff-based reconciliation.
- **Flags never block.** VAI leak / DUP / CHK / missing-data are all advisory —
  the reviewer decides per line; the batch always builds and sends.
- **Source must be FINAL.** Bill from `tevi_warehouse.stg_fiat__withdrawals`
  (view applies FINAL), never raw `wd_tx` without FINAL — the raw table keeps
  stale `Open` rows for requests already `Done` and would nearly double the batch
  (risking a re-pay). The only raw-table read is the VAI leak check (which needs
  the un-backfilled `vai_tx_id`).

## Leave-watch dedup (fixes the old hourly spam)

`tasks.mcn_leave_watch` keeps a state file at
`run/state/mcn-leave-watch-seen.json` and alerts a request only when it is
**newly seen** or has **escalated** to a higher urgency bucket
(`new` → `departing_soon` → `overdue`, ranked in `BUCKET_RANK`). State is
written **only after a successful Telegram send**, using a temp-file + atomic
replace, and IDs no longer pending are pruned. `--reset` clears the file.

## Config is the single source of truth

- Payout markets: `payout/config.py` — agency IDs, currency, `batch_prefix`,
  `usdt_rate` (fixed local-per-USDT, or None to use `amount_usd`), `METHOD_LABELS`
  (11 Bank / 16 E-Wallet / 10 PH), and `SUSPICIOUS_NAME_KEYWORDS`. Change routing,
  rate, or detection keywords there, not inline.
- Warehouse tasks: `common/env.py` — recap-bot token, group chat IDs
  (`CHAT_MEKOO` / `CHAT_RECAP_ID` / `CHAT_RECAP_PH`), `MEKOO_MCN_ID`, agency IDs,
  and `STATE_DIR`. Chat IDs default to the values hardcoded in the old skills and
  are overridable via env.

## Scheduling & control (bot is the main entry)

**`bot.py` (`python3 -m bot`) is how you "run the project"** — a Telegram control
bot that ALSO runs the scheduler in a background thread, so one process both
fires tasks on their clock and lets you manage everything from a chat:
`/status`, `/schedule`, `/settime <task> <08:00,15:00|off>`, `/run <task>
[nosend]` (replies with the log tail when done), `/log <task> [N]`. Config in
`common/env.py`: `CONTROL_BOT_TOKEN` (falls back to the recap bot) and
`CONTROL_ALLOWED` (allow-list of chat ids; empty = locked, DM `/start` to get
your id). It can trigger real payouts — keep the allow-list tight.

`scheduler.py` is the scheduling core. Times live in **`run/schedule.json`**
(`{task: ["HH:MM", ...]}`, ICT), read fresh **every tick** so `/settime` edits
apply with no restart; `DEFAULT_SCHEDULE` seeds it (08:00 for the three reports +
payout; payout also 11/15/18/22). `set_times()` validates + persists; `due()`
matches `HH:MM`. `serve()` launches each task as a subprocess to
`run/logs/<task>.log`, touches `run/scheduler.heartbeat` each tick, and does not
back-run missed fires. Standalone use: `--list` / `--dry-run HH:MM` / `--once
TASK`. `task_cmd()` builds the `python -m` argv (single source: `TASK_SPECS`).

`webui.py` (`python3 -m webui`) is an optional stdlib-only (`http.server`) local
dashboard — run tasks on demand (Dry-run button), watch the heartbeat, tail logs.
Binds `127.0.0.1`; never expose it. OS cron/Task Scheduler remain alternatives
(README) if you don't want the bot process.

## Commands

```bash
pip3 install -r requirements.txt --break-system-packages   # reportlab, clickhouse-connect, requests

python3 -m common.check           # show resolved config (no network)
python3 -m common.check --live    # + ping ClickHouse and Telegram getMe

# Wrapper (recommended — handles the working directory):
scripts/run.sh payout             # payout.run --market both
scripts/run.sh daily-recap
scripts/run.sh mcn-report
scripts/run.sh leave-watch
scripts/run.sh bot                # ★ main entry: Telegram control bot + scheduler (blocks)
scripts/run.sh schedule           # scheduler only (no bot; blocks)
scripts/run.sh webui              # optional web UI → http://127.0.0.1:8787
# Windows: scripts\run.bat <task> [args]   (bot = "run the project")

# Direct:
python3 -m payout.run --market both          # id | ph | both; fetch+build+send
python3 -m tasks.daily_recap
python3 -m tasks.mcn_daily_report
python3 -m tasks.mcn_leave_watch
```

Common flags: `--no-send` (query + print, don't post — all tasks),
`--sample` (render from built-in sample data, no DB — the two report tasks),
`--reset` (leave-watch: clear dedup state), `--now "YYYY-MM-DD HH:MM"` (payout),
`--market {id|ph|both}` (payout).

Offline payout test from a saved ClickHouse result (no DB):
```bash
python3 -m payout.build --market id --rows run/rows_id.json --outdir run/out
```

There are **no tests, linter, or build config**. `run/` is git-ignored scratch
(PDFs, saved rows, dedup state). Each task reports per-item and keeps going on
error rather than aborting the batch.

## ClickHouse notes

- **Payout billing** reads `tevi_warehouse.stg_fiat__withdrawals` (view already
  applies FINAL + `_peerdb_is_deleted=0`, so one current row per request). Filter
  on `original_status` ('Open' / 'Fail' / 'Done'). Beneficiary lives in
  `payload.transaction.billing_detail.*` in two mutually-exclusive shapes —
  bank: `holder_name`/`account_number`/`bank`(+`bank_name`); e-wallet:
  `account_name`/`phone_number`/`wallet_name` — which `payout/queries.py`
  coalesces into holder/account/bank. `payout_method_id`: 11 Bank / 16 E-Wallet
  (ID), 10 (PH). USD value is the view's `amount_usd` column.
- **VAI leak check** is the one read of raw `tevi_fiat.wd_tx FINAL` (needs the
  un-backfilled `vai_tx_id`); still filter `_peerdb_is_deleted=0`.
- Warehouse tasks query `tevi_warehouse.*` with **fully-qualified table names**,
  so the connected default database (`tevi_fiat`) does not matter. All date logic
  is anchored to `now('Asia/Ho_Chi_Minh')` (ICT); reports cover yesterday + MTD.
- **Never append `FORMAT ...`** — the client sets its own format.
