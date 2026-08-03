"""MCN Mekoo daily health report -> Telegram (recap bot).

Ports the `mekoo-mcn-daily-report` skill. Runs 4 ClickHouse queries over
tevi_warehouse (yesterday + MTD-through-yesterday, Asia/Ho_Chi_Minh), formats a
single HTML message, and posts it to the Mekoo group via the recap bot.

    python3 -m tasks.mcn_daily_report              # query + send
    python3 -m tasks.mcn_daily_report --no-send    # query + print, don't send
    python3 -m tasks.mcn_daily_report --sample     # offline format test
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common import ch, env, telegram

MCN = env.MEKOO_MCN_ID

ICT = ZoneInfo("Asia/Ho_Chi_Minh")

# Inviter aliases -> display label (configured in common/env.py).
INDO_ALIAS = env.MCN_ALIAS_INDO   # @banned_tv1
CS_ALIAS = env.MCN_ALIAS_CS       # @Femirastar
BOT_ALIAS = env.MCN_ALIAS_BOT     # Auto Invite Bot


def ict_window() -> dict[str, str]:
    """The report's date window, computed ONCE in Python (ICT).

    All four queries share these literals instead of each calling
    now('Asia/Ho_Chi_Minh') independently — four separate ClickHouse
    round-trips that could evaluate now() on either side of an ICT midnight
    and disagree by a day (the 2026-07-22 Query-D drift). Windows are
    yesterday and MTD-through-yesterday (mtd_end exclusive).
    """
    today = datetime.now(ICT).date()
    return {
        "today": today.isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
        "mtd_start": today.replace(day=1).isoformat(),
        "mtd_end": today.isoformat(),  # exclusive
    }


# --- Queries (verbatim from the skill; MCN id + ICT window literals inlined) --
# Every window boundary is a toDate('YYYY-MM-DD') literal from ict_window(),
# never now('Asia/Ho_Chi_Minh'), so the four queries can never straddle midnight.

def q_members(w: dict[str, str]) -> str:
    return f"""
WITH '{MCN}' AS mekoo_id,
  toDate('{w["mtd_start"]}') AS cur_start,
  toDate('{w["mtd_end"]}') AS cur_end
SELECT
  (SELECT count() FROM (SELECT user_alias FROM tevi_warehouse.dim_users WHERE org_id = mekoo_id GROUP BY user_alias)) AS total_members,
  (SELECT max(active_creator_count) FROM tevi_warehouse.rpt_mcn_performance FINAL WHERE mcn_id = mekoo_id AND date_key >= cur_start AND date_key < cur_end) AS peak_active_mtd
"""


def q_status(w: dict[str, str]) -> str:
    return f"""
WITH '{MCN}' AS mekoo_id,
mekoo_in_org AS (
  SELECT user_alias, max(user_suspended_at) AS user_suspended_at
  FROM tevi_warehouse.dim_users WHERE org_id = mekoo_id GROUP BY user_alias
),
mekoo_creators AS (
  SELECT user_alias, max(is_active_30d) AS is_active_30d
  FROM tevi_warehouse.dim_creators WHERE org_name = 'MEKOO MCN' GROUP BY user_alias
),
last_mekoo_rev AS (
  SELECT user_alias, max(date_key) AS last_rev_date
  FROM tevi_warehouse.fct_creator_revenue WHERE mcn_id = mekoo_id GROUP BY user_alias
)
SELECT
  (SELECT countIf(user_suspended_at > toDateTime('1970-01-01')) FROM mekoo_in_org) AS banned_total,
  (SELECT countIf(u.user_suspended_at = toDateTime('1970-01-01') AND coalesce(c.is_active_30d, 0) = 0)
   FROM mekoo_in_org u LEFT JOIN mekoo_creators c ON c.user_alias = u.user_alias) AS dormant,
  (SELECT count() FROM last_mekoo_rev WHERE user_alias NOT IN (SELECT user_alias FROM mekoo_in_org)) AS left_total,
  (SELECT countIf(last_rev_date >= toDate('{w["mtd_start"]}') AND last_rev_date < toDate('{w["mtd_end"]}')) FROM last_mekoo_rev WHERE user_alias NOT IN (SELECT user_alias FROM mekoo_in_org)) AS left_mtd
