"""Reconciliation + carryover for the payout runner.

The owner prefunds the agency each batch. A request later marked ``Fail`` is a
prefund that was never disbursed = a credit owed back, deducted from the next
transfer. Two edge cases need a state file that rolls a surplus forward:

  1. Fail + carryover > Open  → net transfer is 0, unused credit rolls forward.
  2. A Fail flips between runs and is missed by the next cutoff.

State lives at ``run/state/carryover_<PREFIX>.json`` (git-ignored).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("MEKOO_STATE_DIR", _PROJECT_ROOT / "run" / "state"))

# PDFs are named payout_<market>_<YYYYMMDD_HHMM>.pdf by build.py.
_PDF_RE = re.compile(r"payout_(?P<market>[a-z]+)_(?P<stamp>\d{8}_\d{4})\.pdf$", re.I)


def resolve_cutoff(market: str, outdir: str, now: datetime,
                   fallback_days: int = 7) -> tuple[str, str | None]:
    """Cutoff = timestamp of the most recent prior payout_<market>_*.pdf that is
    strictly before ``now``. Falls back to ``now - fallback_days`` if none.

    Returns (cutoff 'YYYY-MM-DD HH:MM:SS', prior_pdf_name or None).
    """
    best_dt: datetime | None = None
    best_name: str | None = None
    outp = Path(outdir)
    if outp.is_dir():
        for f in outp.iterdir():
            m = _PDF_RE.match(f.name)
            if not m or m.group("market").lower() != market.lower():
                continue
            try:
                dt = datetime.strptime(m.group("stamp"), "%Y%m%d_%H%M")
            except ValueError:
                continue
            if dt < now and (best_dt is None or dt > best_dt):
                best_dt, best_name = dt, f.name
    cutoff_dt = best_dt if best_dt else now - timedelta(days=fallback_days)
    return cutoff_dt.strftime("%Y-%m-%d %H:%M:%S"), best_name


def _state_file(prefix: str) -> Path:
    return STATE_DIR / f"carryover_{prefix}.json"


def load_carryover(prefix: str) -> dict | None:
    f = _state_file(prefix)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None   # corrupt state -> treat as no carryover, don't crash


def save_carryover(prefix: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = _state_file(prefix)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


def delete_carryover(prefix: str) -> None:
    _state_file(prefix).unlink(missing_ok=True)


def net_transfer(total_local: float, total_usdt: float,
                 credit_local: float, credit_usdt: float) -> dict:
    """Apply credits (Fail + carryover) to today's Open total.

    ``credit_*`` = fail_total + carryover. Returns the net transfer (floored at
    0) and any surplus that rolls forward when credits exceed the Open total.
    """
    return {
        "net_transfer_local": round(max(0.0, total_local - credit_local), 2),
        "net_transfer_usdt": round(max(0.0, total_usdt - credit_usdt), 2),
        "surplus_local": round(max(0.0, credit_local - total_local), 2),
        "surplus_usdt": round(max(0.0, credit_usdt - total_usdt), 2),
    }
