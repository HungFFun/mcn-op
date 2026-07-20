"""Daily payout recap (yesterday, ID + PH) -> Telegram (recap bot).

Ports the `tevi-daily-recap` skill. One ClickHouse query returns yesterday's
closed-payout stats for both agencies; a message per agency is posted to its
recap group.

    python3 -m tasks.daily_recap              # query + send both
    python3 -m tasks.daily_recap --no-send    # query + print, don't post
    python3 -m tasks.daily_recap --sample     # offline format test
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from common import ch, env, telegram

AGENCIES = {
    env.AGENCY_ID_ID: {"label": "LUINDO Payout 🇮🇩", "chat": env.CHAT_RECAP_ID, "key": "id"},
    env.AGENCY_ID_PH: {"label": "PHP Payout 🇵🇭", "chat": env.CHAT_RECAP_PH, "key": "ph"},
}

Q_RECAP = f"""
SELECT
  agency_country,
  agency_id,
  countIf(execution_status IN ('completed','failed')) AS total_closed,
  countIf(execution_status = 'completed') AS done_count,
  countIf(execution_status = 'failed') AS fail_count,
  round(sumIf(amount_usd, execution_status = 'completed'), 2) AS done_usd,
  round(avgIf(dateDiff('second', released_at, closed_at), execution_status = 'completed' AND released_at > toDateTime('1970-01-02') AND closed_at > toDateTime('1970-01-02') AND closed_at >= released_at) / 60, 1) AS avg_agency_min,
  round(medianIf(dateDiff('second', released_at, closed_at), execution_status = 'completed' AND released_at > toDateTime('1970-01-02') AND closed_at > toDateTime('1970-01-02') AND closed_at >= released_at) / 60, 1) AS p50_agency_min
FROM tevi_warehouse.stg_fiat__withdrawals
WHERE toDate(closed_at, 'Asia/Ho_Chi_Minh') = toDate(now('Asia/Ho_Chi_Minh')) - 1
  AND agency_id IN ('{env.AGENCY_ID_ID}', '{env.AGENCY_ID_PH}')
GROUP BY agency_country, agency_id
ORDER BY agency_country
"""


def fmt_duration(minutes) -> str:
    try:
        m = float(minutes or 0)
    except (TypeError, ValueError):
        m = 0.0
    if m <= 0:
        return "—"
    total = int(round(m))
    if total < 60:
        return f"{total}m"
    if total < 1440:
        return f"{total // 60}h {total % 60}m"
    return f"{total // 1440}d {(total % 1440) // 60}h"


def format_message(row: dict, label: str, yesterday: str) -> str:
    if not row or int(row.get("total_closed") or 0) == 0:
        return (
            f"📊 <b>Daily Recap — {yesterday}</b>\n"
            f"Agency: {label}\n\n"
            f"No requests closed yesterday."
        )
    done_usd = float(row.get("done_usd") or 0)
    return (
        f"📊 <b>Daily Recap — {yesterday}</b>\n"
        f"Agency: {label}\n\n"
        f"• Total closed: <b>{int(row.get('total_closed') or 0)}</b> requests\n"
        f"• ✅ Completed: <b>{int(row.get('done_count') or 0)}</b> requests "
        f"(≈ {done_usd:,.2f} USDT)\n"
        f"• ❌ Failed: <b>{int(row.get('fail_count') or 0)}</b> requests\n"
        f"• ⏱ Avg agency processing: <b>{fmt_duration(row.get('avg_agency_min'))}</b> "
        f"(median {fmt_duration(row.get('p50_agency_min'))})"
    )


_SAMPLE = [
    {"agency_country": "ID", "agency_id": env.AGENCY_ID_ID, "total_closed": 42,
     "done_count": 39, "fail_count": 3, "done_usd": 5123.44,
     "avg_agency_min": 372.0, "p50_agency_min": 210.0},
    {"agency_country": "PH", "agency_id": env.AGENCY_ID_PH, "total_closed": 18,
     "done_count": 18, "fail_count": 0, "done_usd": 2210.10,
     "avg_agency_min": 45.0, "p50_agency_min": 33.0},
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily payout recap (ID + PH) -> Telegram")
    ap.add_argument("--no-send", dest="send", action="store_false",
                    help="query + print, do not post to Telegram")
    ap.add_argument("--sample", action="store_true",
                    help="skip ClickHouse; render from sample data (offline test)")
    args = ap.parse_args()

    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = _SAMPLE if args.sample else ch.query(Q_RECAP)
    by_agency = {str(r.get("agency_id")): r for r in rows}

    sent = []
    for agency_id, meta in AGENCIES.items():
        row = by_agency.get(agency_id, {})
        msg = format_message(row, meta["label"], yesterday)
        print(f"\n----- {meta['label']} -----\n{msg}")
        if args.sample or not args.send:
            continue
        res = telegram.send_message(meta["chat"], msg)
        sent.append(f"{meta['key']}={res['message_id']}")

    if args.sample:
        print("\n[sample mode] not sent.")
    elif not args.send:
        print("\n[--no-send] not sent.")
    else:
        print(f"\n📤 Sent daily recap via recap bot ({', '.join(sent)}).")


if __name__ == "__main__":
    main()
