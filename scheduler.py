"""In-process daily scheduler — runs the ops tasks on a clock, no external cron.

Start it once and leave it running (it blocks):

    python3 -m scheduler                # serve: fire tasks at their times, forever
    python3 -m scheduler --list         # print the schedule and exit
    python3 -m scheduler --once payout  # run one task now (test), then exit
    python3 -m scheduler --dry-run 08:00  # print what WOULD fire at a given time

Schedule (all times are **Asia/Ho_Chi_Minh / ICT**, so it fires at 8:00 Vietnam
time regardless of the server timezone):

    08:00                      mcn-report, daily-recap, leave-watch
    08:00, 11:00, 15:00,
    18:00, 22:00               payout (ID + PH)

Each fire launches a subprocess (`python -m <module>`), with stdout+stderr
appended to run/logs/<task>.log. Tasks run concurrently if they share a slot
(e.g. everything at 08:00). Missed fires while the machine/process was down are
NOT back-run — same "skip if down" semantics as cron.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ICT = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover - zoneinfo missing/no tzdata
    ICT = timezone(timedelta(hours=7))

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get("MEKOO_LOG_DIR", PROJECT_ROOT / "run" / "logs"))
# Touched each loop tick so the web UI can tell whether the scheduler is alive.
HEARTBEAT = Path(os.environ.get("MEKOO_HEARTBEAT", PROJECT_ROOT / "run" / "scheduler.heartbeat"))

# Task spec: key -> (module, base args). One source of truth; TASKS is the
# concrete `python -m` argv derived from it, task_cmd() is frozen-aware.
TASK_SPECS: dict[str, tuple[str, list[str]]] = {
    "payout": ("payout.run", ["--market", "both"]),
    "mcn-report": ("tasks.mcn_daily_report", []),
    "daily-recap": ("tasks.daily_recap", []),
    "leave-watch": ("tasks.mcn_leave_watch", []),
    "check": ("common.check", []),   # not scheduled; runnable from the UI
}
TASKS: dict[str, list[str]] = {
    key: [sys.executable, "-m", mod, *args] for key, (mod, args) in TASK_SPECS.items()
}
# Tasks that accept --no-send (everything except the config check).
NO_SEND = {"payout", "mcn-report", "daily-recap", "leave-watch"}


def task_cmd(key: str, no_send: bool = False) -> list[str]:
    """argv to launch a task as a subprocess (`python -m module ...`)."""
    extra = ["--no-send"] if (no_send and key in NO_SEND) else []
    mod, args = TASK_SPECS[key]
    return [sys.executable, "-m", mod, *args, *extra]


# --- Schedule (persisted to a JSON file the Telegram bot can edit) -----------
# {task: ["HH:MM", ...]} in ICT. Editing the file / via set_times() takes effect
# on the next tick — no restart needed. Only these tasks are schedulable.
SCHEDULABLE = ["payout", "mcn-report", "daily-recap", "leave-watch"]
DEFAULT_SCHEDULE: dict[str, list[str]] = {
    "payout": ["08:00", "11:00", "15:00", "18:00", "22:00"],
    "mcn-report": ["08:00"],
    "daily-recap": ["08:00"],
    "leave-watch": ["08:00"],
}
SCHEDULE_FILE = Path(os.environ.get("MEKOO_SCHEDULE", PROJECT_ROOT / "run" / "schedule.json"))

_POLL_SECONDS = 20  # tick well within the target minute so we never miss it
_TIME_RE = "%H:%M"


def now_ict() -> datetime:
    return datetime.now(ICT)


def _hhmm(when: datetime) -> str:
    return f"{when.hour:02d}:{when.minute:02d}"


def valid_time(s: str) -> bool:
    try:
        datetime.strptime(s.strip(), _TIME_RE)
        return True
    except ValueError:
        return False


def load_schedule() -> dict[str, list[str]]:
    """Current schedule from the file, or the built-in default. Keeps only known
    schedulable tasks and valid HH:MM entries."""
    if SCHEDULE_FILE.exists():
        try:
            data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
            return {k: sorted({t for t in v if valid_time(t)})
                    for k, v in data.items() if k in SCHEDULABLE}
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return {k: list(v) for k, v in DEFAULT_SCHEDULE.items()}


def save_schedule(sched: dict[str, list[str]]) -> None:
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sched, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SCHEDULE_FILE)


def set_times(task: str, times: list[str]) -> dict[str, list[str]]:
    """Set a task's fire times (empty list = disable). Returns the new schedule."""
    if task not in SCHEDULABLE:
        raise ValueError(f"unknown task '{task}' (schedulable: {', '.join(SCHEDULABLE)})")
    bad = [t for t in times if not valid_time(t)]
    if bad:
        raise ValueError(f"bad time(s): {', '.join(bad)} (expected HH:MM)")
    sched = load_schedule()
    if times:
        sched[task] = sorted({t.strip() for t in times})
    else:
        sched.pop(task, None)
    save_schedule(sched)
    return sched


