"""Importadores de extractos bancarios: Norma 43 (AEB), CAMT.053 (ISO 20022 XML) y CSV/Excel con mapeo por banco."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import xml.etree.ElementTree as ET

import openpyxl

from app.models.schemas import CSVMappingConfig

logger = logging.getLogger(__name__)


def compute_transaction_hash(
    banco: str,
    cuenta_iban: str,
    fecha: str,
    importe: float,
    concepto: str,
    referencia: str = "",
    occurrence: int = 1,
) -> str:
    """Genera un hash SHA-256 determinista del movimiento para evitar duplicados al reimportar."""
    norm_concept = re.sub(r"\s+", " ", (concepto or "").strip().upper())
    norm_ref = (referencia or "").strip().upper()
    norm_bank = (banco or "BANCO").strip().upper()
    norm_iban = re.sub(r"\s+", "", (cuenta_iban or "").upper())
    payload = f"{norm_bank}|{norm_iban}|{fecha}|{importe:.2f}|{norm_concept}|{norm_ref}|#{occurrence}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_norma43(
    content: Union[bytes, str],
    banco_default: str = "Norma43",
    origen_archivo: str = "extracto.n43",
) -> List[Dict[str, Any]]:
    """
    Parsea un fichero Norma 43 (CSB43 de la AEB) de ancho fijo.
    Soporta registros:
      - 11: Cabecera de cuenta
      - 22: Movimiento principal
      - 23: Registros complementarios de concepto (hasta 5 por movimiento)
      - 33 / 88: Final de cuenta y fichero
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
    else:
        text = content

    cuenta_actual = ""
    banco_actual = banco_default
    moneda_actual = "EUR"
    movimientos: List[Dict[str, Any]] = []
    current_tx: Optional[Dict[str, Any]] = None
    seen_counter: Dict[str, int] = {}

    def finalize_current() -> None:
        nonlocal current_tx
        if not current_tx:
            return
        concepto_limpio = re.sub(r"\s+", " ", " ".join(current_tx["conceptos"])).strip()
        current_tx["concepto"] = concepto_limpio
        del current_tx["conceptos"]

        sig_key = f"{current_tx['cuenta_iban']}|{current_tx['fecha']}|{current_tx['importe']:.2f}|{concepto_limpio}|{current_tx['referencia']}"
        seen_counter[sig_key] = seen_counter.get(sig_key, 0) + 1
        current_tx["hash_movimiento"] = compute_transaction_hash(
            banco=current_tx["banco"],
            cuenta_iban=current_tx["cuenta_iban"],
            fecha=current_tx["fecha"],
            importe=current_tx["importe"],
            concepto=concepto_limpio,
            referencia=current_tx["referencia"],
            occurrence=seen_counter[sig_key],
        )
        movimientos.append(current_tx)
        current_tx = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if len(line) < 2:
            continue
        rec_type = line[0:2]

        if rec_type == "11" and len(line) >= 20:
            finalize_current()
            entidad = line[2:6].strip()
            oficina = line[6:10].strip()
            cuenta = line[10:20].strip()
            cuenta_actual = f"{entidad}-{oficina}-{cuenta}"
            divisa_cod = line[47:50].strip() if len(line) >= 50 else "978"
            moneda_actual = "EUR" if divisa_cod in ("978", "") else divisa_cod
            nombre_titular = line[51:77].strip() if len(line) >= 77 else ""
            if banco_default == "Norma43" and entidad:
                banco_actual = f"Entidad {entidad}"

        elif rec_type == "22" and len(line) >= 42:
            finalize_current()
            f_oper = _parse_yymmdd(line[10:16])
            f_val = _parse_yymmdd(line[16:22]) or f_oper
            signo = line[27:28]  # '1' = Debe (cargo, negativo), '2' = Haber (abono, positivo)
            raw_amount = line[28:42].strip()
            amount_val = int(raw_amount) / 100.0 if raw_amount.isdigit() else 0.0
            if signo == "1":
                amount_val = -abs(amount_val)
            else:
                amount_val = abs(amount_val)

            num_doc = line[42:52].strip() if len(line) >= 52 else ""
            ref1 = line[52:64].strip() if len(line) >= 64 else ""
            ref2 = line[64:80].strip() if len(line) >= 65 else ""

            referencia = " ".join(x for x in (num_doc, ref1) if x and x != "0" * len(x)).strip()
            conceptos_iniciales = [ref2] if ref2 else []

            current_tx = {
                "banco": banco_actual,
                "cuenta_iban": cuenta_actual,
                "fecha": f_oper or dt.date.today().isoformat(),
                "fecha_valor": f_val or f_oper or dt.date.today().isoformat(),
                "importe": round(amount_val, 2),
                "moneda": moneda_actual,
                "referencia": referencia,
                "conceptos": conceptos_iniciales,
                "origen_archivo": origen_archivo,
            }

        elif rec_type == "23" and current_tx is not None:
            # Registros complementarios de concepto: posiciones 4..42 y 42..80
            c1 = line[4:42].strip() if len(line) > 4 else ""
            c2 = line[42:80].strip() if len(line) > 42 else ""
            if c1:
                current_tx["conceptos"].append(c1)
            if c2:
                current_tx["conceptos"].append(c2)

        elif rec_type in ("33", "88"):
            finalize_current()

    finalize_current()
    return movimientos


