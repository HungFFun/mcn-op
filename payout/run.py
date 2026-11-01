"""Standalone daily payout runner — connects to ClickHouse and sends to
Telegram directly, no MCP involved.

Per market it: VAI credit pre-flight → fetch Open → resolve reconciliation
cutoff → fetch Fail credits → load carryover → build PDF + summary → send to
Telegram → persist any rolled-forward surplus.

Usage
-----
    python3 -m payout.run --market both           # ID + PH, full flow + send
    python3 -m payout.run --market id
    python3 -m payout.run --market both --no-send # build PDFs only, skip Telegram
    python3 -m payout.run --market both --now "2026-07-14 08:00"
    python3 -m payout.run --market id --no-preflight
    python3 -m payout.run --market id --source extension --no-send  # live API via Chrome extension

Requires credentials in ~/.claude/secrets/clickhouse.env and
~/.claude/secrets/telegram_payout.env (see .env.example). Install deps first:
    pip3 install -r requirements.txt --break-system-packages
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from .config import MARKETS
from . import api_rows
from . import ch_client
from . import ext_bridge
from . import settings
from . import build as build_mod
from . import telegram_client
from . import reconcile
from . import vai_preflight


def run_market(market: str, outdir: str, now: datetime, send: bool,
               preflight: bool = True, source: str = "clickhouse") -> dict:
    m = MARKETS[market]

    # Step 0 — VAI credit pre-flight (advisory, never blocks). It needs the raw
    # vai_tx_id in ClickHouse, so the extension source skips it.
    pf = None
    if preflight and source == "clickhouse":
        leaks = ch_client.fetch_vai_leaks(m["agency_id"])
        pf = vai_preflight.preflight_check(leaks)
        print(f"[{market}] {pf['message']}")
    elif preflight:
        print(f"[{market}] VAI pre-flight skipped (source=extension, needs ClickHouse)")

    # Reconciliation cutoff comes from the prior PDF, not the data source.
    cutoff, prior_pdf = reconcile.resolve_cutoff(market, outdir, now)
    if source == "extension":
        settings.require_extension()
        open_items, fail_items = ext_bridge.request_data(market)
        raw = api_rows.open_raw(open_items)
        fail_raw = api_rows.fail_raw(fail_items, cutoff) if raw["rows"] else None
    else:
        raw = ch_client.fetch_open(m["agency_id"])
        fail_raw = (ch_client.fetch_fail_credits(m["agency_id"], cutoff)
                    if raw["rows"] else None)
    if not raw["rows"]:
        return {"market": market, "skipped": "no Open requests", "vai_preflight": pf}

    carryover = reconcile.load_carryover(m["batch_prefix"])

    summary = build_mod.run_data(
        market, raw, outdir, now,
        fail_raw=fail_raw, carryover=carryover, cutoff=cutoff, prior_pdf=prior_pdf)
    summary["vai_preflight"] = pf

    # Step 7 — persist / clear carryover based on the rolled-forward surplus.
    if summary["surplus_usdt"] > 0 or summary["surplus_local"] > 0:
        reconcile.save_carryover(m["batch_prefix"], {
            "surplus_usdt": summary["surplus_usdt"],
            "surplus_local": summary["surplus_local"],
            "from_pdf": os.path.basename(summary["pdf_path"]),
            "noted_at": f"{summary['date_label']} {summary['hhmm_label']}",
            "reason": "Fail+carryover exceeded Open total",
        })
        summary["carryover_written"] = True
    elif carryover is not None:
        reconcile.delete_carryover(m["batch_prefix"])
        summary["carryover_cleared"] = True

    if send:
        summary["telegram"] = telegram_client.send_payout(market, summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="both", choices=["id", "ph", "both"])
    ap.add_argument("--outdir", default=os.path.join(os.getcwd(), "run", "out"))
    ap.add_argument("--now", default=None, help='override run time "YYYY-MM-DD HH:MM"')
    ap.add_argument("--no-send", dest="send", action="store_false",
                    help="build the PDFs but do not post to Telegram")
    ap.add_argument("--no-preflight", dest="preflight", action="store_false",
                    help="skip the VAI credit pre-flight")
    ap.add_argument("--source", default=settings.EXT["source"],
                    choices=["clickhouse", "extension"],
                    help="clickhouse (warehouse view) or extension (live agency API "
                         "through the Chrome extension, see payout/ext_bridge.py)")
    args = ap.parse_args()

    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now else datetime.now()
    markets = ["id", "ph"] if args.market == "both" else [args.market]

    results = []
    for mk in markets:
        try:
            results.append(run_market(mk, args.outdir, now, args.send, args.preflight,
                                      args.source))
        except Exception as exc:  # noqa: BLE001 — report per market, keep going
            results.append({"market": mk, "error": f"{type(exc).__name__}: {exc}"})

    for r in results:
        if r.get("skipped"):
            print(f"[{r['market']}] skipped — {r['skipped']}")
        elif r.get("error"):
            print(f"[{r['market']}] ERROR — {r['error']}")
        else:
            sent = "sent" if r.get("telegram") else "built (not sent)"
            net = (f" · net {r['net_transfer_local']:,.0f} {r['currency']}"
                   if r["fail_count"] or r["carryover_usdt"] else "")
            print(f"[{r['market']}] {r['batch_id']}: {r['open_count']} Open · "
                  f"{r['total_local']:,.0f} {r['currency']} ≈ {r['total_usdt']:,.2f} USDT · "
                  f"{r['fail_count']} Fail · {len(r['duplicates'])} dup{net} · {sent}")
    print(json.dumps(results, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
