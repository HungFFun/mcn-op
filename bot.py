"""Telegram control bot for the ops tasks — the main "run the project" entry.

Start it once and leave it running (Windows: `scripts\\run.bat bot`). It:
  - runs the daily scheduler in a background thread (fires tasks on their times),
  - drives everything from an inline-button menu in Telegram (no typing commands).

Send /start (or /menu) to open the menu. From there: Status, Schedule, Run a
task (with a Dry-run option + confirm before a real send), Logs, and Set time
(presets or a custom reply).

Config: TELEGRAM_CONTROL_BOT_TOKEN (falls back to the recap bot),
TELEGRAM_CONTROL_CHAT_IDS (comma-separated allow-list). See .env.example.
"""

from __future__ import annotations

import argparse
import html
import json
import threading
import time

import scheduler
from common import env

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("requests is not installed. Run: pip3 install -r requirements.txt") from exc

_API = "https://api.telegram.org/bot{token}/{method}"
_MAXLEN = 3500

SCHEDULABLE = scheduler.SCHEDULABLE          # payout, mcn-report, daily-recap, leave-watch
RUNNABLE = list(scheduler.TASKS)             # + check
_pending: dict[str, str] = {}                # chat_id -> task awaiting a custom time reply


# --- Telegram helpers --------------------------------------------------------

def _api(method: str, **params):
    return requests.post(_API.format(token=env.CONTROL_BOT_TOKEN, method=method),
                         data=params, timeout=40).json()


def esc(s) -> str:
    return html.escape(str(s))


def _kb(rows: list[list[tuple[str, str]]]) -> str:
    return json.dumps({"inline_keyboard":
                       [[{"text": t, "callback_data": d} for t, d in row] for row in rows]})


def send(chat_id, text: str, kb: str | None = None) -> None:
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if kb:
        p["reply_markup"] = kb
    _api("sendMessage", **p)