def parse_camt053(
    content: Union[bytes, str],
    banco_default: str = "CAMT.053",
    origen_archivo: str = "extracto.xml",
) -> List[Dict[str, Any]]:
    """
    Parsea un fichero XML CAMT.053 (ISO 20022 BkToCstmrStmt).
    Ignora los namespaces XML para soportar cualquier versión (camt.053.001.02 / 04 / 08).
    """
    if isinstance(content, bytes):
        xml_text = content.decode("utf-8", errors="replace")
    else:
        xml_text = content

    # Parsear respetando los namespaces (comillas simples o dobles, xsi:schemaLocation, prefijos...)
    # y después quitar el namespace de cada etiqueta para que las rutas funcionen con cualquier
    # revisión ISO 20022 (camt.053.001.02 / 04 / 08).
    root = None
    if isinstance(content, bytes):
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            root = None
    if root is None:
        root = ET.fromstring(xml_text.lstrip("\ufeff"))
    _strip_xml_namespaces(root)
    movimientos: List[Dict[str, Any]] = []
    seen_counter: Dict[str, int] = {}

    for stmt in root.findall(".//Stmt"):
        iban = _xml_text(stmt, "./Acct/Id/IBAN") or _xml_text(stmt, "./Acct/Id/Othr/Id")
        bank_name = (
            _xml_text(stmt, "./Acct/Svcr/FinInstnId/Nm")
            or _xml_text(stmt, "./Acct/Svcr/FinInstnId/BIC")
            or _xml_text(stmt, "./Acct/Svcr/FinInstnId/BICFI")
            or banco_default
        )

        for ntry in stmt.findall("./Ntry"):
            amt_el = ntry.find("./Amt")
            raw_amt = float(amt_el.text.strip()) if (amt_el is not None and amt_el.text) else 0.0
            currency = (amt_el.attrib.get("Ccy") if amt_el is not None else None) or "EUR"
            cdt_dbt = _xml_text(ntry, "./CdtDbtInd").upper()
            importe = -abs(raw_amt) if cdt_dbt == "DBIT" else abs(raw_amt)

            book_dt = (
                _xml_text(ntry, "./BookgDt/Dt")
                or _xml_text(ntry, "./BookgDt/DtTm")[:10]
                or dt.date.today().isoformat()
            )
            val_dt = (
                _xml_text(ntry, "./ValDt/Dt")
                or _xml_text(ntry, "./ValDt/DtTm")[:10]
                or book_dt
            )

            ref = (
                _xml_text(ntry, "./AcctSvcrRef")
                or _xml_text(ntry, ".//Refs/EndToEndId")
                or _xml_text(ntry, ".//Refs/TxId")
            )
            if ref == "NOTPROVIDED":
                ref = ""

            concept_parts: List[str] = []
            addtl_info = _xml_text(ntry, "./AddtlNtryInf")
            if addtl_info:
                concept_parts.append(addtl_info)

            for party_path in (
                ".//RltdPties/Cdtr/Nm",
                ".//RltdPties/Dbtr/Nm",
                ".//RltdPties/Cdtr/Pty/Nm",
                ".//RltdPties/Dbtr/Pty/Nm",
            ):
                party_nm = _xml_text(ntry, party_path)
                if party_nm and party_nm not in concept_parts:
                    concept_parts.append(party_nm)

            for ustrd in ntry.findall(".//RmtInf/Ustrd"):
                if ustrd.text and ustrd.text.strip():
                    txt = ustrd.text.strip()
                    if txt not in concept_parts:
                        concept_parts.append(txt)

            concepto = re.sub(r"\s+", " ", " - ".join(concept_parts)).strip()

            sig_key = f"{iban}|{book_dt}|{importe:.2f}|{concepto}|{ref}"
            seen_counter[sig_key] = seen_counter.get(sig_key, 0) + 1

            movimientos.append(
                {
                    "banco": bank_name,
                    "cuenta_iban": iban,
                    "fecha": book_dt[:10],
                    "fecha_valor": val_dt[:10],
                    "importe": round(importe, 2),
                    "moneda": currency,
                    "concepto": concepto,
                    "referencia": ref,
                    "hash_movimiento": compute_transaction_hash(
                        banco=bank_name,
                        cuenta_iban=iban,
                        fecha=book_dt[:10],
                        importe=importe,
                        concepto=concepto,
                        referencia=ref,
                        occurrence=seen_counter[sig_key],
                    ),
                    "origen_archivo": origen_archivo,
                }
            )

    return movimientos


