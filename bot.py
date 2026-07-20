"""Telegram control bot for the ops tasks — the main "run the project" entry.

Start it once and leave it running (Windows: `scripts\\run.bat bot`). It:
  - runs the daily scheduler in a background thread (fires tasks on their times),
  - drives everything from a persistent reply keyboard above the input (no typing).

Send /start once to install the keyboard: Status, Schedule, Run, Set time.
Tapping a button opens that view; picking a task, confirming a real run, or
choosing preset/custom times uses inline buttons on that message. Status already
reports each task's last-run time, so there is no separate Logs button.

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

SCHEDULABLE = scheduler.SCHEDULABLE          # payout, mcn-report, daily-recap, leave-watch
RUNNABLE = list(scheduler.TASKS)             # + check
_pending: dict[str, str] = {}                # chat_id -> task awaiting a custom time reply

# Pretty display names for the task slugs (used in messages; slug stays the key).
LABELS = {
    "payout": "💸 Payout Batch",
    "mcn-report": "📊 MCN Daily Report",
    "daily-recap": "🧾 Daily Payout Recap",
    "leave-watch": "🚪 MCN Leave-Watch",
    "check": "🩺 Config Check",
}


def _label(task: str) -> str:
    return LABELS.get(task, task)


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

# Persistent reply keyboard: sits above the input box, always visible, one tap
# sends the button label as a message. Installed once on /start; on_message maps
# each label back to the matching top-level view (drill-down stays inline).
def _reply_kb() -> str:
    return json.dumps({
        "keyboard": [
            [{"text": "📊 Status"}, {"text": "🗓 Schedule"}],
            [{"text": "▶️ Run"}, {"text": "⏰ Set time"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }, ensure_ascii=False)


def _task_picker(prefix: str, tasks: list[str]) -> str:
    return _kb([[(_label(t), f"{prefix}:{t}")] for t in tasks])


def _presets(task: str) -> list[str]:
    if task == "payout":
        return ["08:00,11:00,15:00,18:00,22:00", "08:00,15:00", "08:00"]
    return ["08:00", "08:00,15:00", "08:00,12:00,18:00"]


def _fmt_schedule() -> str:
    sched = scheduler.load_schedule()
    rows = [f"• <b>{esc(_label(k))}</b>: {', '.join(sched.get(k, [])) or '(đã tắt)'}" for k in SCHEDULABLE]
    return "🗓 <b>Lịch chạy (ICT)</b>\n" + "\n".join(rows)


def _fmt_status() -> str:
    hb = scheduler.HEARTBEAT
    alive = "🔴 dừng"
    if hb.exists():
        try:
            age = (scheduler.now_ict() - scheduler.datetime.fromisoformat(
                hb.read_text(encoding="utf-8").strip())).total_seconds()
            alive = f"🟢 chạy ({int(age)}s trước)" if age < 90 else f"🟡 cũ ({int(age)}s)"
        except (OSError, ValueError):
            pass
    lines = [f"🩺 <b>Scheduler:</b> {alive}", "", _fmt_schedule(), "", "🏃 <b>Lần chạy gần nhất</b>"]
    for k in RUNNABLE:
        f = scheduler.LOG_DIR / f"{k}.log"
        if f.exists():
            mt = scheduler.datetime.fromtimestamp(f.stat().st_mtime, scheduler.ICT)
            lines.append(f"• {esc(_label(k))}: {mt:%Y-%m-%d %H:%M}")
        else:
            lines.append(f"• {esc(_label(k))}: —")
    return "\n".join(lines)


# --- task execution ----------------------------------------------------------

def _run_and_report(chat_id, task: str, no_send: bool) -> None:
    """Run the task and report only a one-line outcome — no detail dump. The
    task posts its own report to the target group; the control chat just needs
    to know it finished."""
    send(chat_id, f"▶️ Đang chạy <b>{esc(_label(task))}</b>{' (chạy thử)' if no_send else ''}…")
    try:
        rc = scheduler.run_task(task, block=True, no_send=no_send)
    except Exception as exc:  # noqa: BLE001
        send(chat_id, f"❌ <b>{esc(_label(task))}</b> lỗi: {esc(f'{type(exc).__name__}: {exc}')}")
        return
    if rc == 0:
        send(chat_id, f"✅ <b>{esc(_label(task))}</b> đã chạy xong.")
    else:
        send(chat_id, f"❌ <b>{esc(_label(task))}</b> lỗi (rc={rc}).")


# --- update handling ---------------------------------------------------------

# Reply-keyboard labels → the top-level view to open (inline drill-down follows).
_REPLY_ACTIONS = {
    "📊 Status": lambda cid: send(cid, _fmt_status()),
    "🗓 Schedule": lambda cid: send(cid, _fmt_schedule()),
    "▶️ Run": lambda cid: send(cid, "▶️ Chạy task nào?", _task_picker("runp", RUNNABLE)),
    "⏰ Set time": lambda cid: send(cid, "⏰ Đặt giờ cho task nào?", _task_picker("setp", SCHEDULABLE)),
}


def on_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    # A pending custom-time reply? (reply-keyboard taps are not time values.)
    key = str(chat_id)
    if key in _pending and text and not text.startswith("/") and text not in _REPLY_ACTIONS:
        task = _pending.pop(key)
        times = [] if text.lower() in ("off", "none", "disable") else \
            [t for t in text.replace(" ", "").split(",") if t]
        try:
            scheduler.set_times(task, times)
        except ValueError as exc:
            send(chat_id, f"⚠️ {esc(exc)}")
            return
        send(chat_id, "✅ Updated.\n\n" + _fmt_schedule())
        return

    authorized = (not env.CONTROL_ALLOWED) or allowed(chat_id)
    cmd = text.split("@")[0].split()[0].lower() if text else ""

    if cmd == "/start":
        ok = "✅ authorized" if authorized else "⛔ not authorized"
        # Install the persistent reply keyboard alongside the welcome line.
        send(chat_id, f"👋 mekoo-op control.\nYour chat id: <code>{chat_id}</code> ({ok})",
             _reply_kb() if authorized else None)
        return

    if not authorized:
        send(chat_id, f"⛔ Unauthorized. Your chat id: <code>{chat_id}</code>")
        return

    # A reply-keyboard button tap opens its view directly.
    action = _REPLY_ACTIONS.get(text)
    if action:
        action(chat_id)
        return

    # Anything else → nudge to the keyboard (and re-install it if dismissed).
    send(chat_id, "👇 Chọn một mục từ bàn phím bên dưới.", _reply_kb())


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

    # apply preset time: setv:<task>:<val>
    if data.startswith("setv:"):
        _, task, val = data.split(":", 2)
        times = [] if val == "off" else val.split(",")
        try:
            scheduler.set_times(task, times)
            edit(chat_id, mid, "✅ Đã cập nhật.\n\n" + _fmt_schedule())
        except ValueError as exc:
            edit(chat_id, mid, f"⚠️ {esc(exc)}")
        return
    # custom time entry: setc:<task>
    if data.startswith("setc:"):
        task = data.split(":", 1)[1]
        _pending[str(chat_id)] = task
        edit(chat_id, mid, f"✏️ Nhập giờ cho <b>{esc(_label(task))}</b>, ví dụ "
             f"<code>08:00,15:00</code> (hoặc <code>off</code> để tắt).")
        return
    # submenu for a task under Run/Set
    if ":" in data:
        prefix, task = data.split(":", 1)
        if prefix == "runp":
            # Pick a task → run it immediately. No task moves money directly
            # (they only post a report/PDF), so there is no confirm step.
            edit(chat_id, mid, f"▶️ Đã đưa <b>{esc(_label(task))}</b> vào hàng đợi…")
            threading.Thread(target=_run_and_report, args=(chat_id, task, False), daemon=True).start()
        elif prefix == "setp":
            rows = [[(p, f"setv:{task}:{p}")] for p in _presets(task)]
            rows.append([("🚫 Tắt", f"setv:{task}:off"), ("✏️ Tùy chỉnh", f"setc:{task}")])
            rows.append([("⬅️ Quay lại", "set")])
            cur = ", ".join(scheduler.load_schedule().get(task, [])) or "(đã tắt)"
            edit(chat_id, mid, f"⏰ <b>{esc(_label(task))}</b> — hiện tại: {cur}\nChọn giờ:", _kb(rows))
        return
    # top-level (reachable via the "⬅️ Quay lại" buttons inside the task flows)
    if data == "run":
        edit(chat_id, mid, "▶️ Chạy task nào?", _task_picker("runp", RUNNABLE))
    elif data == "set":
        edit(chat_id, mid, "⏰ Đặt giờ cho task nào?", _task_picker("setp", SCHEDULABLE))


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
