"""Previsión de liquidez: saldo del último extracto frente a los pagos pendientes a proveedores."""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Dict, List, Optional

from app.db.database import Database

DEFAULT_TERM_DAYS = 30
BALANCE_STALE_DAYS = 7
WEEKS_IN_SERIES = 13

# (clave, etiqueta, día inicial, día final) relativos a hoy; el tramo 'vencido' es todo lo anterior a hoy.
BUCKETS = [
    ("vencido", "Vencido", None, -1),
    ("proximos_7", "Próximos 7 días", 0, 7),
    ("dias_8_30", "De 8 a 30 días", 8, 30),
    ("dias_31_60", "De 31 a 60 días", 31, 60),
    ("dias_61_90", "De 61 a 90 días", 61, 90),
]


def _parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def default_term_days(db: Database) -> int:
    try:
        days = int(db.get_setting("plazo_pago_defecto_dias", DEFAULT_TERM_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_TERM_DAYS
    return days if 0 <= days <= 365 else DEFAULT_TERM_DAYS


def learned_terms(db: Database) -> Dict[int, Dict[str, Any]]:
    """Mediana de (vencimiento − emisión) de las facturas de cada proveedor que traen ambas fechas."""
    by_supplier: Dict[int, List[int]] = {}
    for row in db.list_invoice_date_pairs():
        emision = _parse_date(row.get("fecha_emision"))
        venc = _parse_date(row.get("fecha_vencimiento"))
        if not emision or not venc:
            continue
        days = (venc - emision).days
        if 0 <= days <= 365:
            by_supplier.setdefault(int(row["proveedor_id"]), []).append(days)
    return {
        sid: {"dias": int(round(statistics.median(values))), "muestras": len(values)}
        for sid, values in by_supplier.items()
        if values
    }


def supplier_payment_terms(db: Database) -> Dict[str, Any]:
    """Plazo de pago efectivo de cada proveedor: el fijado a mano, el aprendido o el general."""
    default_days = default_term_days(db)
    learned = learned_terms(db)
    items = []
    for sup in db.list_suppliers():
        sid = int(sup["id"])
        explicit = sup.get("plazo_pago_dias")
        learned_info = learned.get(sid)
        if explicit is not None:
            efectivo, origen = int(explicit), "fijado"
        elif learned_info:
            efectivo, origen = learned_info["dias"], "aprendido"
        else:
            efectivo, origen = default_days, "general"
        items.append(
            {
                "proveedor_id": sid,
                "nombre": sup.get("nombre") or "",
                "nif": sup.get("nif") or "",
                "total_facturas": int(sup.get("total_facturas") or 0),
                "plazo_pago_dias": explicit,
                "plazo_aprendido": learned_info["dias"] if learned_info else None,
                "muestras_aprendidas": learned_info["muestras"] if learned_info else 0,
                "plazo_efectivo": efectivo,
                "origen": origen,
            }
        )
    return {"plazo_defecto": default_days, "items": items}


def _pending_amount(inv: Dict[str, Any]) -> float:
    """Importe que falta por pagar (total − pagos confirmados). Los abonos cuentan en negativo."""
    pending = round(float(inv.get("total") or 0.0) - float(inv.get("importe_conciliado") or 0.0), 2)
    if inv.get("tipo_documento") == "abono":
        return -abs(pending)
    return pending if pending > 0 else 0.0


def compute_liquidity(db: Database, horizonte: int = 90, today: Optional[dt.date] = None) -> Dict[str, Any]:
    today = today or dt.date.today()
    horizonte = max(7, min(int(horizonte or 90), 365))

    # 1. Saldo actual: suma del último saldo de cada cuenta
    cuentas = []
    saldo_actual: Optional[float] = None
    saldo_antiguo = False
    for b in db.latest_bank_balances():
        fecha = _parse_date(b.get("fecha_saldo"))
        dias = (today - fecha).days if fecha else None
        antiguo = dias is None or dias > BALANCE_STALE_DAYS
        saldo_antiguo = saldo_antiguo or antiguo
        cuentas.append(
            {
                "banco": b.get("banco") or "",
                "cuenta": b.get("cuenta") or "",
                "fecha_saldo": b.get("fecha_saldo"),
                "saldo": round(float(b.get("saldo") or 0.0), 2),
                "dias_antiguedad": dias,
                "antiguo": antiguo,
            }
        )
        saldo_actual = round((saldo_actual or 0.0) + float(b.get("saldo") or 0.0), 2)

    # 2. Pagos pendientes con su fecha de vencimiento (real o estimada por proveedor)
    terms = {t["proveedor_id"]: t["plazo_efectivo"] for t in supplier_payment_terms(db)["items"]}
    default_days = default_term_days(db)
    pagos: List[Dict[str, Any]] = []
    for inv in db.list_open_payables():
        pending = _pending_amount(inv)
        if abs(pending) < 0.01:
            continue
        due = _parse_date(inv.get("fecha_vencimiento"))
        estimado = False
        if not due:
            estimado = True
            emision = _parse_date(inv.get("fecha_emision"))
            term = terms.get(inv.get("proveedor_id"), default_days)
            due = emision + dt.timedelta(days=term) if emision else today
        dias = (due - today).days
        if dias > horizonte:
            continue
        pagos.append(
            {
                "factura_id": inv["id"],
                "fecha": due.isoformat(),
                "dias": dias,
                "vencida": dias < 0,
                "proveedor": inv.get("proveedor_nombre") or "",
                "numero_factura": inv.get("numero_factura") or "",
                "tipo_documento": inv.get("tipo_documento") or "",
                "importe_pendiente": pending,
                "estimado": estimado,
            }
        )
    pagos.sort(key=lambda p: (p["fecha"], p["proveedor"], p["factura_id"]))

    # 3. Tramos con saldo proyectado tras los pagos acumulados
    tramos = []
    acumulado = 0.0
    for key, label, start, end in BUCKETS:
        if start is not None and start > horizonte:
            break
        sel = [
            p for p in pagos
            if (start is None or p["dias"] >= start) and p["dias"] <= min(end, horizonte)
        ]
        importe = round(sum(p["importe_pendiente"] for p in sel), 2)
        acumulado = round(acumulado + importe, 2)
        tramos.append(
            {
                "clave": key,
                "etiqueta": label,
                "importe": importe,
                "facturas": len(sel),
                "acumulado": acumulado,
                "saldo_proyectado": round(saldo_actual - acumulado, 2) if saldo_actual is not None else None,
            }
        )

    # 4. Serie semanal (13 semanas): lo vencido se supone pagado en la primera semana
    serie = []
    acumulado = round(sum(p["importe_pendiente"] for p in pagos if p["dias"] < 0), 2)
    for week in range(WEEKS_IN_SERIES):
        desde = today + dt.timedelta(days=7 * week)
        hasta = desde + dt.timedelta(days=6)
        semana = round(
            sum(p["importe_pendiente"] for p in pagos if 7 * week <= p["dias"] <= 7 * week + 6), 2
        )
        if week == 0:
            semana = round(semana + acumulado, 2)
            acumulado = semana
        else:
            acumulado = round(acumulado + semana, 2)
        serie.append(
            {
                "semana": week + 1,
                "desde": desde.isoformat(),
                "hasta": hasta.isoformat(),
                "pagos": semana,
                "pagos_acumulados": acumulado,
                "saldo_proyectado": round(saldo_actual - acumulado, 2) if saldo_actual is not None else None,
            }
        )

    def _sum_until(day: int) -> float:
        return round(sum(p["importe_pendiente"] for p in pagos if p["dias"] <= day), 2)

    vencido = round(sum(p["importe_pendiente"] for p in pagos if p["dias"] < 0), 2)
    prox_7 = round(sum(p["importe_pendiente"] for p in pagos if 0 <= p["dias"] <= 7), 2)
    prox_30 = round(sum(p["importe_pendiente"] for p in pagos if 0 <= p["dias"] <= 30), 2)
    saldo_30 = round(saldo_actual - _sum_until(30), 2) if saldo_actual is not None else None
    saldo_90 = round(saldo_actual - _sum_until(90), 2) if saldo_actual is not None else None

    # 5. Nivel de alerta
    motivos: List[str] = []
    if saldo_actual is None:
        alerta = "ambar"
        motivos.append("Aún no has importado ningún extracto con saldo.")
    elif saldo_30 is not None and saldo_30 < 0:
        alerta = "rojo"
        motivos.append("Con los pagos de los próximos 30 días el saldo quedaría en negativo.")
    else:
        alerta = "verde"
        if saldo_90 is not None and saldo_90 < 0:
            alerta = "ambar"
            motivos.append("Con los pagos de los próximos 90 días el saldo quedaría en negativo.")
        if saldo_antiguo:
            alerta = "ambar"
            motivos.append(f"El saldo del banco tiene más de {BALANCE_STALE_DAYS} días: importa un extracto reciente.")

    return {
        "fecha_referencia": today.isoformat(),
        "horizonte_dias": horizonte,
        "saldo_actual": saldo_actual,
        "saldo_disponible": saldo_actual is not None,
        "saldo_antiguo": saldo_antiguo,
        "cuentas": cuentas,
        "resumen": {
            "vencido": vencido,
            "proximos_7": prox_7,
            "proximos_30": prox_30,
            "saldo_previsto_30": saldo_30,
            "saldo_previsto_90": saldo_90,
            "facturas_estimadas": sum(1 for p in pagos if p["estimado"]),
        },
        "tramos": tramos,
        "serie_semanal": serie,
        "pagos": pagos,
        "alerta": alerta,
        "motivos": motivos,
    }