def parse_csv_or_excel(
    content: Union[bytes, str],
    filename: str = "extracto.csv",
    mapping: Optional[Union[CSVMappingConfig, Dict[str, Any]]] = None,
    banco_default: str = "Banco CSV",
) -> List[Dict[str, Any]]:
    """
    Parsea un fichero CSV o Excel (.xlsx) usando el mapeo de columnas del banco
    (o autodetectando las cabeceras estándar en español si no se especifica mapeo).
    """
    cfg: Optional[CSVMappingConfig] = None
    if isinstance(mapping, CSVMappingConfig):
        cfg = mapping
    elif isinstance(mapping, dict) and mapping.get("columna_fecha"):
        cfg = CSVMappingConfig(**mapping)

    rows = _read_tabular_rows(content, filename=filename, separator=cfg.separador if cfg else None)
    if not rows:
        return []

    # Localizar la fila de cabecera real
    header_idx = 0
    for idx, row in enumerate(rows[:10]):
        joined = " ".join(str(c).lower() for c in row if c is not None)
        if any(w in joined for w in ("fecha", "date", "concepto", "descripci", "importe", "amount")):
            header_idx = idx
            break

    headers = [str(c or "").strip() for c in rows[header_idx]]
    data_rows = rows[header_idx + 1 :]

    banco = (cfg.banco if cfg and cfg.banco else banco_default).strip()
    col_fecha = _find_column_index(headers, cfg.columna_fecha if cfg else None, ["fecha operación", "fecha operacion", "fecha", "f. operación", "date"])
    col_valor = _find_column_index(headers, cfg.columna_fecha_valor if cfg else None, ["fecha valor", "f. valor", "valor", "value date"])
    col_concepto = _find_column_index(headers, cfg.columna_concepto if cfg else None, ["concepto", "descripción", "descripcion", "movimiento", "detalle", "description"])
    col_importe = _find_column_index(headers, cfg.columna_importe if cfg else None, ["importe", "cantidad", "amount", "euros", "importe (eur)"])
    col_cargo = _find_column_index(headers, cfg.columna_cargo if cfg else None, ["cargo", "debe", "pagos", "debit"])
    col_abono = _find_column_index(headers, cfg.columna_abono if cfg else None, ["abono", "haber", "ingresos", "credit"])
    col_ref = _find_column_index(headers, cfg.columna_referencia if cfg else None, ["referencia", "documento", "ref", "nº documento"])

    if col_fecha is None or col_concepto is None or (col_importe is None and col_cargo is None):
        raise ValueError(
            f"No se pudieron mapear las columnas obligatorias (fecha, concepto, importe) en '{filename}'. "
            f"Cabeceras detectadas: {headers}"
        )

    movimientos: List[Dict[str, Any]] = []
    seen_counter: Dict[str, int] = {}

    for r in data_rows:
        if not r or all(str(cell or "").strip() == "" for cell in r):
            continue

        raw_fecha = _safe_cell(r, col_fecha)
        fecha_iso = _parse_flexible_date(raw_fecha, preferred_fmt=cfg.formato_fecha if cfg else None)
        if not fecha_iso:
            continue

        raw_val = _safe_cell(r, col_valor) if col_valor is not None else ""
        fecha_val_iso = _parse_flexible_date(raw_val) or fecha_iso
        concepto = re.sub(r"\s+", " ", _safe_cell(r, col_concepto)).strip()
        referencia = _safe_cell(r, col_ref) if col_ref is not None else ""

        if col_importe is not None and _safe_cell(r, col_importe) != "":
            importe = _amount_from_row(r, col_importe)
        else:
            cargo_val = abs(_amount_from_row(r, col_cargo)) if col_cargo is not None else 0.0
            abono_val = abs(_amount_from_row(r, col_abono)) if col_abono is not None else 0.0
            importe = round(abono_val - cargo_val, 2)

        sig_key = f"{banco}|{fecha_iso}|{importe:.2f}|{concepto}|{referencia}"
        seen_counter[sig_key] = seen_counter.get(sig_key, 0) + 1

        movimientos.append(
            {
                "banco": banco,
                "cuenta_iban": "",
                "fecha": fecha_iso,
                "fecha_valor": fecha_val_iso,
                "importe": round(importe, 2),
                "moneda": "EUR",
                "concepto": concepto,
                "referencia": referencia,
                "hash_movimiento": compute_transaction_hash(
                    banco=banco,
                    cuenta_iban="",
                    fecha=fecha_iso,
                    importe=importe,
                    concepto=concepto,
                    referencia=referencia,
                    occurrence=seen_counter[sig_key],
                ),
                "origen_archivo": filename,
            }
        )

    return movimientos


