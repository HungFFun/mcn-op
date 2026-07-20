"""Turn raw ClickHouse results into clean payout records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Record:
    request_number: str
    method_id: str        # '11' bank / '16' e-wallet (ID); '10' (PH)
    local: float          # net_amount in local currency (IDR / PHP)
    usd: float            # amount_usd from the warehouse (informational)
    holder: str           # beneficiary name (holder_name or account_name)
    account: str          # account_number or phone_number
    bank: str             # bank / bank_name / wallet_name (brand)
    wallet: str           # wallet_name if e-wallet, else ""
    phone: str            # phone_number if e-wallet, else ""
    contact: str
    created: str
    xr: float = 0.0       # payout_request.exchange_rate (PHP/USDT for PH; 0 if absent)

    @property
    def is_ewallet(self) -> bool:
        return bool(self.wallet)

    def to_dict(self) -> dict:
        return asdict(self)


def _to_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0.0


def _s(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v).strip())


def parse_rows(payload: Any) -> list[Record]:
    """Accepts the ClickHouse result in any of these shapes:

      - the already-decoded {"columns": [...], "rows": [...]} object
      - the raw connector string '{"result": "{...}"}' / '{"columns": ...}'
      - a plain list of row-lists (with the standard column order)
    """
    obj = _unwrap(payload)
    columns = obj["columns"]
    idx = {c: i for i, c in enumerate(columns)}

    def col(row, name, default=None):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else default

    records: list[Record] = []
    for row in obj["rows"]:
        records.append(
            Record(
                request_number=_s(col(row, "request_number")),
                method_id=_s(col(row, "method_id")),
                local=_to_float(col(row, "net_local")),
                usd=_to_float(col(row, "usd")),
                holder=_s(col(row, "holder")),
                account=_s(col(row, "acct")),
                bank=_s(col(row, "bank")),
                wallet=_s(col(row, "wallet")),
                phone=_s(col(row, "phone")),
                contact=_s(col(row, "contact")),
                created=_s(col(row, "created")),
                xr=_to_float(col(row, "xr")),
            )
        )
    return records


def _unwrap(payload: Any) -> dict:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict) and "result" in payload and isinstance(payload["result"], str):
        payload = json.loads(payload["result"])
    if isinstance(payload, dict) and "columns" in payload:
        return payload
    if isinstance(payload, list):
        cols = ["request_number", "method_id", "net_local", "usd",
                "holder", "acct", "bank", "wallet", "phone", "contact", "xr", "created"]
        return {"columns": cols, "rows": payload}
    raise ValueError("Unrecognised ClickHouse result shape")
