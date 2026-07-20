"""Step 0 — VAI credit pre-flight (query-based).

Before billing a new Open batch, check whether Tevi has transferred STAR (VAI)
to the agency wallet for every ``Done`` request. A Done request means the agency
already paid the user in fiat; if Tevi never credited VAI back (``vai_tx_id``
empty on the raw ``tevi_fiat.wd_tx`` row), money left the agency but VAI was
never funded — a leak (the 2026-07-02 settlement bug dropped 6 credits, ~$959).

The detection query lives in queries.vai_leaks_sql; here we only turn its rows
into a status + human message. This NEVER blocks the payout — it flags risk.
"""

from __future__ import annotations


def preflight_check(leak_rows: list[dict]) -> dict:
    """Turn leak-candidate rows (Done + empty vai_tx_id) into a status report.

    Returns {status, message, leaks} where status is ``clean`` or ``leak``.
    """
    if not leak_rows:
        return {
            "status": "clean",
            "message": "✅ VAI pre-flight clean — every Done request has a STAR/VAI credit.",
            "leaks": [],
        }

    total = 0.0
    for r in leak_rows:
        try:
            total += float(r.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    sample = ", ".join(str(r.get("request_number")) for r in leak_rows[:10])
    more = "" if len(leak_rows) <= 10 else f" (+{len(leak_rows) - 10} more)"
    return {
        "status": "leak",
        "message": (
            f"🔴 VAI credit LEAK: {len(leak_rows)} Done request(s) with no STAR credit "
            f"(≈ {total:,.2f} total). Tevi has not reimbursed the agency wallet — "
            f"investigate. Payout continues, not blocked.\n   {sample}{more}"
        ),
        "leaks": leak_rows,
    }