"""


# Verification uses fct_tick_space_verifications (TEV-1105, from 2026-07-07).
# The old fct_channel_verifications stalled 2026-05-27 across all Tevi — do NOT
# use it. outcome = approved / rejected / pending, with a decided_at timestamp.
# Caveat: the new model does not track revokes done outside the application flow.
def q_kyc(w: dict[str, str]) -> str:
    return f"""
WITH '{MCN}' AS mekoo_id,
  toDate('{w["mtd_start"]}') AS cur_start,
  toDate('{w["mtd_end"]}') AS cur_end,
mekoo_users AS (SELECT DISTINCT user_alias FROM tevi_warehouse.dim_users WHERE org_id = mekoo_id),
kyc_per_user AS (
  SELECT user_alias, argMax(status, submitted_at) AS latest_status
  FROM tevi_warehouse.fct_kyc_submissions
  WHERE user_alias IN (SELECT user_alias FROM mekoo_users)
  GROUP BY user_alias
),
tick_latest AS (
  SELECT toUInt64(user_alias) AS user_alias,
    argMax(outcome, coalesce(decided_at, submitted_at)) AS latest_outcome
  FROM tevi_warehouse.fct_tick_space_verifications
  WHERE toUInt64(user_alias) IN (SELECT user_alias FROM mekoo_users)
  GROUP BY user_alias
)
SELECT
  (SELECT countIf(latest_status = 'approved') FROM kyc_per_user) AS kyc_approved,
  (SELECT countIf(latest_outcome = 'approved') FROM tick_latest) AS verified_total,
  (SELECT uniqExact(toUInt64(user_alias))
   FROM tevi_warehouse.fct_tick_space_verifications
   WHERE toUInt64(user_alias) IN (SELECT user_alias FROM mekoo_users)
     AND outcome = 'approved'
     AND toDate(decided_at) >= cur_start AND toDate(decided_at) < cur_end
  ) AS granted_mtd,
  (SELECT uniqExact(toUInt64(user_alias))
   FROM tevi_warehouse.fct_tick_space_verifications
   WHERE toUInt64(user_alias) IN (SELECT user_alias FROM mekoo_users)
     AND outcome = 'pending'
  ) AS pending_applications
"""


# Reports two funnel stages per inviter:
#   Sent     = invitation rows created in the window (by created_at).
#   Accepted = first-accepted attribution — each invitee counted once, credited
#              to their EARLIEST accepted inviter (argMin), matching the admin panel.
def q_invites(w: dict[str, str]) -> str:
    return f"""
WITH '{MCN}' AS mekoo_id,
  toDate('{w["yesterday"]}') AS yesterday,
  toDate('{w["mtd_start"]}') AS mtd_start,
  toDate('{w["mtd_end"]}') AS mtd_end,
