"""Exportador a Excel (.xlsx) de contabilidad básica generado dinámicamente desde SQLite."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, Optional, Union

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config import CATEGORIAS_INICIALES, TIPOS_DOCUMENTO
from app.db.database import Database


class AccountingExcelExporter:
    """
    Genera el libro Excel de contabilidad básica a partir de SQLite (Sección 7 y 9).
    Nunca utiliza el Excel como base de datos: SQLite es la única fuente de verdad.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def generate_workbook_bytes(
        self,
        mes: Optional[str] = None,
        categoria: Optional[str] = None,
        estado_revision: Optional[str] = None,
    ) -> bytes:
        wb = openpyxl.Workbook()
        # Eliminar hoja vacía por defecto tras crear las nuestras
        default_sheet = wb.active

        invoices = self.db.list_invoices(
            mes=mes,
            categoria=categoria,
            estado_revision=estado_revision,
            limit=5000,
        )
        metrics = self.db.get_dashboard_metrics()
        transactions = self.db.list_bank_transactions(limit=2000)

        self._build_invoices_sheet(wb.create_sheet("Facturas Recibidas"), invoices)
        self._build_monthly_and_category_sheet(wb.create_sheet("Resumen Mensual y Categorías"), metrics)
        self._build_suppliers_sheet(wb.create_sheet("Por Proveedor"), metrics)
        self._build_reconciliation_sheet(wb.create_sheet("Conciliación Bancaria"), transactions)

        wb.remove(default_sheet)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def export_to_file(
        self,
        output_path: Union[Path, str],
        mes: Optional[str] = None,
        categoria: Optional[str] = None,
    ) -> Path:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.generate_workbook_bytes(mes=mes, categoria=categoria))
        return target

    def _build_invoices_sheet(self, ws: Any, invoices: list[Dict[str, Any]]) -> None:
        headers = [
            "ID",
            "Fecha Emisión",
            "Fecha Vencimiento",
            "Nº Factura",
            "Proveedor",
            "NIF/CIF",
            "Categoría",
            "Tipo Documento",
            "Base Imponible (€)",
            "IVA (€)",
            "Recargo Equiv. (€)",
            "IRPF (€)",
            "Total Factura (€)",
            "Moneda",
            "Estado Revisión",
            "Estado Pago",
            "Importe Conciliado (€)",
            "Importe Pendiente (€)",
            "Confianza (%)",
            "Ruta Archivo PDF",
        ]
        ws.append(headers)
        self._style_header_row(ws, len(headers))

        cat_map = {c["codigo"]: c["nombre"] for c in CATEGORIAS_INICIALES}

        total_base = 0.0
        total_iva = 0.0
        total_re = 0.0
        total_irpf = 0.0
        total_general = 0.0

        for inv in invoices:
            extra = inv.get("datos_extra") or {}
            re_val = round(float(extra.get("recargo_equivalencia") or 0.0), 2)
            irpf_val = round(float(extra.get("irpf") or 0.0), 2)
            base_val = round(float(inv.get("base_imponible") or 0.0), 2)
            iva_val = round(float(inv.get("iva") or 0.0), 2)
            tot_val = round(float(inv.get("total") or 0.0), 2)

            total_base += base_val
            total_iva += iva_val
            total_re += re_val
            total_irpf += irpf_val
            total_general += tot_val

            cat_code = inv.get("categoria") or "otros"
            doc_code = inv.get("tipo_documento") or "otros"

            ws.append(
                [
                    inv.get("id"),
                    inv.get("fecha_emision") or "",
                    inv.get("fecha_vencimiento") or "",
                    inv.get("numero_factura") or "",
                    inv.get("proveedor_nombre") or "",
                    inv.get("nif") or "",
                    cat_map.get(cat_code, cat_code),
                    TIPOS_DOCUMENTO.get(doc_code, doc_code),
                    base_val,
                    iva_val,
                    re_val,
                    irpf_val,
                    tot_val,
                    inv.get("moneda") or "EUR",
                    inv.get("estado_revision") or "",
                    inv.get("estado_pago") or "",
                    round(float(inv.get("importe_conciliado") or 0.0), 2),
                    round(float(inv.get("importe_pendiente") or 0.0), 2),
                    round(float(inv.get("nivel_confianza") or 0.0) * 100, 1),
                    inv.get("ruta_archivo") or "",
                ]
            )

        # Fila de totales
        ws.append(
            [
                "",
                "",
                "",
                "",
                "TOTALES",
                "",
                "",
                "",
                round(total_base, 2),
                round(total_iva, 2),
                round(total_re, 2),
                round(total_irpf, 2),
                round(total_general, 2),
                "EUR",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
        last_row = ws.max_row
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=last_row, column=col_idx)
            cell.font = Font(bold=True)

        self._auto_fit_columns(ws)

    def _build_monthly_and_category_sheet(self, ws: Any, metrics: Dict[str, Any]) -> None:
        ws.append(["RESUMEN DE GASTO POR MES"])
        ws.cell(row=1, column=1).font = Font(bold=True, size=12)
        ws.append(["Mes", "Nº Facturas", "Base Imponible (€)", "IVA (€)", "Total (€)"])
        self._style_header_row(ws, 5, row_idx=2)

        for m in metrics.get("gasto_por_mes", []):
            ws.append(
                [
                    m.get("mes"),
                    m.get("cantidad"),
                    m.get("base_imponible"),
                    m.get("iva"),
                    m.get("total"),
                ]
            )

        ws.append([])
        start_cat_row = ws.max_row + 1
        ws.append(["RESUMEN DE GASTO POR CATEGORÍA"])
        ws.cell(row=start_cat_row, column=1).font = Font(bold=True, size=12)
        ws.append(["Categoría", "Nº Facturas", "Total (€)"])
        self._style_header_row(ws, 3, row_idx=start_cat_row + 1)

        cat_map = {c["codigo"]: c["nombre"] for c in CATEGORIAS_INICIALES}
        for c in metrics.get("gasto_por_categoria", []):
            code = c.get("categoria") or "otros"
            ws.append([cat_map.get(code, code), c.get("cantidad"), c.get("total")])

        self._auto_fit_columns(ws)

    def _build_suppliers_sheet(self, ws: Any, metrics: Dict[str, Any]) -> None:
        headers = ["Proveedor", "NIF/CIF", "Nº Facturas", "Total Facturado (€)"]
        ws.append(headers)
        self._style_header_row(ws, len(headers))
        for p in metrics.get("gasto_por_proveedor", []):
            ws.append([p.get("proveedor"), p.get("nif"), p.get("cantidad"), p.get("total")])
        self._auto_fit_columns(ws)

    def _build_reconciliation_sheet(self, ws: Any, transactions: list[Dict[str, Any]]) -> None:
        headers = [
            "ID Mov.",
            "Fecha",
            "Fecha Valor",
            "Banco",
            "IBAN / Cuenta",
            "Concepto",
            "Importe (€)",
            "Estado Conciliación",
            "Diferencia Pendiente (€)",
            "Facturas Vinculadas",
            "Notas",
        ]
        ws.append(headers)
        self._style_header_row(ws, len(headers))

        for tx in transactions:
            links = tx.get("vinculos") or []
            links_desc = ", ".join(
                f"{l.get('proveedor_nombre', '')} #{l.get('numero_factura', '')} ({l.get('importe_asignado', 0):.2f}€)"
                for l in links
            )
            ws.append(
                [
                    tx.get("id"),
                    tx.get("fecha"),
                    tx.get("fecha_valor") or "",
                    tx.get("banco") or "",
                    tx.get("cuenta_iban") or "",
                    tx.get("concepto") or "",
                    round(float(tx.get("importe") or 0.0), 2),
                    tx.get("estado_conciliacion") or "",
                    round(float(tx.get("diferencia_pendiente") or 0.0), 2),
                    links_desc,
                    tx.get("notas") or "",
                ]
            )
        self._auto_fit_columns(ws)

    @staticmethod
    def _style_header_row(ws: Any, num_cols: int, row_idx: int = 1) -> None:
        fill = PatternFill(start_color="0F766E", end_color="0F766E", fill_type="solid")
        font = Font(color="FFFFFF", bold=True)
        thin_border = Border(
            bottom=Side(style="thin", color="CBD5E1"),
        )
        for col in range(1, num_cols + 1):
            cell = ws.cell(row=row_idx, column=col)
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

    @staticmethod
    def _auto_fit_columns(ws: Any) -> None:
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val = str(cell.value or "")
                if len(val) > max_len:
                    max_len = len(val)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 48)