def due(when: datetime, sched: dict[str, list[str]] | None = None) -> list[str]:
    """Task keys scheduled to fire at `when` (HH:MM match, ICT)."""
    sched = load_schedule() if sched is None else sched
    hm = _hhmm(when)
    return [task for task, times in sched.items() if hm in times]


def tail(key: str, lines: int = 40) -> str:
    """Last N lines of a task's log (empty string if none)."""
    f = LOG_DIR / f"{key}.log"
    if not f.exists():
        return ""
    return "\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def run_task(key: str, block: bool = False, no_send: bool = False) -> subprocess.Popen | int:
    """Launch a task as a subprocess, appending output to its log file."""
    cmd = task_cmd(key, no_send)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"{key}.log"
    stamp = now_ict().strftime("%Y-%m-%d %H:%M:%S %Z")
    fh = open(logfile, "a", encoding="utf-8")
    fh.write(f"\n===== {stamp} — fire {key}: {' '.join(cmd[1:])} =====\n")
    fh.flush()
    print(f"[{stamp}] fire {key} → {logfile}")
    if block:
        rc = subprocess.run(cmd, cwd=PROJECT_ROOT, stdout=fh, stderr=subprocess.STDOUT).returncode
        fh.close()
        return rc
    return subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=fh, stderr=subprocess.STDOUT)


def _beat() -> None:
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(now_ict().isoformat(), encoding="utf-8")
    except OSError:
        pass


def serve() -> None:
    print(f"Scheduler up (ICT). Logs → {LOG_DIR} · schedule → {SCHEDULE_FILE}")
    _print_schedule(load_schedule())
    fired: set[tuple] = set()
    while True:
        n = now_ict()
        _beat()
        today = n.date().isoformat()
        for key in due(n):   # re-reads the file each tick → bot edits apply live
            tag = (today, _hhmm(n), key)
            if tag not in fired:
                fired.add(tag)
                try:
                    run_task(key)
                except Exception as exc:  # noqa: BLE001 — never let one task kill the loop
                    print(f"[{n:%H:%M}] ERROR launching {key}: {type(exc).__name__}: {exc}")
        # drop tags from earlier days so the set stays small
        fired = {t for t in fired if t[0] == today}
        time.sleep(_POLL_SECONDS)


def _print_schedule(sched: dict[str, list[str]]) -> None:
    for key in SCHEDULABLE:
        times = ", ".join(sched.get(key, [])) or "(disabled)"
        print(f"  {key:12} {times}")


def main() -> None:
    ap = argparse.ArgumentParser(description="mekoo-op in-process daily scheduler (ICT)")
    ap.add_argument("--list", action="store_true", help="print the schedule and exit")
    ap.add_argument("--once", metavar="TASK", choices=list(TASKS),
                    help="run one task now (blocking), then exit")
    ap.add_argument("--dry-run", metavar="HH:MM",
                    help="print which tasks would fire at HH:MM (ICT), then exit")
    args = ap.parse_args()

    if args.list:
        print(f"Schedule (ICT) — {SCHEDULE_FILE}:")
        _print_schedule(load_schedule())
        return
    if args.once:
        rc = run_task(args.once, block=True)
        sys.exit(rc if isinstance(rc, int) else 0)
    if args.dry_run:
        hh, _, mm = args.dry_run.partition(":")
        when = now_ict().replace(hour=int(hh), minute=int(mm or 0))
        keys = due(when)
        print(f"At {args.dry_run} ICT would fire: {', '.join(keys) if keys else '(nothing)'}")
        return

    serve()


if __name__ == "__main__":
    main()