joined_yest AS (
  SELECT user_alias FROM tevi_warehouse.dim_users
  WHERE org_id = mekoo_id AND toDate(org_joined_at) = yesterday
),
joined_mtd AS (
  SELECT user_alias FROM tevi_warehouse.dim_users
  WHERE org_id = mekoo_id AND toDate(org_joined_at) >= mtd_start AND toDate(org_joined_at) < mtd_end
),
attrib_yest AS (
  SELECT invitee_alias, argMin(inviter_alias, updated_at) AS latest_inviter
  FROM tevi_warehouse.fct_mcn_invitations
  WHERE mcn_id = mekoo_id AND status = 'accepted'
    AND inviter_alias IN ({CS_ALIAS}, {INDO_ALIAS}, {BOT_ALIAS})
    AND invitee_alias IN (SELECT user_alias FROM joined_yest)
  GROUP BY invitee_alias
),
attrib_mtd AS (
  SELECT invitee_alias, argMin(inviter_alias, updated_at) AS latest_inviter
  FROM tevi_warehouse.fct_mcn_invitations
  WHERE mcn_id = mekoo_id AND status = 'accepted'
    AND inviter_alias IN ({CS_ALIAS}, {INDO_ALIAS}, {BOT_ALIAS})
    AND invitee_alias IN (SELECT user_alias FROM joined_mtd)
  GROUP BY invitee_alias
),
sent_counts AS (
  SELECT
    inviter_alias,
    countIf(toDate(created_at) = yesterday) AS sent_yest,
    countIf(toDate(created_at) >= mtd_start AND toDate(created_at) < mtd_end) AS sent_mtd
  FROM tevi_warehouse.fct_mcn_invitations
  WHERE mcn_id = mekoo_id AND inviter_alias IN ({CS_ALIAS}, {INDO_ALIAS}, {BOT_ALIAS})
  GROUP BY inviter_alias
)
SELECT
  (SELECT countIf(latest_inviter = {INDO_ALIAS}) FROM attrib_yest) AS indo_yest,
  (SELECT countIf(latest_inviter = {CS_ALIAS}) FROM attrib_yest) AS cs_yest,
  (SELECT countIf(latest_inviter = {BOT_ALIAS}) FROM attrib_yest) AS bot_yest,
  (SELECT count() FROM attrib_yest) AS total_yest,
  (SELECT countIf(latest_inviter = {INDO_ALIAS}) FROM attrib_mtd) AS indo_mtd,
  (SELECT countIf(latest_inviter = {CS_ALIAS}) FROM attrib_mtd) AS cs_mtd,
  (SELECT countIf(latest_inviter = {BOT_ALIAS}) FROM attrib_mtd) AS bot_mtd,
  (SELECT count() FROM attrib_mtd) AS total_mtd,
  (SELECT sent_yest FROM sent_counts WHERE inviter_alias = {INDO_ALIAS}) AS indo_sent_yest,
  (SELECT sent_yest FROM sent_counts WHERE inviter_alias = {CS_ALIAS}) AS cs_sent_yest,
  (SELECT sent_yest FROM sent_counts WHERE inviter_alias = {BOT_ALIAS}) AS bot_sent_yest,
  (SELECT sent_mtd FROM sent_counts WHERE inviter_alias = {INDO_ALIAS}) AS indo_sent_mtd,
  (SELECT sent_mtd FROM sent_counts WHERE inviter_alias = {CS_ALIAS}) AS cs_sent_mtd,
  (SELECT sent_mtd FROM sent_counts WHERE inviter_alias = {BOT_ALIAS}) AS bot_sent_mtd,
  (SELECT sum(sent_yest) FROM sent_counts) AS total_sent_yest,
  (SELECT sum(sent_mtd)  FROM sent_counts) AS total_sent_mtd
