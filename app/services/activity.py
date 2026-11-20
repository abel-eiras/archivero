"""Registro de actividad: estado de salud del programa ("¿está funcionando?") y avisos diarios de vencimientos."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from app.db.database import Database

logger = logging.getLogger(__name__)

HEARTBEAT_SETTING = "poller_latido"
DUE_NOTICE_SETTING = "ultimo_aviso_vencimientos"
LLM_ERRORS_THRESHOLD = 3


def fmt_eur(value: float) -> str:
    """1234.5 -> '1.234,50 €' (formato español sin depender del locale del sistema)."""
    txt = f"{abs(float(value or 0.0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-{txt} €" if float(value or 0.0) < 0 else f"{txt} €"


def _parse_iso_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def stale_threshold_minutes(db: Database) -> int:
    """Un buzón se considera 'parado' si no se revisa bien en max(3 × intervalo, 60) minutos."""
    try:
        interval = int(db.get_setting("intervalo_correo_minutos", 15) or 15)
    except (TypeError, ValueError):
        interval = 15
    return max(3 * interval, 60)


def record_heartbeat(db: Database) -> None:
    """El vigilante de correo deja constancia de que sigue vivo en cada vuelta."""
    try:
        db.set_settings({HEARTBEAT_SETTING: dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()})
    except Exception as exc:  # pragma: no cover - defensivo
        logger.warning("No se pudo registrar el latido del vigilante de correo: %s", exc)


def compute_health(db: Database, poller_running: Optional[bool] = None) -> Dict[str, Any]:
    """
    Resume si el programa está funcionando:
    - Última revisión correcta de cada buzón activo (aviso si lleva demasiado sin revisarse).
    - Si el vigilante de correo en segundo plano está en marcha y su último latido.
    - Fallos de la IA en las últimas 24 h (aviso a partir de 3).
    """
    now = dt.datetime.now(dt.timezone.utc)
    threshold = stale_threshold_minutes(db)
    problemas: List[Dict[str, str]] = []
    buzones: List[Dict[str, Any]] = []

    accounts = db.list_mail_accounts(active_only=True)
    for acc in accounts:
        last_ok = _parse_iso_datetime(acc.get("ultima_sincronizacion_ok"))
        minutes = int((now - last_ok).total_seconds() // 60) if last_ok else None
        estado = "ok"
        nombre = acc.get("nombre") or acc.get("email")
        try:
            acc_interval = int(acc.get("intervalo_minutos") or 0)
        except (TypeError, ValueError):
            acc_interval = 0
        acc_threshold = max(3 * acc_interval, 60) if acc_interval > 0 else threshold
        connection_failed = (acc.get("ultima_sincronizacion_ok") or "") != (acc.get("ultima_sincronizacion") or "")
        if acc.get("ultimo_error") and connection_failed:
            estado = "error"
            problemas.append(
                {
                    "nivel": "error",
                    "mensaje": f"No se puede entrar en el correo «{nombre}»: {acc['ultimo_error']}"[:400],
                }
            )
        elif acc.get("ultimo_error"):
            estado = "aviso"
            problemas.append(
                {
                    "nivel": "aviso",
                    "mensaje": (
                        f"Correo «{nombre}»: {acc['ultimo_error']} Puedes reintentarlos desde aquí o desde Configuración."
                    )[:400],
                }
            )
        elif minutes is None:
            estado = "aviso"
            problemas.append({"nivel": "aviso", "mensaje": f"El correo «{nombre}» todavía no se ha revisado nunca."})
        elif minutes > acc_threshold:
            estado = "aviso"
            problemas.append(
                {
                    "nivel": "aviso",
                    "mensaje": f"El correo «{nombre}» lleva {minutes} minutos sin revisarse correctamente.",
                }
            )
        buzones.append(
            {
                "id": acc["id"],
                "nombre": acc.get("nombre") or "",
                "email": acc.get("email") or "",
                "ultima_revision_ok": acc.get("ultima_sincronizacion_ok"),
                "ultimo_intento": acc.get("ultima_sincronizacion"),
                "ultimo_error": acc.get("ultimo_error"),
                "mensajes_con_error": acc.get("mensajes_con_error", 0),
                "minutos_desde_revision": minutes,
                "estado": estado,
            }
        )

    poller_enabled = bool(db.get_setting("poller_activo", True))
    heartbeat = _parse_iso_datetime(db.get_setting(HEARTBEAT_SETTING, None))
    heartbeat_minutes = int((now - heartbeat).total_seconds() // 60) if heartbeat else None
    if accounts and poller_enabled:
        if poller_running is False:
            problemas.append(
                {
                    "nivel": "aviso",
                    "mensaje": "La revisión automática del correo no está en marcha. Cierra y vuelve a abrir Archivero.",
                }
            )
        elif heartbeat_minutes is not None and heartbeat_minutes > threshold:
            problemas.append(
                {
                    "nivel": "aviso",
                    "mensaje": f"La revisión automática del correo no da señales desde hace {heartbeat_minutes} minutos.",
                }
            )
    elif accounts and not poller_enabled:
        problemas.append({"nivel": "aviso", "mensaje": "La revisión automática del correo está desactivada."})

    errores_ia = db.count_recent_events("error_ia", hours=24)
    if errores_ia >= LLM_ERRORS_THRESHOLD:
        problemas.append(
            {
                "nivel": "aviso",
                "mensaje": (
                    f"La IA ha fallado {errores_ia} veces en las últimas 24 horas y se usó el lector básico. "
                    "Comprueba la clave en Configuración."
                ),
            }
        )

    if any(p["nivel"] == "error" for p in problemas):
        estado_global = "error"
    elif problemas:
        estado_global = "aviso"
    else:
        estado_global = "ok"

    return {
        "estado": estado_global,
        "mensaje": "El programa está funcionando" if estado_global == "ok" else "Hay cosas que revisar",
        "problemas": problemas,
        "buzones": buzones,
        "vigilante": {
            "activado": poller_enabled,
            "en_marcha": poller_running,
            "ultimo_latido": heartbeat.isoformat() if heartbeat else None,
            "minutos_desde_latido": heartbeat_minutes,
        },
        "errores_ia_24h": errores_ia,
        "umbral_minutos": threshold,
        "generado_en": now.replace(microsecond=0).isoformat(),
    }


def generate_due_date_notice(db: Database, today: Optional[dt.date] = None) -> Optional[int]:
    """
    Una vez al día crea UN aviso resumen con las facturas sin pagar vencidas o que vencen
    en los próximos 3 días (solo las que traen fecha de vencimiento). Devuelve el id del aviso o None.
    """
    today = today or dt.date.today()
    today_iso = today.isoformat()
    try:
        if db.get_setting(DUE_NOTICE_SETTING, "") == today_iso:
            return None
        limit = today + dt.timedelta(days=3)
        vencidas: List[Dict[str, Any]] = []
        proximas: List[Dict[str, Any]] = []
        for inv in db.list_open_payables():
            if inv.get("tipo_documento") == "abono":
                continue
            due = _parse_date(inv.get("fecha_vencimiento"))
            if not due or due > limit:
                continue
            pending = round(float(inv.get("total") or 0.0) - float(inv.get("importe_conciliado") or 0.0), 2)
            if pending < 0.01:
                continue
            item = {"id": inv["id"], "importe": pending, "fecha_vencimiento": due.isoformat()}
            (vencidas if due < today else proximas).append(item)

        db.set_settings({DUE_NOTICE_SETTING: today_iso})
        if not vencidas and not proximas:
            return None

        partes = []
        if vencidas:
            partes.append(
                f"{len(vencidas)} factura(s) vencida(s) sin pagar ({fmt_eur(sum(i['importe'] for i in vencidas))})"
            )
        if proximas:
            partes.append(
                f"{len(proximas)} que vence(n) en los próximos 3 días ({fmt_eur(sum(i['importe'] for i in proximas))})"
            )
        mensaje = "Pagos a vigilar: " + " y ".join(partes) + ". Míralo en el Resumen General."
        return db.log_event(
            tipo="vencimiento",
            nivel="aviso",
            mensaje=mensaje,
            detalle={"fecha": today_iso, "vencidas": vencidas[:50], "proximas": proximas[:50]},
        )
    except Exception as exc:
        logger.warning("No se pudo generar el aviso diario de vencimientos: %s", exc)
        return None
