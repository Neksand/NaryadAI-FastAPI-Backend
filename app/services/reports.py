"""Генерация XLSX/PDF, порт src/reports/report.service.ts."""
import io
import json
import os
from datetime import datetime, timezone


async def _rows(conn, export: dict) -> tuple[list[str], list[list]]:
    rtype = export["report_type"]
    filters = export["filters"] if isinstance(export["filters"], dict) else json.loads(export["filters"] or "{}")
    if rtype == "materials":
        rows = await conn.fetch("SELECT m.name, sum(mw.quantity)::text AS qty FROM material_writeoffs mw JOIN materials m ON m.id=mw.material_id GROUP BY m.name LIMIT 10000")
        return (["material", "qty"], [[r["name"], r["qty"]] for r in rows])
    if rtype == "downtime":
        rows = await conn.fetch("SELECT id, number, equipment_stopped_at, equipment_restored_at FROM work_orders WHERE equipment_stopped_at IS NOT NULL LIMIT 10000")
        return (["id", "number", "stopped_at", "restored_at"], [[str(r["id"]), r["number"], str(r["equipment_stopped_at"]), str(r["equipment_restored_at"])] for r in rows])
    wo_id = filters.get("work_order_id")
    if wo_id:
        rows = await conn.fetch("SELECT id, number, status, priority, issued_at FROM work_orders WHERE id=$1::uuid", wo_id)
    else:
        rows = await conn.fetch("SELECT id, number, status, priority, issued_at FROM work_orders ORDER BY issued_at DESC LIMIT 10000")
    return (["id", "number", "status", "priority", "issued_at"],
            [[str(r["id"]), r["number"], r["status"], r["priority"], str(r["issued_at"])] for r in rows])


def _xlsx(header: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill(start_color="194C93", fill_type="solid")
    for r in rows:
        ws.append(r)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf(header: list[str], rows: list[list]) -> bytes:
    font_path = os.environ.get("PDF_FONT_PATH", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf")
    if not os.path.exists(font_path):
        raise RuntimeError("PDF_FONT_PATH must point to a Unicode TrueType font")
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("Noto", "", font_path)
    pdf.set_font("Noto", size=10)
    pdf.cell(0, 10, " | ".join(header))
    pdf.ln()
    pdf.set_font("Noto", size=8)
    for r in rows[:500]:
        pdf.cell(0, 6, " | ".join(str(x) for x in r)[:180])
        pdf.ln()
    return bytes(pdf.output())


async def process_export(conn, export_id: str) -> None:
    from app.storage import put_bytes

    exp = await conn.fetchrow("SELECT * FROM report_exports WHERE id=$1::uuid", export_id)
    if not exp or exp["state"] == "completed":
        return
    await conn.execute("UPDATE report_exports SET state='processing' WHERE id=$1::uuid", export_id)
    try:
        header, rows = await _rows(conn, dict(exp))
        fmt = exp["format"]
        data = _xlsx(header, rows) if fmt == "xlsx" else _pdf(header, rows)
        key = f"reports/{exp['requested_by']}/{export_id}.{'xlsx' if fmt == 'xlsx' else 'pdf'}"
        put_bytes(key, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "application/pdf")
        await conn.execute("UPDATE report_exports SET state='completed', object_key=$2, completed_at=now() WHERE id=$1::uuid", export_id, key)
    except Exception as e:  # noqa: BLE001
        await conn.execute("UPDATE report_exports SET state='failed', error=$2 WHERE id=$1::uuid", export_id, str(e)[:500])