"""


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fetch(w: dict[str, str]) -> dict:
    d = {}
    d.update(ch.query_one(q_members(w)))
    d.update(ch.query_one(q_status(w)))
    d.update(ch.query_one(q_kyc(w)))
    d.update(ch.query_one(q_invites(w)))
    return {k: _i(v) for k, v in d.items()}


def check_invites_sane(d: dict) -> str | None:
    """Guardrail against the Query-D drift: no daily invitation activity at all
    while MTD sent is non-zero almost certainly means yesterday's window was
    lost. Returns a reason string when the data looks wrong, else None.
    """
    daily = _i(d.get("total_sent_yest")) + _i(d.get("total_yest"))
    mtd_sent = (_i(d.get("indo_sent_mtd")) + _i(d.get("cs_sent_mtd"))
                + _i(d.get("bot_sent_mtd")))
    if daily == 0 and mtd_sent > 0:
        return (f"daily invitations all zero (sent+accepted=0) but MTD sent="
                f"{mtd_sent} — yesterday's window was likely lost")
    return None


def format_message(d: dict, report_date: str) -> str:
    n = lambda k: f"{_i(d.get(k)):,}"  # noqa: E731
    i = lambda k: _i(d.get(k))         # noqa: E731

    def inviter(label: str, sy, sm, ay, am) -> str:
        return (
            f"• <b>{label}</b>\n"
            f"   ↗ Sent:     <b>{i(sy)} · {i(sm)}</b>\n"
            f"   ✅ Accepted: <b>{i(ay)} · {i(am)}</b>\n"
        )

    return (
        f"📊 <b>MCN Mekoo · Daily Report — {report_date} (ICT)</b>\n\n"
        f"👥 <b>Creator base · Mekoo MCN</b>\n"
        f"• Active members: <b>{n('total_members')}</b>\n"
        f"• Active peak: <b>{n('peak_active_mtd')}</b>\n"
        f"• Banned: <b>{n('banned_total')}</b>\n"
        f"• Dormant (no rev 30d): <b>{n('dormant')}</b>\n"
        f"• Left MCN: <b>{n('left_mtd')}</b> · Total ever left: {n('left_total')}\n\n"
        f"✅ <b>KYC &amp; Verification</b>\n"
        f"• KYC approved: <b>{n('kyc_approved')}</b>\n"
        f"• Verified tick: <b>{n('verified_total')}</b>\n"
        f"• Tick granted (MTD): <b>{n('granted_mtd')}</b>\n"
        f"• Tick pending applications: <b>{n('pending_applications')}</b>\n\n"
        f"🎯 <b>Invitations</b>  (daily · MTD)\n\n"
        + inviter("@banned_tv1", "indo_sent_yest", "indo_sent_mtd", "indo_yest", "indo_mtd") + "\n"
        + inviter("@Femirastar", "cs_sent_yest", "cs_sent_mtd", "cs_yest", "cs_mtd") + "\n"
        + inviter("Auto Invite Bot (2310953262)", "bot_sent_yest", "bot_sent_mtd", "bot_yest", "bot_mtd") + "\n"
        + inviter("Total", "total_sent_yest", "total_sent_mtd", "total_yest", "total_mtd").rstrip()
    )


_SAMPLE = {
    "total_members": 5496, "peak_active_mtd": 1111, "banned_total": 1875,
    "dormant": 2529, "left_mtd": 2, "left_total": 550,
    "kyc_approved": 1708, "verified_total": 2088, "granted_mtd": 18,
    "pending_applications": 7,
    "indo_yest": 25, "cs_yest": 0, "bot_yest": 17, "total_yest": 43,
    "indo_mtd": 174, "cs_mtd": 20, "bot_mtd": 99, "total_mtd": 373,
    "indo_sent_yest": 30, "cs_sent_yest": 4, "bot_sent_yest": 20, "total_sent_yest": 54,
    "indo_sent_mtd": 210, "cs_sent_mtd": 33, "bot_sent_mtd": 140, "total_sent_mtd": 383,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="MCN Mekoo daily report -> Telegram")
    ap.add_argument("--no-send", dest="send", action="store_false",
                    help="query + print message, do not post to Telegram")
    ap.add_argument("--sample", action="store_true",
                    help="skip ClickHouse; render the message from sample data (offline test)")
    args = ap.parse_args()

    w = ict_window()
    report_date = datetime.strptime(w["yesterday"], "%Y-%m-%d").strftime("%d/%m/%Y")
    data = _SAMPLE if args.sample else fetch(w)
    msg = format_message(data, report_date)
    print(msg)

    if args.sample:
        print("\n[sample mode] not sent.")
        return

    reason = None if args.sample else check_invites_sane(data)
    if reason:
        print(f"\n⚠️  Sanity check failed: {reason}")
        print("Refusing to send a likely-wrong report. Re-run manually once the "
              "warehouse has yesterday's invitation rows.")
        raise SystemExit(1)

    if not args.send:
        print("\n[--no-send] not sent.")
        return

    res = telegram.send_message(env.CHAT_MEKOO, msg)
    print(f"\n📤 Sent MCN Mekoo daily report to {env.CHAT_MEKOO} (msg_id={res['message_id']}).")


if __name__ == "__main__":
    main()
