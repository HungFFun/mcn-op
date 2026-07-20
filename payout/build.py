"""Core payout-batch builder: ClickHouse Open result -> PDF + summary dict.

Two entry points:
  - ``run_data(market, raw, outdir, now, ...)`` — the pure core used by run.py
    (which supplies live ClickHouse data + reconciliation inputs).
  - ``run(...)`` / ``main()`` — file-based CLI for offline testing from a saved
    Open result (no reconciliation).

The returned summary drives the Telegram send (see telegram_client.py).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from .config import MARKETS, METHOD_LABELS
from .parse import parse_rows, Record
from .analyze import totals, find_duplicates, find_suspicious, find_missing_details
from .pdf_report import build_pdf
from . import reconcile


def _fmt(n: float) -> str:
    return f"{n:,.2f}" if n % 1 else f"{int(n):,}"


def _batch_id(prefix: str, now: datetime) -> str:
    return f"{prefix}-{now.strftime('%Y%m%d-%H%M')}"


def _dynamic_rate(records: list[Record], markup: float) -> float | None:
    """PHP/USDT = avg(payout_request.exchange_rate over the Open rows) + markup
    (payout-ph skill). Returns None if no row carries an exchange_rate, so the
    caller falls back to the warehouse ``amount_usd`` sum."""
    xrs = [r.xr for r in records if r.xr > 0]
    if not xrs:
        return None
    return round(sum(xrs) / len(xrs) + markup, 6)


def run(market: str, rows_path: str, outdir: str, now: datetime) -> dict:
    """File-based entry: read a saved Open result, build (no reconciliation)."""
    with open(rows_path, "r", encoding="utf-8") as f:
        raw = f.read()
    return run_data(market, raw, outdir, now)


def run_data(market: str, raw, outdir: str, now: datetime,
             fail_raw=None, carryover: dict | None = None,
             cutoff: str | None = None, prior_pdf: str | None = None) -> dict:
    """Build the batch. ``raw`` is the Open ClickHouse result; ``fail_raw`` the
    Fail-credits result (optional); ``carryover`` the prior surplus state."""
    if market not in MARKETS:
        raise SystemExit(f"unknown market '{market}' (expected id|ph)")
    m = MARKETS[market]
    ccy = m["currency"]

    records = parse_rows(raw)

    # Rate resolution: fixed (ID) → dynamic avg(exchange_rate)+markup (PH) →
    # None (fall back to the warehouse amount_usd sum). The dynamic rate is
    # derived from the Open rows and reused for the Fail credits below, so both
    # sections convert at the same PHP/USDT (payout-ph skill).
    rate = m.get("usdt_rate")
    if rate is None and m.get("dynamic_rate_markup") is not None:
        rate = _dynamic_rate(records, m["dynamic_rate_markup"])

    tot = totals(records, rate=rate, currency=ccy)

    dups = find_duplicates(records)
    susp = find_suspicious(records)
    for s in susp:
        s["currency"] = ccy
    missing = find_missing_details(records)
    dup_reqs = {rn for g in dups for rn in g["requests"]}
    susp_reqs = {s["request_number"] for s in susp}

    # --- reconciliation --------------------------------------------------
    fail_records: list[Record] = parse_rows(fail_raw) if fail_raw else []
    fail_local = round(sum(r.local for r in fail_records), 2)
    fail_usdt = round(fail_local / rate, 2) if rate else round(sum(r.usd for r in fail_records), 2)
    carry_local = float(carryover["surplus_local"]) if carryover else 0.0
    carry_usdt = float(carryover["surplus_usdt"]) if carryover else 0.0

    credit_local = round(fail_local + carry_local, 2)
    credit_usdt = round(fail_usdt + carry_usdt, 2)
    nt = reconcile.net_transfer(tot.total_local, tot.total_usdt, credit_local, credit_usdt)

    date_label = now.strftime("%Y-%m-%d")
    hhmm_label = now.strftime("%H:%M")
    batch_id = _batch_id(m["batch_prefix"], now)

    os.makedirs(outdir, exist_ok=True)
    pdf_path = os.path.join(outdir, f"payout_{market}_{now.strftime('%Y%m%d_%H%M')}.pdf")
    recon = {
        "fail_count": len(fail_records),
        "fail_local": fail_local,
        "fail_usdt": fail_usdt,
        "carry_local": carry_local,
        "carry_usdt": carry_usdt,
        "net_transfer_local": nt["net_transfer_local"],
        "net_transfer_usdt": nt["net_transfer_usdt"],
        "cutoff": cutoff,
    } if (fail_records or carryover) else None
    build_pdf(
        out_path=pdf_path,
        market_label=m["label"],
        agency_name=m["agency_name"],
        agency_id=m["agency_id"],
        batch_id=batch_id,
        generated_at=f"{date_label} {hhmm_label}",
        records=records,
        tot=tot,
        duplicate_reqs=dup_reqs,
        suspicious_reqs=susp_reqs,
        method_labels=METHOD_LABELS,
        recon=recon,
    )

    batch_message = _compose_message(batch_id, m, tot, dups, susp, missing, recon)

    return {
        "market": market,
        "batch_id": batch_id,
        "date_label": date_label,
        "hhmm_label": hhmm_label,
        "pdf_path": os.path.abspath(pdf_path),
        "open_count": tot.count,
        "total_usdt": tot.total_usdt,
        "total_local": tot.total_local,
        "currency": ccy,
        "rate": rate,
        "blended_rate": tot.blended_rate,
        "fail_count": len(fail_records),
        "fail_total_usdt": fail_usdt,
        "fail_total_local": fail_local,
        "carryover_usdt": carry_usdt,
        "carryover_local": carry_local,
        "net_transfer_usdt": nt["net_transfer_usdt"],
        "net_transfer_local": nt["net_transfer_local"],
        "surplus_usdt": nt["surplus_usdt"],
        "surplus_local": nt["surplus_local"],
        "cutoff": cutoff,
        "prior_pdf": prior_pdf,
        "telegram_tool": m["telegram_tool"],
        "duplicates": dups,
        "suspicious": susp,
        "missing_details": missing,
        "batch_message": batch_message,
    }


def _compose_message(batch_id, m, tot, dups, susp, missing, recon) -> str:
    ccy = tot.currency or m["currency"]
    lines = [
        f"<b>Batch {batch_id}</b>",
        f"{tot.count} lệnh Open · {_fmt(tot.total_local)} {ccy} ≈ {_fmt(tot.total_usdt)} USDT",
    ]
    if recon and (recon["fail_count"] or recon["carry_local"] or recon["carry_usdt"]):
        lines.append(
            f"➖ Trừ credit: {recon['fail_count']} Fail "
            f"{_fmt(recon['fail_local'])} {ccy}"
            + (f" + carryover {_fmt(recon['carry_local'])} {ccy}" if recon["carry_local"] else "")
        )
        lines.append(
            f"💸 Chuyển thực: <b>{_fmt(recon['net_transfer_local'])} {ccy}</b> "
            f"≈ <b>{_fmt(recon['net_transfer_usdt'])} USDT</b>"
        )
    if dups:
        lines.append("")
        lines.append(f"⚠️ <b>{len(dups)} nhóm trùng</b> (kiểm tra từng dòng):")
        for g in dups[:15]:
            lines.append(f"• {g['label']} → {', '.join(g['requests'])}")
    if susp:
        lines.append("")
        lines.append(f"🔎 <b>{len(susp)} tên nghi vấn</b> (không chặn):")
        for s in susp[:15]:
            lines.append(
                f"• {s['request_number']} — \"{s['holder'] or '(trống)'}\" "
                f"({_fmt(s['local'])} {ccy})")
    if missing:
        lines.append("")
        lines.append(f"ℹ️ {len(missing)} lệnh thiếu số TK trong payload (cần bổ sung trước khi chi).")
    if not dups and not susp and not missing:
        lines.append("")
        lines.append("✅ Không có trùng lặp, tên nghi vấn hay thiếu dữ liệu.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=list(MARKETS))
    ap.add_argument("--rows", required=True, help="ClickHouse Open result JSON")
    ap.add_argument("--outdir", default="./out")
    ap.add_argument("--now", default=None, help='override run time "YYYY-MM-DD HH:MM"')
    args = ap.parse_args()

    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now else datetime.now()
    summary = run(args.market, args.rows, args.outdir, now)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
