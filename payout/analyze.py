"""Totals, duplicate detection and suspicious-name flagging."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .config import SUSPICIOUS_NAME_KEYWORDS, MIN_HOLDER_NAME_LEN
from .parse import Record


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


@dataclass
class Totals:
    count: int
    total_usdt: float
    total_local: float
    currency: str
    rate: float | None = None   # local per USDT if a fixed rate was used

    @property
    def blended_rate(self) -> float:
        return round(self.total_local / self.total_usdt, 2) if self.total_usdt else 0.0


def totals(records: list[Record], rate: float | None = None, currency: str = "") -> Totals:
    total_local = round(sum(r.local for r in records), 2)
    if rate:
        total_usdt = round(total_local / rate, 2)
    else:
        # No fixed rate configured: use the warehouse's own USD figure.
        total_usdt = round(sum(r.usd for r in records), 2)
    return Totals(
        count=len(records),
        total_usdt=total_usdt,
        total_local=total_local,
        currency=currency,
        rate=rate,
    )


def find_duplicates(records: list[Record]) -> list[dict]:
    """Groups of >1 request sharing the SAME (account, holder) pair.

    ``account`` already coalesces account_number (bank) and phone_number
    (e-wallet), so one rule covers both. Matching on the pair — not on either
    field alone — is intentional: the same person may legitimately have two
    requests to different accounts, and two people may share a joint account."""
    by_pair: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for r in records:
        if r.account and r.holder:
            by_pair[(_norm(r.account), _norm(r.holder))].append(r)

    groups: list[dict] = []
    for (acct, _holder), grp in by_pair.items():
        if len(grp) < 2:
            continue
        first = grp[0]
        groups.append({
            "reason": "same_account_holder",
            "label": f"{first.bank or '?'} {first.account} · {first.holder}",
            "requests": [r.request_number for r in grp],
            "holder": first.holder,
            "account": first.account,
            "bank": first.bank,
        })
    return groups


def find_suspicious(records: list[Record]) -> list[dict]:
    """Holder names that need a manual look before paying. Flagged, never
    auto-blocked — the reviewer decides row by row. Catches:

      - empty holder name
      - holder that is just a wallet/bank brand (e.g. "DANA", "GCash")
      - holder equal to the account/phone (phone-as-name)
      - implausibly short names
    """
    flagged: list[dict] = []
    for r in records:
        name = _norm(r.holder)
        reasons: list[str] = []
        if not name:
            reasons.append("empty holder name")
        elif name == _norm(r.account) or name == _norm(r.phone):
            reasons.append("holder equals account/phone number")
        elif len(name) < MIN_HOLDER_NAME_LEN:
            reasons.append("holder name too short")
        else:
            for kw in SUSPICIOUS_NAME_KEYWORDS:
                if name == kw or re.search(rf"\b{re.escape(kw)}\b", name):
                    reasons.append(f"name matches '{kw}'")
                    break
        if reasons:
            flagged.append({
                "request_number": r.request_number,
                "holder": r.holder,
                "account": r.account,
                "bank": r.bank,
                "local": r.local,
                "currency": "",   # filled in by the caller from the market currency
                "reasons": reasons,
            })
    return flagged


def find_missing_details(records: list[Record]) -> list[dict]:
    """Requests with no usable account to transfer to (rare with the staging
    view). Worth a heads-up; the beneficiary data must be filled before paying."""
    out = []
    for r in records:
        if not r.account.strip():
            out.append({
                "request_number": r.request_number,
                "holder": r.holder,
                "account": r.account,
                "contact": r.contact,
                "local": r.local,
            })
    return out
