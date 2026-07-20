"""Billing-detail PDF for a payout batch (reportlab)."""

from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)

from .parse import Record
from .analyze import Totals


def _fmt(n: float) -> str:
    return f"{n:,.2f}" if n % 1 else f"{int(n):,}"


def _brand(r: Record) -> str:
    """Display brand: wallet name for e-wallet, short bank code otherwise."""
    if r.wallet:
        return r.wallet
    if r.bank:
        return r.bank.split(" - ")[0].strip()  # "BCA - Bank Central Asia" -> "BCA"
    return "—"


def build_pdf(
    out_path: str,
    market_label: str,
    agency_name: str,
    agency_id: str,
    batch_id: str,
    generated_at: str,
    records: list[Record],
    tot: Totals,
    duplicate_reqs: set[str],
    suspicious_reqs: set[str],
    method_labels: dict[str, str] | None = None,
    recon: dict | None = None,
) -> str:
    method_labels = method_labels or {}
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=15, spaceAfter=2)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5,
                           textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7.5, leading=9)

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
    )
    flow = []
    flow.append(Paragraph(f"Tevi Payout — {market_label}", h1))
    flow.append(Paragraph(
        f"Batch <b>{batch_id}</b> &nbsp;|&nbsp; Agency {agency_name} "
        f"({agency_id}) &nbsp;|&nbsp; Generated {generated_at}", small))
    flow.append(Spacer(1, 4 * mm))
    rate_note = (f"(≈ {_fmt(tot.blended_rate)} {tot.currency}/USDT"
                 + (" — fixed" if tot.rate else " — blended") + ")")
    flow.append(Paragraph(
        f"<b>{tot.count}</b> Open requests &nbsp;|&nbsp; "
        f"Total <b>{_fmt(tot.total_local)} {tot.currency}</b> "
        f"≈ <b>{_fmt(tot.total_usdt)} USDT</b> {rate_note}", styles["Normal"]))
    flow.append(Spacer(1, 4 * mm))

    header = ["#", "Request", "Method", "Account / Phone", "Account Name",
              f"Net ({tot.currency})", "USDT", "Flag"]
    data = [header]
    for i, r in enumerate(records, 1):
        flags = []
        if r.request_number in duplicate_reqs:
            flags.append("DUP")
        if r.request_number in suspicious_reqs:
            flags.append("CHK")
        method = _brand(r)
        kind = method_labels.get(r.method_id, "")
        method_cell = f"{method}" + (f" <font size=6 color='#888'>({kind})</font>" if kind else "")
        data.append([
            str(i),
            Paragraph(r.request_number, cell),
            Paragraph(method_cell, cell),
            Paragraph(r.account or "—", cell),
            Paragraph(r.holder or "—", cell),
            _fmt(r.local),
            _fmt(r.usd),
            " ".join(flags),
        ])

    table = Table(
        data, repeatRows=1,
        colWidths=[8 * mm, 24 * mm, 26 * mm, 30 * mm, 36 * mm,
                   24 * mm, 14 * mm, 12 * mm],
    )
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (5, 0), (6, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (7, 0), (7, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
        ("FONTSIZE", (0, 1), (-1, -1), 7.5),
    ]
    for i, r in enumerate(records, 1):
        if r.request_number in duplicate_reqs or r.request_number in suspicious_reqs:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fef3c7")))
    table.setStyle(TableStyle(style))
    flow.append(table)

    if recon:
        flow.append(Spacer(1, 4 * mm))
        ccy = tot.currency
        lines = [f"<b>Reconciliation</b> (cutoff {recon.get('cutoff') or '—'})"]
        lines.append(
            f"Open total: {_fmt(tot.total_local)} {ccy} ≈ {_fmt(tot.total_usdt)} USDT")
        if recon["fail_count"]:
            lines.append(
                f"− Fail credits ({recon['fail_count']}): "
                f"{_fmt(recon['fail_local'])} {ccy} ≈ {_fmt(recon['fail_usdt'])} USDT")
        if recon["carry_local"] or recon["carry_usdt"]:
            lines.append(
                f"− Carryover: {_fmt(recon['carry_local'])} {ccy} "
                f"≈ {_fmt(recon['carry_usdt'])} USDT")
        lines.append(
            f"<b>= Net transfer: {_fmt(recon['net_transfer_local'])} {ccy} "
            f"≈ {_fmt(recon['net_transfer_usdt'])} USDT</b>")
        for ln in lines:
            flow.append(Paragraph(ln, styles["Normal"]))

    flow.append(Spacer(1, 3 * mm))
    flow.append(Paragraph(
        "DUP = duplicate (account, holder) in this batch &nbsp;·&nbsp; "
        "CHK = holder name needs manual review. Neither is auto-blocked.", small))

    doc.build(flow)
    return out_path
