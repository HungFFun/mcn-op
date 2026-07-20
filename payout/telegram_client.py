"""Send payout PDFs + summaries to Telegram directly via the Bot API.

Mirrors the caption format of the payout-id / payout-ph skill. Credentials come
from settings (the same telegram_payout.env the tevi-telegram MCP uses).
"""

from __future__ import annotations

from typing import Any

from . import settings

_API = "https://api.telegram.org/bot{token}/{method}"


def _fmt(n: float) -> str:
    return f"{n:,.2f}" if n % 1 else f"{int(n):,}"


def _requests():
    try:
        import requests  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "requests is not installed. Run:\n"
            "    pip3 install -r requirements.txt --break-system-packages"
        ) from exc
    return requests


def build_caption(market: str, summary: dict[str, Any]) -> str:
    """PDF caption. USDT is the headline figure; local currency is the ≈ hint.

    No reconciliation:
        Payout PH — 2026-07-14 08:04
        • 9 request.
        • Transfer 153.20 USDT (≈ 9,169.21 PHP).
        Batch ID: PH-20260714-0804
        @imnotyourhiiro

    With Fail credits / carryover the "Transfer" line shows the NET amount and
    the credit lines are itemised above it. Batch ID lets Marcus tag the VAI
    wallet transfer for later reconciliation.
    """
    mention = settings.TELEGRAM["mention"].get(market, "")
    ccy = summary["currency"]
    has_credit = summary.get("fail_count") or summary.get("carryover_usdt")

    lines = [f"<b>Payout {market.upper()} — {summary['date_label']} {summary['hhmm_label']}</b>"]
    if not has_credit:
        lines.append(f"• {summary['open_count']} request.")
        lines.append(
            f"• Transfer {_fmt(summary['total_usdt'])} USDT "
            f"(≈ {_fmt(summary['total_local'])} {ccy}).")
    else:
        lines.append(
            f"• {summary['open_count']} request · {_fmt(summary['total_usdt'])} USDT "
            f"(≈ {_fmt(summary['total_local'])} {ccy}).")
        if summary.get("fail_count"):
            lines.append(
                f"• − {summary['fail_count']} Fail credit: "
                f"{_fmt(summary['fail_total_usdt'])} USDT "
                f"(≈ {_fmt(summary['fail_total_local'])} {ccy}).")
        if summary.get("carryover_usdt"):
            lines.append(
                f"• − Carryover: {_fmt(summary['carryover_usdt'])} USDT "
                f"(≈ {_fmt(summary['carryover_local'])} {ccy}).")
        lines.append(
            f"• <b>Transfer {_fmt(summary['net_transfer_usdt'])} USDT "
            f"(≈ {_fmt(summary['net_transfer_local'])} {ccy}).</b>")

    lines.append(f"Batch ID: <code>{summary['batch_id']}</code>")
    if mention:
        lines.append(mention)
    return "\n".join(lines)


def send_payout(market: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Post the PDF with its caption (single message). Returns the message id.

    Duplicate/suspicious/missing flags are not posted as a separate message —
    they are already highlighted (DUP/CHK) inside the attached PDF.
    """
    settings.require_telegram(market)
    requests = _requests()
    token = settings.TELEGRAM["bot_token"]
    chat_id = settings.TELEGRAM["chat_id"][market]

    with open(summary["pdf_path"], "rb") as fh:
        resp = requests.post(
            _API.format(token=token, method="sendDocument"),
            data={
                "chat_id": chat_id,
                "caption": build_caption(market, summary),
                "parse_mode": "HTML",
            },
            files={"document": (f"payout_{market}_{summary['batch_id']}.pdf", fh, "application/pdf")},
            timeout=60,
        )
    doc = resp.json()
    if not doc.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {doc}")

    return {"document_message_id": doc["result"]["message_id"]}