def edit(chat_id, message_id, text: str, kb: str | None = None) -> None:
    p = {"chat_id": chat_id, "message_id": message_id, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if kb:
        p["reply_markup"] = kb
    _api("editMessageText", **p)


def answer(cq_id, text: str = "") -> None:
    _api("answerCallbackQuery", callback_query_id=cq_id, text=text)


def allowed(chat_id) -> bool:
    return str(chat_id) in env.CONTROL_ALLOWED


# --- menus -------------------------------------------------------------------

def _menu_kb() -> str:
    return _kb([
        [("📊 Status", "status"), ("🗓 Schedule", "sched")],
        [("▶️ Run", "run"), ("📄 Logs", "log")],
        [("⏰ Set time", "set")],
    ])


def _task_picker(prefix: str, tasks: list[str], back: str = "menu") -> str:
    rows = [[(t, f"{prefix}:{t}")] for t in tasks]
    rows.append([("⬅️ Menu", back)])
    return _kb(rows)


def _presets(task: str) -> list[str]:
    if task == "payout":
        return ["08:00,11:00,15:00,18:00,22:00", "08:00,15:00", "08:00"]
    return ["08:00", "08:00,15:00", "08:00,12:00,18:00"]


MENU_TEXT = "🤖 <b>mekoo-op control</b>\nChoose an action:"


def _fmt_schedule() -> str:
    sched = scheduler.load_schedule()
    rows = [f"• <b>{esc(k)}</b>: {', '.join(sched.get(k, [])) or '(disabled)'}" for k in SCHEDULABLE]
    return "🗓 <b>Schedule (ICT)</b>\n" + "\n".join(rows)


def _fmt_status() -> str:
    hb = scheduler.HEARTBEAT
    alive = "down"
    if hb.exists():
        try:
            age = (scheduler.now_ict() - scheduler.datetime.fromisoformat(
                hb.read_text(encoding="utf-8").strip())).total_seconds()
            alive = f"up ({int(age)}s ago)" if age < 90 else f"stale ({int(age)}s)"
        except (OSError, ValueError):
            pass
    lines = [f"🩺 <b>Scheduler:</b> {alive}", "", _fmt_schedule(), "", "🏃 <b>Last run</b>"]
    for k in RUNNABLE:
        f = scheduler.LOG_DIR / f"{k}.log"
        if f.exists():
            mt = scheduler.datetime.fromtimestamp(f.stat().st_mtime, scheduler.ICT)
            lines.append(f"• {esc(k)}: {mt:%Y-%m-%d %H:%M}")
        else:
            lines.append(f"• {esc(k)}: —")
    return "\n".join(lines)


def _back_menu_kb() -> str:
    return _kb([[("⬅️ Menu", "menu")]])


# --- task execution ----------------------------------------------------------

def _run_and_report(chat_id, task: str, no_send: bool) -> None:
    send(chat_id, f"▶️ Running <b>{esc(task)}</b>{' (dry-run)' if no_send else ''}…")
    try:
        rc = scheduler.run_task(task, block=True, no_send=no_send)
    except Exception as exc:  # noqa: BLE001
        send(chat_id, f"❌ <b>{esc(task)}</b> failed: {esc(f'{type(exc).__name__}: {exc}')}", _back_menu_kb())
        return
    status = "✅" if rc == 0 else f"❌ (rc={rc})"
    tail = scheduler.tail(task, 30)
    body = f"\n<pre>{esc(tail)}</pre>" if tail else ""
    msg = f"{status} <b>{esc(task)}</b> done{body}"
    if len(msg) > _MAXLEN:
        msg = f"{status} <b>{esc(task)}</b> done — open Logs for details."
    send(chat_id, msg, _back_menu_kb())


def _send_log(chat_id, task: str, lines: int = 40) -> None:
    text = scheduler.tail(task, lines)
    if not text:
        send(chat_id, f"📄 <b>{esc(task)}</b>: no log yet.", _back_menu_kb())
        return
    if len(text) <= _MAXLEN:
        send(chat_id, f"📄 <b>{esc(task)}</b> · last {lines} lines\n<pre>{esc(text)}</pre>", _back_menu_kb())
        return
    path = scheduler.LOG_DIR / f"{task}.log"
    with open(path, "rb") as fh:
        requests.post(_API.format(token=env.CONTROL_BOT_TOKEN, method="sendDocument"),
                      data={"chat_id": chat_id, "caption": f"{task}.log"},
                      files={"document": (f"{task}.log", fh, "text/plain")}, timeout=60)
    send(chat_id, "⬆️ Full log sent as a file.", _back_menu_kb())


# --- update handling ---------------------------------------------------------

def on_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    # A pending custom-time reply?
    key = str(chat_id)
    if key in _pending and text and not text.startswith("/"):
        task = _pending.pop(key)
        times = [] if text.lower() in ("off", "none", "disable") else \
            [t for t in text.replace(" ", "").split(",") if t]
        try:
            scheduler.set_times(task, times)
        except ValueError as exc:
            send(chat_id, f"⚠️ {esc(exc)}", _back_menu_kb())
            return
        send(chat_id, "✅ Updated.\n\n" + _fmt_schedule(), _back_menu_kb())
        return

    cmd = text.split("@")[0].split()[0].lower() if text else ""
    if cmd == "/start":
        ok = "✅ authorized" if allowed(chat_id) or not env.CONTROL_ALLOWED else "⛔ not authorized"
        send(chat_id, f"👋 mekoo-op control.\nYour chat id: <code>{chat_id}</code> ({ok})")
    if not env.CONTROL_ALLOWED or allowed(chat_id):
        send(chat_id, MENU_TEXT, _menu_kb())
    elif cmd != "/start":
        send(chat_id, f"⛔ Unauthorized. Your chat id: <code>{chat_id}</code>")


def on_callback(cq: dict) -> None:
    data = cq.get("data", "")
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    cq_id = cq["id"]

    if env.CONTROL_ALLOWED and not allowed(chat_id):
        answer(cq_id, "Unauthorized")
        return
    answer(cq_id)

    # execute a run: rgo:<task>:<0|1>
    if data.startswith("rgo:"):
        _, task, dry = data.split(":")
        edit(chat_id, mid, MENU_TEXT, _menu_kb())
        threading.Thread(target=_run_and_report, args=(chat_id, task, dry == "1"), daemon=True).start()
        return
    # apply preset time: setv:<task>:<val>
    if data.startswith("setv:"):
        _, task, val = data.split(":", 2)
        times = [] if val == "off" else val.split(",")
        try:
            scheduler.set_times(task, times)
            edit(chat_id, mid, "✅ Updated.\n\n" + _fmt_schedule(), _back_menu_kb())
        except ValueError as exc:
            edit(chat_id, mid, f"⚠️ {esc(exc)}", _back_menu_kb())
        return
    # custom time entry: setc:<task>
    if data.startswith("setc:"):
        task = data.split(":", 1)[1]
        _pending[str(chat_id)] = task
        edit(chat_id, mid, f"✏️ Reply with times for <b>{esc(task)}</b>, e.g. "
             f"<code>08:00,15:00</code> (or <code>off</code>).", _back_menu_kb())
        return
    # submenu for a task under Run/Log/Set
    if ":" in data:
        prefix, task = data.split(":", 1)
        if prefix == "runp":
            kb = _kb([[("▶️ Run (send)", f"rcfm:{task}"), ("🧪 Dry-run", f"rgo:{task}:1")],
                      [("⬅️ Back", "run")]])
            edit(chat_id, mid, f"Run <b>{esc(task)}</b>?", kb)
        elif prefix == "rcfm":
            kb = _kb([[("✅ Confirm send", f"rgo:{task}:0"), ("⬅️ Back", "run")]])
            edit(chat_id, mid, f"⚠️ <b>{esc(task)}</b> will run for real and may post to Telegram. Confirm?", kb)
        elif prefix == "logp":
            _send_log(chat_id, task)
            edit(chat_id, mid, MENU_TEXT, _menu_kb())
        elif prefix == "setp":
            rows = [[(p, f"setv:{task}:{p}")] for p in _presets(task)]
            rows.append([("🚫 Off", f"setv:{task}:off"), ("✏️ Custom", f"setc:{task}")])
            rows.append([("⬅️ Back", "set")])
            cur = ", ".join(scheduler.load_schedule().get(task, [])) or "(disabled)"
            edit(chat_id, mid, f"⏰ <b>{esc(task)}</b> — now: {cur}\nPick times:", _kb(rows))
        return
    # top-level
    if data == "menu":
        edit(chat_id, mid, MENU_TEXT, _menu_kb())
    elif data == "status":
        edit(chat_id, mid, _fmt_status(), _back_menu_kb())
    elif data == "sched":
        edit(chat_id, mid, _fmt_schedule(), _back_menu_kb())
    elif data == "run":
        edit(chat_id, mid, "▶️ Run which task?", _task_picker("runp", RUNNABLE))
    elif data == "log":
        edit(chat_id, mid, "📄 Log of which task?", _task_picker("logp", RUNNABLE))
    elif data == "set":
        edit(chat_id, mid, "⏰ Set time for which task?", _task_picker("setp", SCHEDULABLE))


def poll() -> None:
    offset = None
    while True:
        try:
            r = _api("getUpdates", timeout=30, offset=offset)
        except Exception as exc:  # noqa: BLE001
            print(f"getUpdates error: {type(exc).__name__}: {exc}")
            time.sleep(3)
            continue
        if not r.get("ok"):
            print(f"getUpdates not ok: {r}")
            time.sleep(3)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    on_callback(upd["callback_query"])
                elif upd.get("message") and upd["message"].get("text"):
                    on_message(upd["message"])
            except Exception as exc:  # noqa: BLE001
                print(f"handle error: {type(exc).__name__}: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description="mekoo-op Telegram control bot (buttons) + scheduler")
    ap.add_argument("--no-scheduler", dest="scheduler", action="store_false",
                    help="don't run the scheduler in-process (bot only)")
    args = ap.parse_args()

    env.require_control_bot()
    me = _api("getMe")
    who = me.get("result", {}).get("username", "?") if me.get("ok") else "AUTH FAILED"
    allow = ", ".join(env.CONTROL_ALLOWED) if env.CONTROL_ALLOWED else "(none — locked; DM /start)"
    print(f"Control bot @{who} up. Allowed chats: {allow}")

    if args.scheduler:
        threading.Thread(target=scheduler.serve, daemon=True).start()
        print("Scheduler thread started.")

    poll()


if __name__ == "__main__":
    main()
