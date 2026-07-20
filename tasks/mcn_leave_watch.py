"""MCN Mekoo leave-request watch -> Telegram alert, with dedup.

Ports `mcn-leave-watch`. Queries pending leave requests, buckets each by urgency
(new / departing_soon / overdue), and alerts ONLY when a request is newly seen or
has escalated to a higher bucket. A JSON state file prevents the hourly re-spam
that the original scheduled task produced.

    python3 -m tasks.mcn_leave_watch              # query + alert if needed
    python3 -m tasks.mcn_leave_watch --no-send    # query + print, don't post
    python3 -m tasks.mcn_leave_watch --reset      # clear the dedup state file
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from common import ch, env, telegram

MCN = env.MEKOO_MCN_ID
STATE_FILE = env.STATE_DIR / "mcn-leave-watch-seen.json"

BUCKET_RANK = {"new": 1, "departing_soon": 2, "overdue": 3}

Q_PENDING = f"""
WITH '{MCN}' AS mekoo_id,
mekoo_slugs AS (
  SELECT user_alias, argMax(channel_slug, org_joined_at) AS channel_slug
  FROM tevi_warehouse.dim_users WHERE org_id = mekoo_id GROUP BY user_alias
)
SELECT
  lr.leave_request_id AS leave_request_id,
  toUInt64(lr.requester_alias) AS tevi_id,
  lr.requester_channel_name AS channel,
  coalesce(m.channel_slug, '') AS channel_slug,
  concat('https://tevi.com/@', coalesce(m.channel_slug, '')) AS channel_url,
  toUnixTimestamp(lr.auto_proceed_at) - toUnixTimestamp(now('Asia/Ho_Chi_Minh')) AS seconds_to_departure,
  lr.is_overdue AS is_overdue,
  toUnixTimestamp(now('Asia/Ho_Chi_Minh')) - toUnixTimestamp(lr.created_at) AS seconds_since_created
FROM tevi_warehouse.fct_mcn_leave_requests lr
LEFT JOIN mekoo_slugs m ON m.user_alias = toUInt64(lr.requester_alias)
WHERE lr.mcn_id = mekoo_id AND lr.status = 'pending'
ORDER BY lr.created_at DESC
"""


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            STATE_FILE.rename(STATE_FILE.with_suffix(".json.bak"))
    return {"alerted": {}, "last_updated": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def bucket_of(row: dict) -> str | None:
    if int(row.get("is_overdue") or 0) == 1:
        return "overdue"
    std = int(row.get("seconds_to_departure") or 0)
    if 0 < std <= 86400:
        return "departing_soon"
    if int(row.get("seconds_since_created") or 0) <= 3900:
        return "new"
    return None


def _line(row: dict) -> str:
    url = row.get("channel_url") or ""
    chan = row.get("channel") or "(no name)"
    return f"• <a href=\"{url}\"><b>{chan}</b></a> ({row.get('tevi_id')})"


def build_alert(to_alert: list[dict], now_ict: datetime) -> str:
    buckets = {"overdue": [], "departing_soon": [], "new": []}
    for r in to_alert:
        buckets[r["bucket"]].append(r)

    parts = [f"🚪 <b>MCN Mekoo · Leave Request Alert — {now_ict.strftime('%d/%m/%Y %H:%M')} (ICT)</b>\n"]
    if buckets["overdue"]:
        parts.append(f"⚠️ <b>Overdue — need action NOW:</b> {len(buckets['overdue'])}")
        parts += [_line(r) for r in buckets["overdue"]]
        parts.append("")
    if buckets["departing_soon"]:
        parts.append(f"⏰ <b>Auto-departing within 24h:</b> {len(buckets['departing_soon'])}")
        parts += [_line(r) for r in buckets["departing_soon"]]
        parts.append("")
    if buckets["new"]:
        parts.append(f"🆕 <b>New pending requests:</b> {len(buckets['new'])}")
        parts += [_line(r) for r in buckets["new"]]
        parts.append("")
    parts.append(env.LEAVE_WATCH_CC)
    return "\n".join(parts)


def evaluate(rows: list[dict], state: dict) -> list[dict]:
    """Return the rows that should be alerted (new or escalated)."""
    to_alert = []
    for row in rows:
        bucket = bucket_of(row)
        if bucket is None:
            continue
        rid = str(row["leave_request_id"])
        prev_rank = BUCKET_RANK.get(state["alerted"].get(rid), 0)
        if BUCKET_RANK[bucket] > prev_rank:
            row = dict(row, bucket=bucket)
            to_alert.append(row)
    return to_alert


def main() -> None:
    ap = argparse.ArgumentParser(description="MCN Mekoo leave-request watch (deduped)")
    ap.add_argument("--no-send", dest="send", action="store_false",
                    help="evaluate + print, do not post to Telegram")
    ap.add_argument("--reset", action="store_true", help="clear the dedup state file and exit")
    args = ap.parse_args()

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print(f"Cleared {STATE_FILE}")
        else:
            print("No state file to clear.")
        return

    now_ict = datetime.now(timezone(timedelta(hours=7)))
    rows = ch.query(Q_PENDING)
    state = load_state()
    to_alert = evaluate(rows, state)

    total = len(rows)
    if not to_alert:
        print(f"✅ No new escalations — {total} pending, all seen already.")
        return

    msg = build_alert(to_alert, now_ict)
    print(msg)
    if not args.send:
        print(f"\n[--no-send] {len(to_alert)} would be alerted (of {total} pending).")
        return

    res = telegram.send_message(env.CHAT_MEKOO, msg, disable_preview=True)

    # Persist ONLY after a successful send, and prune IDs no longer pending.
    for r in to_alert:
        state["alerted"][str(r["leave_request_id"])] = r["bucket"]
    current = {str(r["leave_request_id"]) for r in rows}
    state["alerted"] = {k: v for k, v in state["alerted"].items() if k in current}
    state["last_updated"] = datetime.now(timezone.utc).astimezone().isoformat()
    save_state(state)

    counts = {b: sum(1 for r in to_alert if r["bucket"] == b) for b in BUCKET_RANK}
    print(f"\n📤 Alert sent (msg_id={res['message_id']}). "
          f"overdue={counts['overdue']}, departing_soon={counts['departing_soon']}, "
          f"new={counts['new']}. Total pending={total}.")


if __name__ == "__main__":
    main()
