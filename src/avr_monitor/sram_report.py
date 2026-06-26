"""
sram_report.py — gera um PDF didático do dump da SRAM do Arduino.

Estrutura do PDF:
  Página 1  — Resumo: timestamp, intervalo, estado atual da ULA
  Página 2  — Tabela de variáveis da ULA (endereço, valor, cor, descrição)
  Páginas+  — Dump hexadecimal com bytes da ULA destacados por cor
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Dict, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_OP_NAMES = ["AND", "OR", "NOT", "XOR", "ADD", "SUB", "MUL", "DIV"]

_HIGHLIGHT: Dict[str, Any] = {
    "addr_x":      colors.Color(0.68, 0.85, 0.90),
    "addr_y":      colors.Color(0.80, 0.70, 0.90),
    "addr_result": colors.Color(0.70, 0.90, 0.70),
    "addr_carry":  colors.Color(1.00, 0.80, 0.55),
    "addr_op":     colors.Color(1.00, 0.95, 0.60),
    "addr_estado": colors.Color(1.00, 0.75, 0.75),
}

_FIELD_META = {
    "addr_estado": ("estado", "Estado da ULA (0=EDITING, 4=RESULT)"),
    "addr_x":      ("x",     "Primeiro operando (4 bits)"),
    "addr_y":      ("y",     "Segundo operando (4 bits)"),
    "addr_result": ("result","Resultado da operação (4 bits)"),
    "addr_carry":  ("carry", "Carry/overflow (1 bit)"),
    "addr_op":     ("op",    "Código da operação (0-7)"),
}


def _parse_addr(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 16)
    except (ValueError, TypeError):
        return 0


def generate_sram_pdf_report(
    sram_map: "dict[int, int]",
    ula_addresses: "dict[str, int]",
    latest_snapshot: Any,
    start: int,
    length: int,
) -> bytes:
    """
    Gera o PDF e retorna os bytes.

    sram_map       — {endereço_absoluto: byte_value}
    ula_addresses  — {"addr_x": int, "addr_y": int, ...}
    latest_snapshot — AVRSnapshot (pode ser None)
    start / length  — intervalo coletado
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    center_style = ParagraphStyle("center", parent=styles["Normal"], alignment=TA_CENTER)
    story = []

    ula = getattr(latest_snapshot, "ula", None)

    # ── Página 1: Resumo ───────────────────────────────────────────────────────
    story.append(Paragraph("SRAM ULA Report", styles["Title"]))
    story.append(Paragraph("Arduino AVR ULA Monitor", styles["Heading2"]))
    story.append(Spacer(1, 6 * mm))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bytes_collected = len(sram_map)

    info_rows = [
        ["Campo", "Valor"],
        ["Gerado em", now_str],
        ["Endereço inicial", f"0x{start:04X}"],
        ["Comprimento", f"{length} bytes"],
        ["Bytes coletados", str(bytes_collected)],
    ]
    if ula:
        info_rows += [
            ["Estado ULA", ula.estado_name],
            ["Operação", f"{ula.op_name} (op={ula.op})"],
            ["X", str(ula.x)],
            ["Y", str(ula.y)],
            ["Resultado", str(ula.result)],
            ["Carry", str(ula.carry)],
            ["State version", str(ula.state_version)],
        ]

    info_table = Table(info_rows, colWidths=[55 * mm, 110 * mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME",       (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",       (0, 0), (-1, -1), 10),
        ("FONTNAME",       (0, 0), (0, -1),  "Helvetica-Bold"),
        ("FONTNAME",       (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("BACKGROUND",     (0, 0), (-1, 0),  colors.Color(0.2, 0.2, 0.2)),
        ("TEXTCOLOR",      (0, 0), (-1, 0),  colors.white),
        ("GRID",           (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("TOPPADDING",     (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph(
        "SRAM é a memória volátil usada pelo programa em execução. "
        "Variáveis como x, y, result, carry, op e estado ficam armazenadas "
        "na SRAM enquanto o Arduino está ligado. Este relatório mostra o "
        "conteúdo completo da SRAM no momento da captura, com os endereços "
        "das variáveis da ULA destacados por cor.",
        styles["Normal"],
    ))
    story.append(PageBreak())

    # ── Página 2: Tabela ULA ───────────────────────────────────────────────────
    story.append(Paragraph("Variáveis da ULA na SRAM", styles["Heading1"]))
    story.append(Spacer(1, 4 * mm))

    ula_rows = [["Variável", "Endereço", "Valor na SRAM", "Descrição"]]
    ula_table_styles: list = [
        ("FONTNAME",      (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 0), (-1, -1), 10),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.Color(0.2, 0.2, 0.2)),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
    ]

    for row_i, (field_key, (var_name, desc)) in enumerate(_FIELD_META.items(), start=1):
        addr     = ula_addresses.get(field_key, 0)
        addr_str = f"0x{addr:04X}" if addr else "N/A"
        val      = sram_map.get(addr)
        val_str  = f"{val} (0x{val:02X})" if val is not None else "not captured"
        ula_rows.append([var_name, addr_str, val_str, desc])
        color = _HIGHLIGHT.get(field_key)
        if color:
            ula_table_styles.append(
                ("BACKGROUND", (0, row_i), (-1, row_i), color)
            )

    ula_table = Table(ula_rows, colWidths=[22 * mm, 28 * mm, 38 * mm, 87 * mm])
    ula_table.setStyle(TableStyle(ula_table_styles))
    story.append(ula_table)
    story.append(Spacer(1, 6 * mm))

    # Legenda de cores
    story.append(Paragraph("Legenda de Cores", styles["Heading3"]))
    story.append(Spacer(1, 2 * mm))
    legend_rows = [["Cor", "Variável", "Descrição"]]
    legend_styles: list = [
        ("FONTNAME",      (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.Color(0.2, 0.2, 0.2)),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("ALIGN",         (0, 0), (0, -1),  "CENTER"),
    ]
    for li, (field_key, color) in enumerate(_HIGHLIGHT.items(), start=1):
        var_name = _FIELD_META.get(field_key, (field_key, ""))[0]
        desc     = _FIELD_META.get(field_key, ("", ""))[1]
        legend_rows.append(["", var_name, desc])
        legend_styles.append(("BACKGROUND", (0, li), (0, li), color))

    legend_table = Table(legend_rows, colWidths=[15 * mm, 22 * mm, 138 * mm])
    legend_table.setStyle(TableStyle(legend_styles))
    story.append(legend_table)
    story.append(PageBreak())

    # ── Páginas 3+: Dump hexadecimal ─────────────────────────────────────────
    story.append(Paragraph("Dump Hexadecimal da SRAM", styles["Heading1"]))
    story.append(Spacer(1, 3 * mm))

    # Mapeia endereço → field_key para colorir células
    addr_to_field: Dict[int, str] = {}
    for field_key, addr in ula_addresses.items():
        if addr:
            addr_to_field[addr] = field_key

    # Alinha inicio em múltiplo de 16
    row_start  = (start // 16) * 16
    end_addr   = start + length
    hex_rows   = [["Endereço"] + [f"{i:X}" for i in range(16)]]
    hex_styles: list = [
        ("FONTNAME",      (0, 0), (-1, -1), "Courier"),
        ("FONTSIZE",      (0, 0), (-1, -1), 7),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 1),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 1),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.Color(0.25, 0.25, 0.25)),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Courier-Bold"),
        ("FONTNAME",      (0, 0), (0, -1),  "Courier-Bold"),
        ("TEXTCOLOR",     (0, 0), (0, -1),  colors.Color(0.1, 0.1, 0.1)),
    ]

    row_idx = 1
    for row_addr in range(row_start, end_addr, 16):
        row = [f"0x{row_addr:04X}"]
        for col in range(16):
            addr = row_addr + col
            if addr < start or addr >= end_addr:
                row.append("")
            else:
                val = sram_map.get(addr)
                row.append(f"{val:02X}" if val is not None else "--")
                field_key = addr_to_field.get(addr)
                if field_key and val is not None:
                    color = _HIGHLIGHT.get(field_key)
                    if color:
                        hex_styles.append(
                            ("BACKGROUND", (col + 1, row_idx), (col + 1, row_idx), color)
                        )
        hex_rows.append(row)
        row_idx += 1

    # 20mm endereço + 16 × 10mm bytes = 180mm total (cabe em A4 portrait com margens 15mm)
    hex_col_widths = [20 * mm] + [10 * mm] * 16
    hex_table = Table(hex_rows, colWidths=hex_col_widths, repeatRows=1)
    hex_table.setStyle(TableStyle(hex_styles))
    story.append(hex_table)

    doc.build(story)
    return buf.getvalue()