# =====================================================================
# Helpers internos de parseo bancario
# =====================================================================


def _parse_yymmdd(raw: str) -> Optional[str]:
    s = raw.strip()
    if len(s) != 6 or not s.isdigit():
        return None
    yy = int(s[0:2])
    mm = int(s[2:4])
    dd = int(s[4:6])
    year = 2000 + yy if yy < 80 else 1900 + yy
    try:
        return dt.date(year, mm, dd).isoformat()
    except ValueError:
        return None


def _strip_xml_namespaces(root: ET.Element) -> None:
    """Elimina el '{namespace}' de las etiquetas para buscar por nombre local."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _xml_text(element: ET.Element, xpath: str) -> str:
    node = element.find(xpath)
    if node is not None and node.text:
        return node.text.strip()
    return ""


def _read_tabular_rows(
    content: Union[bytes, str],
    filename: str,
    separator: Optional[str] = None,
) -> List[List[Any]]:
    if filename.lower().endswith(".xlsx"):
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
        ws = wb.active
        return [list(row) for row in ws.iter_rows(values_only=True)]

    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1", errors="replace")
    else:
        text = content

    sample = text[:2048]
    sep = separator
    if not sep:
        sep = ";" if sample.count(";") >= sample.count(",") else ","

    reader = csv.reader(io.StringIO(text), delimiter=sep)
    return [row for row in reader]


def _find_column_index(
    headers: List[str],
    configured_name: Optional[str],
    candidates: List[str],
) -> Optional[int]:
    norm_headers = [h.strip().lower() for h in headers]
    if configured_name:
        target = configured_name.strip().lower()
        for idx, h in enumerate(norm_headers):
            if h == target or target in h:
                return idx
        if configured_name.isdigit():
            col_i = int(configured_name)
            if 0 <= col_i < len(headers):
                return col_i

    for cand in candidates:
        for idx, h in enumerate(norm_headers):
            if h == cand or cand in h:
                return idx
    return None


def _safe_cell(row: List[Any], index: Optional[int]) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    val = row[index]
    if val is None:
        return ""
    if isinstance(val, (dt.date, dt.datetime)):
        return val.strftime("%Y-%m-%d")
    return str(val).strip()


def _parse_flexible_date(raw: str, preferred_fmt: Optional[str] = None) -> Optional[str]:
    s = raw.strip()
    if not s:
        return None
    formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y", "%d.%m.%Y"]
    if preferred_fmt and preferred_fmt not in formats:
        formats.insert(0, preferred_fmt)
    for fmt in formats:
        try:
            return dt.datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _amount_from_row(row: List[Any], index: Optional[int]) -> float:
    """Lee un importe de una fila: los números nativos de Excel se usan tal cual."""
    if index is not None and 0 <= index < len(row):
        val = row[index]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return round(float(val), 2)
    return _parse_amount_cell(_safe_cell(row, index))


def _parse_amount_cell(raw: str) -> float:
    """
    Convierte un importe textual a float admitiendo formatos españoles e internacionales:
    '1.234,56', '1.234' (miles), '1,234.56', '123,45-' (signo final), '(12,00)' (negativo
    contable), símbolos de moneda y espacios (incluidos los no separables).
    """
    s = str(raw or "").strip()
    for token in ("€", "EUR", "eur", "$", "\u00a0", "\u202f", " ", "+"):
        s = s.replace(token, "")
    if not s:
        return 0.0

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    if s.endswith("-"):
        negative = not negative
        s = s[:-1]
    if s.startswith("-"):
        negative = not negative
        s = s[1:]
    if not s:
        return 0.0

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if s.count(",") > 1:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif "." in s:
        # Sin coma: un punto seguido de exactamente 3 dígitos es separador de miles ('1.234', '12.345.678')
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")

    try:
        value = float(s)
    except ValueError as exc:
        raise ValueError(f"Importe no reconocido: '{raw}'") from exc
    return round(-value if negative else value, 2)
