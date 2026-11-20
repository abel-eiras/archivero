"""Exportador a Excel (.xlsx) de contabilidad básica generado dinámicamente desde SQLite."""

from __future__ import annotations

import datetime as dt
import io
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config import CATEGORIAS_INICIALES, TIPOS_DOCUMENTO
from app.db.database import Database


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell_value(value: Any) -> Any:
    """
    Sanea textos procedentes de facturas/extractos antes de escribirlos en Excel:
    - elimina caracteres de control ilegales en XML (si no, openpyxl aborta la exportación);
    - neutraliza la inyección de fórmulas anteponiendo un apóstrofo a los textos que empiezan
      por = + - @ (los números, incluidos los negativos, se dejan como números).
    """
    if not isinstance(value, str):
        return value
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", value)
    if cleaned.startswith(_FORMULA_PREFIXES):
        cleaned = "'" + cleaned
    return cleaned


def _safe_row(values: list) -> list:
    return [_safe_cell_value(v) for v in values]


DATE_FORMAT = "dd/mm/yyyy"
MONEY_FORMAT = "#,##0.00 €"
IVA_RATES = (21.0, 10.0, 4.0, 0.0)
RE_RATES = (5.2, 1.4, 0.5)
RATE_TOLERANCE = 0.35  # puntos porcentuales para deducir el tipo a partir de base y cuota
SIN_DESGLOSE = "mixto/sin desglose"


