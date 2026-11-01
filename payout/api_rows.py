"""Map live agency-API rows (wapi.flowstreamx.com/fiat/v1/agency/tx) to the
{"columns": [...], "rows": [...]} shape ch_client returns, so parse.parse_rows
and build.run_data work unchanged whatever the source.

Beneficiary fields are coalesced across the bank / e-wallet shapes exactly like
queries.py. The API has no amount_usd, so USD per row = net_amount /
exchange_rate (only used when the market has no fixed usdt_rate).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

OPEN_COLUMNS = ["request_number", "method_id", "net_local", "usd", "holder", "acct",
                "bank", "wallet", "phone", "contact", "xr", "created"]
FAIL_COLUMNS = ["request_number", "method_id", "net_local", "usd", "holder", "acct",
                "failed_at"]


def open_raw(items: list[dict]) -> dict[str, Any]:
    """Open requests, oldest first."""
    items = sorted(items, key=lambda r: r.get("created_on") or 0)
    return {"columns": OPEN_COLUMNS, "rows": [_open_row(r) for r in items]}


def fail_raw(items: list[dict], cutoff: str) -> dict[str, Any]:
    """Fail requests updated after ``cutoff`` ('YYYY-MM-DD HH:MM:SS', local time).
    The API cannot filter on update time, so the full Fail list is filtered here."""
    cut = datetime.strptime(cutoff, "%Y-%m-%d %H:%M:%S").timestamp()
    items = sorted((r for r in items if (r.get("updated_on") or 0) > cut),
                   key=lambda r: r.get("updated_on") or 0)
    return {"columns": FAIL_COLUMNS, "rows": [_fail_row(r) for r in items]}


def _first(*vals: Any) -> str:
    return next((str(v) for v in vals if v not in (None, "")), "")


def _num(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _ts(epoch: Any) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else ""


def _common(r: dict) -> dict:
    tx = r.get("transaction") or {}
    bd = tx.get("billing_detail") or {}
    net = _num(tx.get("net_amount"))
    xr = _num(r.get("exchange_rate"))
    return {
        "request_number": _first(r.get("request_number")),
        "method_id": _first((tx.get("payout_method") or {}).get("id")),
        "net_local": net,
        "usd": round(net / xr, 2) if xr else 0.0,
        "holder": _first(bd.get("holder_name"), bd.get("account_name")),
        "acct": _first(bd.get("account_number"), bd.get("phone_number")),
        "bank": _first(bd.get("bank"), bd.get("bank_name"), bd.get("wallet_name")),
        "wallet": _first(bd.get("wallet_name")) or None,
        "phone": _first(bd.get("phone_number")) or None,
        "xr": xr,
    }


def _open_row(r: dict) -> list:
    d = _common(r)
    d["contact"] = ""
    d["created"] = _ts(r.get("created_on"))
    return [d[c] for c in OPEN_COLUMNS]


def _fail_row(r: dict) -> list:
    d = _common(r)
    d["failed_at"] = _ts(r.get("updated_on"))
    return [d[c] for c in FAIL_COLUMNS]
