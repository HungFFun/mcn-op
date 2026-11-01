"""Daily payout recap (yesterday, ID + PH) -> Telegram (recap bot).

Ports the `tevi-daily-recap` skill. One ClickHouse query returns yesterday's
closed-payout stats for both agencies; a message per agency is posted to its
recap group.

Source is `tevi_warehouse.rpt_agency_payout` — a nightly-rebuilt table, so it
needs no grant on raw `tevi_fiat.wd_tx` (which `stg_fiat__withdrawals` reads
and which was revoked). It has no agency_id, so agencies are matched by name.
A freshness guard refuses to send when last night's rebuild has not landed.

    python3 -m tasks.daily_recap              # query + send both
    python3 -m tasks.daily_recap --no-send    # query + print, don't post
    python3 -m tasks.daily_recap --sample     # offline format test
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from common import ch, env, telegram

AGENCIES = {
    env.AGENCY_NAME_ID: {"label": "LUINDO Payout 🇮🇩", "chat": env.CHAT_RECAP_ID, "key": "id"},
    env.AGENCY_NAME_PH: {"label": "PHP Payout 🇵🇭", "chat": env.CHAT_RECAP_PH, "key": "ph"},
}

_NAMES = ", ".join("'" + n.replace("'", "\\'") + "'" for n in AGENCIES)

# The table is rebuilt nightly; it is complete for yesterday only once it holds
# a row updated after today's ICT midnight. known_agencies catches a renamed
# agency, which would otherwise read as a silent "no requests closed".
Q_FRESHNESS = f"""
SELECT
  max(updated_at) >= toStartOfDay(now('Asia/Ho_Chi_Minh')) AS is_fresh,
  toString(max(updated_at)) AS last_updated,
  groupUniqArrayIf(agency_name, agency_name IN ({_NAMES})
    AND created_at >= now() - INTERVAL 30 DAY) AS known_agencies
FROM tevi_warehouse.rpt_agency_payout
"""

Q_RECAP = f"""
SELECT
  agency_name,
  countIf(request_status IN ('Success','Failed')) AS total_closed,
  countIf(request_status = 'Success') AS done_count,
  countIf(request_status = 'Failed') AS fail_count,
  round(sumIf(request_amount_usd, request_status = 'Success'), 2) AS done_usd,
  round(avgIf(dateDiff('second', released_at, closed_at), request_status = 'Success' AND released_at > toDateTime('1970-01-02') AND closed_at > toDateTime('1970-01-02') AND closed_at >= released_at) / 60, 1) AS avg_agency_min,
  round(medianIf(dateDiff('second', released_at, closed_at), request_status = 'Success' AND released_at > toDateTime('1970-01-02') AND closed_at > toDateTime('1970-01-02') AND closed_at >= released_at) / 60, 1) AS p50_agency_min
FROM tevi_warehouse.rpt_agency_payout
WHERE toDate(closed_at, 'Asia/Ho_Chi_Minh') = toDate(now('Asia/Ho_Chi_Minh')) - 1
  AND agency_name IN ({_NAMES})
GROUP BY agency_name
ORDER BY agency_name
"""


def check_freshness() -> None:
    """Abort (non-zero exit, nothing sent) if the source is stale or an agency
    name no longer matches."""
    row = (ch.query(Q_FRESHNESS) or [{}])[0]
    if not int(row.get("is_fresh") or 0):
        raise SystemExit(
            f"rpt_agency_payout is stale (last updated_at {row.get('last_updated')}); "
            "nightly rebuild not landed yet — not sending a partial recap."
        )
    missing = set(AGENCIES) - set(row.get("known_agencies") or [])
    if missing:
        raise SystemExit(
            f"No requests in 30 days for agency name(s) {sorted(missing)} — renamed? "
            "Set AGENCY_NAME_ID / AGENCY_NAME_PH."
        )


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
    {"agency_name": env.AGENCY_NAME_ID, "total_closed": 42,
     "done_count": 39, "fail_count": 3, "done_usd": 5123.44,
     "avg_agency_min": 372.0, "p50_agency_min": 210.0},
    {"agency_name": env.AGENCY_NAME_PH, "total_closed": 18,
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
    if args.sample:
        rows = _SAMPLE
    else:
        check_freshness()
        rows = ch.query(Q_RECAP)
    by_agency = {str(r.get("agency_name")): r for r in rows}

    sent = []
    for agency_name, meta in AGENCIES.items():
        row = by_agency.get(agency_name, {})
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