def _to_date(value: Any) -> Any:
    """Convierte 'YYYY-MM-DD' en una fecha real de Excel; si no se puede, deja el texto."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)


def _num(value: Any) -> float:
    try:
        f = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _match_rate(rate: float, options: tuple) -> Optional[float]:
    for opt in options:
        if abs(rate - opt) <= RATE_TOLERANCE:
            return opt
    return None


def tax_breakdown(inv: Dict[str, Any]) -> Dict[str, Any]:
    """
    Desglose de IVA y recargo de equivalencia por tipo de una factura.
    1) Usa datos_extra.desglose_iva si la IA lo ha leído.
    2) Si no, deduce un único tipo cuando cuota/base encaja (con tolerancia) en 21, 10, 4 o 0 %.
    3) Si no encaja, la línea queda como «mixto/sin desglose».
    Devuelve {"origen": str, "lineas": [{tipo_iva, base, cuota, tipo_re, cuota_re}]} con importes en positivo
    tal como vienen en la factura (el signo de los abonos se aplica fuera).
    """
    extra = inv.get("datos_extra") or {}
    base = _num(inv.get("base_imponible"))
    iva = _num(inv.get("iva"))
    re_total = _num(extra.get("recargo_equivalencia"))

    desglose = extra.get("desglose_iva")
    lineas: List[Dict[str, Any]] = []
    if isinstance(desglose, list) and desglose:
        for d in desglose:
            if not isinstance(d, dict):
                continue
            l_base = _num(d.get("base"))
            l_cuota = _num(d.get("cuota"))
            tipo = _match_rate(_num(d.get("tipo")), IVA_RATES) if d.get("tipo") is not None else None
            l_re = _num(d.get("recargo_cuota"))
            tipo_re = _match_rate(_num(d.get("recargo_tipo")), RE_RATES) if l_re else None
            if tipo_re is None and l_re and l_base:
                tipo_re = _match_rate(abs(l_re / l_base) * 100, RE_RATES)
            if l_base or l_cuota or l_re:
                lineas.append({"tipo_iva": tipo, "base": l_base, "cuota": l_cuota, "tipo_re": tipo_re, "cuota_re": l_re})
        if lineas:
            return {"origen": "por tipos (leído de la factura)", "lineas": lineas}

    if abs(base) < 0.005:
        tipo = 0.0 if abs(iva) < 0.005 else None
    else:
        tipo = _match_rate(abs(iva / base) * 100, IVA_RATES)
    tipo_re = None
    if re_total and abs(base) >= 0.005:
        tipo_re = _match_rate(abs(re_total / base) * 100, RE_RATES)
    linea = {"tipo_iva": tipo, "base": base, "cuota": iva, "tipo_re": tipo_re, "cuota_re": re_total}
    origen = "deducido de base y cuota" if tipo is not None else SIN_DESGLOSE
    return {"origen": origen, "lineas": [linea]}


def _rate_label(rate: float) -> str:
    return f"{rate:g}".replace(".", ",")


class AccountingExcelExporter:
    """
    Genera el libro Excel de contabilidad básica a partir de SQLite (Sección 7 y 9).
    Nunca utiliza el Excel como base de datos: SQLite es la única fuente de verdad.
    Todas las hojas respetan los filtros elegidos (mes, categoría, estado de revisión).
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
        default_sheet = wb.active

        filters = dict(mes=mes or None, categoria=categoria or None, estado_revision=estado_revision or None)
        # Sin límites: el Excel incluye todas las facturas que cumplen los filtros.
        invoices = self.db.list_invoices(limit=None, tipo_documento="_sin_albaranes", **filters)
        albaranes = self.db.list_invoices(limit=None, tipo_documento="albaran", **filters)
        transactions = self.db.list_bank_transactions(limit=None, mes=mes or None)

        self._build_invoices_sheet(wb.create_sheet("Facturas Recibidas"), invoices)
        self._build_vat_book_sheet(wb.create_sheet("Libro registro recibidas"), invoices)
        self._build_monthly_and_category_sheet(wb.create_sheet("Resumen Mensual y Categorías"), invoices)
        self._build_suppliers_sheet(wb.create_sheet("Por Proveedor"), invoices)
        self._build_reconciliation_sheet(wb.create_sheet("Conciliación Bancaria"), transactions)
        self._build_delivery_notes_sheet(wb.create_sheet("Albaranes"), albaranes)

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

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    @staticmethod
    def _sign(inv: Dict[str, Any]) -> float:
        """Los abonos restan: se muestran siempre en negativo."""
        return -1.0 if (inv.get("tipo_documento") == "abono") else 1.0

    @classmethod
    def _signed(cls, inv: Dict[str, Any], value: float) -> float:
        value = round(_num(value), 2)
        return -abs(value) if cls._sign(inv) < 0 else value

    @staticmethod
    def _format_row(ws: Any, row_idx: int, date_cols: tuple = (), money_cols: tuple = ()) -> None:
        for c in date_cols:
            cell = ws.cell(row=row_idx, column=c)
            if isinstance(cell.value, dt.date):
                cell.number_format = DATE_FORMAT
        for c in money_cols:
            cell = ws.cell(row=row_idx, column=c)
            if isinstance(cell.value, (int, float)):
                cell.number_format = MONEY_FORMAT

    def _totals_row(self, ws: Any, label_col: int, label: str, sums: Dict[int, float], width: int) -> None:
        row = [""] * width
        row[label_col - 1] = label
        for col, val in sums.items():
            row[col - 1] = round(val, 2)
        ws.append(row)
        last = ws.max_row
        for col_idx in range(1, width + 1):
            ws.cell(row=last, column=col_idx).font = Font(bold=True)
        self._format_row(ws, last, money_cols=tuple(sums.keys()))

    # ------------------------------------------------------------------
    # Hojas
    # ------------------------------------------------------------------

    def _build_invoices_sheet(self, ws: Any, invoices: List[Dict[str, Any]]) -> None:
        rate_headers: List[str] = []
        for r in IVA_RATES:
            if r == 0.0:
                rate_headers.append("Base exenta / 0% (€)")
            else:
                rate_headers += [f"Base {_rate_label(r)}% (€)", f"IVA {_rate_label(r)}% (€)"]
        rate_headers += ["Base sin desglose (€)", "IVA sin desglose (€)"]
        rate_headers += [f"Recargo {_rate_label(r)}% (€)" for r in RE_RATES] + ["Recargo sin desglose (€)"]
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
            *rate_headers,
            "Desglose IVA",
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
        n_rate = len(rate_headers)
        money_cols = tuple(range(9, 14 + n_rate)) + (18 + n_rate, 19 + n_rate)
        sums: Dict[int, float] = {c: 0.0 for c in range(9, 14 + n_rate)}

        cat_map = {c["codigo"]: c["nombre"] for c in CATEGORIAS_INICIALES}
        for inv in invoices:
            extra = inv.get("datos_extra") or {}
            sg = lambda v: self._signed(inv, v)  # noqa: E731
            desglose = tax_breakdown(inv)
            buckets: Dict[str, float] = {}
            for linea in desglose["lineas"]:
                t = linea["tipo_iva"]
                if t is None:
                    buckets["base_x"] = buckets.get("base_x", 0.0) + linea["base"]
                    buckets["iva_x"] = buckets.get("iva_x", 0.0) + linea["cuota"]
                else:
                    buckets[f"base_{t}"] = buckets.get(f"base_{t}", 0.0) + linea["base"]
                    buckets[f"iva_{t}"] = buckets.get(f"iva_{t}", 0.0) + linea["cuota"]
                if linea["cuota_re"]:
                    key = f"re_{linea['tipo_re']}" if linea["tipo_re"] is not None else "re_x"
                    buckets[key] = buckets.get(key, 0.0) + linea["cuota_re"]
            rate_values: List[float] = []
            for r in IVA_RATES:
                if r == 0.0:
                    rate_values.append(sg(buckets.get(f"base_{r}", 0.0)))
                else:
                    rate_values += [sg(buckets.get(f"base_{r}", 0.0)), sg(buckets.get(f"iva_{r}", 0.0))]
            rate_values += [sg(buckets.get("base_x", 0.0)), sg(buckets.get("iva_x", 0.0))]
            rate_values += [sg(buckets.get(f"re_{r}", 0.0)) for r in RE_RATES] + [sg(buckets.get("re_x", 0.0))]

            cat_code = inv.get("categoria") or "otros"
            doc_code = inv.get("tipo_documento") or "otros"
            main_values = [
                sg(inv.get("base_imponible")),
                sg(inv.get("iva")),
                sg(extra.get("recargo_equivalencia")),
                sg(extra.get("irpf")),
                sg(inv.get("total")),
            ]
            ws.append(_safe_row(
                [
                    inv.get("id"),
                    _to_date(inv.get("fecha_emision")),
                    _to_date(inv.get("fecha_vencimiento")),
                    inv.get("numero_factura") or "",
                    inv.get("proveedor_nombre") or "",
                    inv.get("nif") or "",
                    cat_map.get(cat_code, cat_code),
                    TIPOS_DOCUMENTO.get(doc_code, doc_code),
                    *main_values,
                    *rate_values,
                    desglose["origen"],
                    inv.get("moneda") or "EUR",
                    inv.get("estado_revision") or "",
                    inv.get("estado_pago") or "",
                    round(_num(inv.get("importe_conciliado")), 2),
                    sg(inv.get("importe_pendiente")),
                    round(_num(inv.get("nivel_confianza")) * 100, 1),
                    inv.get("ruta_archivo") or "",
                ]
            ))
            for offset, val in enumerate(main_values + rate_values):
                sums[9 + offset] += val
            self._format_row(ws, ws.max_row, date_cols=(2, 3), money_cols=money_cols)

        self._totals_row(ws, 5, "TOTALES", sums, len(headers))
        ws.freeze_panes = "A2"
        self._auto_fit_columns(ws)

    def _build_vat_book_sheet(self, ws: Any, invoices: List[Dict[str, Any]]) -> None:
        """Libro registro de facturas recibidas (columnas de la AEAT), una fila por tipo de IVA."""
        headers = [
            "Fecha expedición",
            "Fecha operación",
            "Serie-Número",
            "NIF emisor",
            "Nombre emisor",
            "Base imponible (€)",
            "Tipo IVA (%)",
            "Cuota IVA deducible (€)",
            "Tipo RE (%)",
            "Cuota RE (€)",
            "Total factura (€)",
        ]
        ws.append(headers)
        self._style_header_row(ws, len(headers))
        sums = {6: 0.0, 8: 0.0, 10: 0.0, 11: 0.0}
        ordered = sorted(invoices, key=lambda i: (i.get("fecha_emision") or "", i.get("id") or 0))
        for inv in ordered:
            desglose = tax_breakdown(inv)
            fecha = _to_date(inv.get("fecha_emision"))
            for idx, linea in enumerate(desglose["lineas"]):
                base = self._signed(inv, linea["base"])
                cuota = self._signed(inv, linea["cuota"])
                cuota_re = self._signed(inv, linea["cuota_re"]) if linea["cuota_re"] else 0.0
                total = self._signed(inv, inv.get("total")) if idx == 0 else None
                tipo_iva = linea["tipo_iva"] if linea["tipo_iva"] is not None else SIN_DESGLOSE
                tipo_re = linea["tipo_re"] if linea["tipo_re"] is not None else ("?" if cuota_re else None)
                ws.append(_safe_row(
                    [
                        fecha,
                        fecha,
                        inv.get("numero_factura") or "",
                        inv.get("nif") or "",
                        inv.get("proveedor_nombre") or "",
                        base,
                        tipo_iva,
                        cuota,
                        tipo_re,
                        cuota_re,
                        total,
                    ]
                ))
                sums[6] += base
                sums[8] += cuota
                sums[10] += cuota_re
                sums[11] += total or 0.0
                self._format_row(ws, ws.max_row, date_cols=(1, 2), money_cols=(6, 8, 10, 11))
        self._totals_row(ws, 5, "TOTALES", sums, len(headers))
        ws.freeze_panes = "A2"
        self._auto_fit_columns(ws)

    def _build_monthly_and_category_sheet(self, ws: Any, invoices: List[Dict[str, Any]]) -> None:
        ws.append(["RESUMEN DE GASTO POR MES"])
        ws.cell(row=1, column=1).font = Font(bold=True, size=12)
        ws.append(["Mes", "Nº Facturas", "Base Imponible (€)", "IVA (€)", "Total (€)"])
        self._style_header_row(ws, 5, row_idx=2)

        por_mes: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "base": 0.0, "iva": 0.0, "total": 0.0})
        por_cat: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "total": 0.0})
        for inv in invoices:
            mes = (inv.get("fecha_emision") or "")[:7] or "sin fecha"
            m = por_mes[mes]
            m["n"] += 1
            m["base"] += self._signed(inv, inv.get("base_imponible"))
            m["iva"] += self._signed(inv, inv.get("iva"))
            m["total"] += self._signed(inv, inv.get("total"))
            c = por_cat[inv.get("categoria") or "otros"]
            c["n"] += 1
            c["total"] += self._signed(inv, inv.get("total"))

        for mes in sorted(por_mes):
            m = por_mes[mes]
            ws.append(_safe_row([mes, m["n"], round(m["base"], 2), round(m["iva"], 2), round(m["total"], 2)]))
            self._format_row(ws, ws.max_row, money_cols=(3, 4, 5))

        ws.append([])
        start_cat_row = ws.max_row + 1
        ws.append(["RESUMEN DE GASTO POR CATEGORÍA"])
        ws.cell(row=start_cat_row, column=1).font = Font(bold=True, size=12)
        ws.append(["Categoría", "Nº Facturas", "Total (€)"])
        self._style_header_row(ws, 3, row_idx=start_cat_row + 1)

        cat_map = {c["codigo"]: c["nombre"] for c in CATEGORIAS_INICIALES}
        for code, c in sorted(por_cat.items(), key=lambda kv: -kv[1]["total"]):
            ws.append(_safe_row([cat_map.get(code, code), c["n"], round(c["total"], 2)]))
            self._format_row(ws, ws.max_row, money_cols=(3,))

        self._auto_fit_columns(ws)

    def _build_suppliers_sheet(self, ws: Any, invoices: List[Dict[str, Any]]) -> None:
        headers = ["Proveedor", "NIF/CIF", "Nº Facturas", "Total Facturado (€)"]
        ws.append(headers)
        self._style_header_row(ws, len(headers))
        por_prov: Dict[str, Dict[str, Any]] = {}
        for inv in invoices:
            name = inv.get("proveedor_nombre") or "Proveedor desconocido"
            p = por_prov.setdefault(name, {"nif": inv.get("nif") or "", "n": 0, "total": 0.0})
            p["n"] += 1
            p["total"] += self._signed(inv, inv.get("total"))
            if not p["nif"] and inv.get("nif"):
                p["nif"] = inv.get("nif")
        for name, p in sorted(por_prov.items(), key=lambda kv: -kv[1]["total"]):
            ws.append(_safe_row([name, p["nif"], p["n"], round(p["total"], 2)]))
            self._format_row(ws, ws.max_row, money_cols=(4,))
        self._auto_fit_columns(ws)

    def _build_reconciliation_sheet(self, ws: Any, transactions: List[Dict[str, Any]]) -> None:
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
                f"{l.get('proveedor_nombre', '')} #{l.get('numero_factura', '')} ({_num(l.get('importe_asignado')):.2f}€)"
                for l in links
            )
            ws.append(_safe_row(
                [
                    tx.get("id"),
                    _to_date(tx.get("fecha")),
                    _to_date(tx.get("fecha_valor")),
                    tx.get("banco") or "",
                    tx.get("cuenta_iban") or "",
                    tx.get("concepto") or "",
                    round(_num(tx.get("importe")), 2),
                    tx.get("estado_conciliacion") or "",
                    round(_num(tx.get("diferencia_pendiente")), 2),
                    links_desc,
                    tx.get("notas") or "",
                ]
            ))
            self._format_row(ws, ws.max_row, date_cols=(2, 3), money_cols=(7, 9))
        self._auto_fit_columns(ws)

    def _build_delivery_notes_sheet(self, ws: Any, albaranes: List[Dict[str, Any]]) -> None:
        """Albaranes y notas de entrega: se listan aparte porque no son facturas ni cuentan como gasto."""
        headers = ["ID", "Fecha", "Nº Albarán", "Proveedor", "NIF/CIF", "Importe indicado (€)", "Ruta Archivo PDF"]
        ws.append(headers)
        self._style_header_row(ws, len(headers))
        for inv in albaranes:
            ws.append(_safe_row(
                [
                    inv.get("id"),
                    _to_date(inv.get("fecha_emision")),
                    inv.get("numero_factura") or "",
                    inv.get("proveedor_nombre") or "",
                    inv.get("nif") or "",
                    round(_num(inv.get("total")), 2),
                    inv.get("ruta_archivo") or "",
                ]
            ))
            self._format_row(ws, ws.max_row, date_cols=(2,), money_cols=(6,))
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
