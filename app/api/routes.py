"""Endpoints REST de FastAPI para Dashboard, Facturas, Revisión, Conciliación, Correo, Configuración y Excel."""

from __future__ import annotations

import html
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import re
import secrets

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import ValidationError

from app.config import (
    APP_VERSION,
    CATEGORIAS_INICIALES,
    LLM_PROVIDERS,
    MAX_BANK_UPLOAD_BYTES,
    MAX_PDF_UPLOAD_BYTES,
    OFFICIAL_GITHUB_REPO,
    TIPOS_DOCUMENTO,
    resolve_llm_endpoint,
    resolve_llm_model,
)
from app.db.database import NULLABLE_INVOICE_FIELDS, Database
from app.models.schemas import (
    CSVMappingConfig,
    InvoiceExtraction,
    InvoiceUpdatePayload,
    MailAccountCreate,
    ManualReconciliationRequest,
    RuleCreate,
    SettingsUpdate,
)
from app.services.activity import compute_health
from app.services.credentials import CredentialManager
from app.services.data_location import change_data_dir
from app.services.excel_exporter import AccountingExcelExporter
from app.services.liquidity import compute_liquidity, supplier_payment_terms
from app.services.llm_processor import describe_llm_error, validate_invoice_extraction
from app.services.mail_poller import MailPollerService, OAuth2MailConnector
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.reconciliation import BankReconciliationService, ReconciliationError, ReconciliationNotFound
from app.services.storage import relocate_archived_pdf, remove_empty_parents
from app.services.updater_service import UpdaterService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_credentials(request: Request) -> CredentialManager:
    return request.app.state.credentials


def get_mail_poller(request: Request) -> MailPollerService:
    return request.app.state.mail_poller


RESTART_REQUIRED_DETAIL = "Reinicia Archivero para usar la nueva carpeta de datos."


def ensure_ingestion_allowed(request: Request) -> None:
    """Tras cambiar la carpeta de datos no se aceptan datos nuevos hasta reiniciar (irían a la base antigua)."""
    if getattr(request.app.state, "restart_required", False):
        raise HTTPException(status_code=409, detail=RESTART_REQUIRED_DETAIL)


def _read_upload_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Lee un fichero subido sin pasar del tamaño máximo (devuelve max_bytes+1 bytes si lo supera)."""
    return upload.file.read(max_bytes + 1)


def _sync_invoice_archive(db: Database, invoice: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tras editar o confirmar una factura, deja su PDF en la carpeta que le corresponde (Año/Mes/Categoría/
    Proveedor) para que la carpeta de la gestoría siempre esté ordenada. Nunca pierde el archivo.
    """
    ruta = invoice.get("ruta_archivo") or ""
    if not ruta:
        return invoice
    base = Path(db.get_setting("carpeta_salida") or "archivo_facturas")
    res = relocate_archived_pdf(
        current_path=ruta,
        base_output_dir=base,
        fecha_emision=invoice.get("fecha_emision"),
        categoria=invoice.get("categoria") or "otros",
        proveedor=invoice.get("proveedor_nombre") or "",
        numero_factura=invoice.get("numero_factura") or "",
        hash_pdf=invoice.get("hash_pdf") or "",
        tipo_documento=invoice.get("tipo_documento") or "",
    )
    if res.error:
        db.log_event(
            tipo="config_cambiada",
            nivel="aviso",
            mensaje=f"Factura {invoice.get('numero_factura') or invoice['id']}: {res.message}",
            entidad="factura",
            entidad_id=invoice["id"],
        )
        return invoice
    if res.moved:
        if invoice.get("drive_file_id"):
            db.log_event(
                tipo="config_cambiada",
                nivel="aviso",
                mensaje=(
                    f"Factura {invoice.get('numero_factura') or invoice['id']}: el PDF se ha movido en la carpeta local, "
                    "pero la copia de Google Drive se queda donde estaba."
                ),
                entidad="factura",
                entidad_id=invoice["id"],
            )
        return db.update_invoice(int(invoice["id"]), {"ruta_archivo": res.new_path}) or invoice
    return invoice


# =====================================================================
# 1. Dashboard y Catálogos
# =====================================================================


@router.get("/dashboard")
def read_dashboard(db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Devuelve las métricas completas del panel principal leyendo exclusivamente de SQLite."""
    metrics = db.get_dashboard_metrics()
    settings = db.get_all_settings()
    metrics["catalogos"] = {
        "categorias": settings.get("categorias") or CATEGORIAS_INICIALES,
        "tipos_documento": TIPOS_DOCUMENTO,
        "version_app": APP_VERSION,
        "esquema_db_version": db.get_schema_version(),
    }
    return metrics


# =====================================================================
# 2. Facturas y Bandeja de Revisión
# =====================================================================


@router.get("/invoices")
def list_invoices(
    estado_revision: Optional[str] = Query(default=None),
    estado_pago: Optional[str] = Query(default=None),
    categoria: Optional[str] = Query(default=None),
    proveedor: Optional[str] = Query(default=None),
    mes: Optional[str] = Query(default=None),
    fecha_desde: Optional[str] = Query(default=None),
    fecha_hasta: Optional[str] = Query(default=None),
    busqueda: Optional[str] = Query(default=None),
    incluir_sustituidas: bool = Query(default=False),
    tipo_documento: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    filters = dict(
        incluir_sustituidas=incluir_sustituidas,
        estado_revision=estado_revision or None,
        estado_pago=estado_pago or None,
        categoria=categoria or None,
        proveedor=proveedor or None,
        mes=mes or None,
        fecha_desde=fecha_desde or None,
        fecha_hasta=fecha_hasta or None,
        busqueda=busqueda or None,
        tipo_documento=tipo_documento or None,
    )
    items = db.list_invoices(limit=limit, offset=offset, **filters)
    return {"total": db.count_invoices(**filters), "limit": limit, "offset": offset, "items": items}


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db: Database = Depends(get_db)) -> Dict[str, Any]:
    inv = db.get_invoice_by_id(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")
    return inv


@router.post("/invoices/upload")
def upload_invoice_pdfs(
    request: Request,
    files: List[UploadFile] = File(...),
    remitente: str = Form(default=""),
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """
    Sube y procesa uno o varios PDF. Es una función normal (no async) para que FastAPI la ejecute en
    un hilo aparte y no bloquee el servidor. Cada archivo se valida y procesa por separado: si uno
    falla, se indica su error y los demás siguen adelante.
    """
    ensure_ingestion_allowed(request)
    settings = db.get_all_settings()
    provider = settings.get("proveedor_modelo") or "google_ai_studio"
    llm_key = creds.get_llm_api_key(provider)
    gdrive_token = creds.get_gdrive_token()

    pipeline = InvoiceIngestionPipeline(
        db=db,
        llm_api_key=llm_key,
        gdrive_access_token=gdrive_token,
    )

    resultados: List[Dict[str, Any]] = []
    insertadas = 0
    duplicadas = 0
    errores = 0
    limite_mb = MAX_PDF_UPLOAD_BYTES // (1024 * 1024)

    for f in files:
        nombre = f.filename or "factura.pdf"
        item: Dict[str, Any] = {"filename": nombre, "status": "error", "invoice_id": None, "hash_pdf": "", "invoice": None}
        try:
            content = _read_upload_limited(f, MAX_PDF_UPLOAD_BYTES)
            if not content:
                item["detail"] = "El archivo está vacío."
            elif len(content) > MAX_PDF_UPLOAD_BYTES:
                item["detail"] = f"El archivo pasa de {limite_mb} MB; no se ha procesado."
            elif b"%PDF" not in content[:1024]:
                item["detail"] = "No es un PDF válido."
            else:
                res = pipeline.ingest_pdf(pdf_source=content, filename=nombre, sender_email=remitente)
                item.update(
                    {
                        "status": res.status,
                        "invoice_id": res.invoice_id,
                        "hash_pdf": res.hash_pdf,
                        "detail": res.detail,
                        "invoice": res.invoice,
                    }
                )
        except Exception as exc:
            logger.warning("Error procesando el PDF subido %s: %s", nombre, exc)
            item["detail"] = f"No se pudo leer este PDF: {exc}"
        if item["status"] == "insertada":
            insertadas += 1
        elif item["status"] == "duplicada":
            duplicadas += 1
        elif item["status"] == "error":
            errores += 1
        resultados.append(item)

    return {
        "procesados": len(resultados),
        "insertadas": insertadas,
        "duplicadas": duplicadas,
        "errores": errores,
        "resultados": resultados,
    }


@router.put("/invoices/{invoice_id}")
def update_or_confirm_invoice(
    invoice_id: int,
    payload: InvoiceUpdatePayload,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """
    Actualiza los datos de una factura (en la bandeja de revisión o en el listado),
    revalida la coherencia aritmética y de fechas y, si el usuario lo indica,
    aprende la regla de categoría/tipo para ese proveedor.
    """
    existing = db.get_invoice_by_id(invoice_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")

    updates = payload.model_dump(exclude_unset=True)
    aprender_regla = bool(updates.pop("aprender_regla_proveedor", False))

    merged = dict(existing)
    merged.update(
        {k: v for k, v in updates.items() if v is not None or k in NULLABLE_INVOICE_FIELDS}
    )

    # Revalidar con los nuevos importes y fechas
    ext = InvoiceExtraction(
        es_factura=True,
        proveedor=merged.get("proveedor_nombre") or "",
        nif=merged.get("nif") or "",
        numero_factura=merged.get("numero_factura") or "",
        fecha_emision=merged.get("fecha_emision"),
        fecha_vencimiento=merged.get("fecha_vencimiento"),
        base_imponible=float(merged.get("base_imponible") or 0.0),
        iva=float(merged.get("iva") or 0.0),
        total=float(merged.get("total") or 0.0),
        moneda=merged.get("moneda") or "EUR",
        tipo_documento=merged.get("tipo_documento") or "factura_mercancia",
        categoria=merged.get("categoria") or "compras_mercancia",
        nivel_confianza=1.0 if updates.get("estado_revision") == "confirmada" else float(merged.get("nivel_confianza") or 0.85),
        datos_extra=merged.get("datos_extra") or {},
    )
    umbral = float(db.get_setting("umbral_confianza", 0.85) or 0.85)
    val = validate_invoice_extraction(ext, umbral_confianza=umbral)

    if updates.get("estado_revision") == "confirmada":
        updates["nivel_confianza"] = 1.0
        updates["errores_validacion"] = val.errores
    else:
        updates["errores_validacion"] = val.errores + [f"Aviso: {w}" for w in val.advertencias]

    updated = db.update_invoice(invoice_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="No se pudo actualizar la factura.")

    archive_fields = {"proveedor_nombre", "fecha_emision", "categoria", "numero_factura", "tipo_documento", "estado_revision"}
    if archive_fields & set(updates.keys()):
        updated = _sync_invoice_archive(db, updated)

    if aprender_regla and updated.get("proveedor_nombre"):
        patron = updated.get("nif") or updated["proveedor_nombre"]
        db.create_rule(
            tipo_regla="nif_categoria",
            patron=patron,
            proveedor_id=updated.get("proveedor_id"),
            proveedor_nombre=updated["proveedor_nombre"],
            categoria=updated.get("categoria") or "",
            tipo_documento=updated.get("tipo_documento") or "",
            aprendida=True,
        )

    return updated


@router.post("/invoices/{invoice_id}/confirm")
def confirm_invoice_quick(invoice_id: int, db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Confirma directamente una factura pendiente de revisión."""
    inv = db.get_invoice_by_id(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")
    updated = db.update_invoice(
        invoice_id,
        {"estado_revision": "confirmada", "nivel_confianza": 1.0, "errores_validacion": []},
    )
    return _sync_invoice_archive(db, updated) if updated else {}


@router.delete("/invoices/{invoice_id}")
def delete_invoice_endpoint(invoice_id: int, db: Database = Depends(get_db)) -> Dict[str, Any]:
    ok = db.delete_invoice(invoice_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")
    return {"deleted": True, "id": invoice_id}


@router.get("/invoices/{invoice_id}/pdf")
def open_archived_pdf(invoice_id: int, db: Database = Depends(get_db)) -> Response:
    """
    Sirve el archivo PDF archivado para visualizarlo o descargarlo.
    Según la Sección 6: si alguien movió o renombró el archivo fuera de la app,
    el dashboard sigue funcionando y este endpoint avisa claramente de que no lo encuentra.
    """
    inv = db.get_invoice_by_id(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Factura no encontrada en la base de datos.")

    ruta = inv.get("ruta_archivo") or ""
    if not ruta or not Path(ruta).is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"El archivo PDF archivado no se encuentra en la ruta registrada ({ruta or 'sin ruta'}). "
                "Es posible que haya sido movido o renombrado manualmente en la carpeta de la gestoría. "
                "Los datos contables siguen intactos en SQLite."
            ),
        )

    file_path = Path(ruta)
    return FileResponse(
        path=str(file_path),
        media_type="application/pdf",
        filename=file_path.name,
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
    )


# =====================================================================
# 3. Conciliación Bancaria (Fase 6)
# =====================================================================


@router.get("/bank/transactions")
def list_bank_transactions(
    estado_conciliacion: Optional[str] = Query(default=None),
    banco: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    filters = dict(estado_conciliacion=estado_conciliacion or None, banco=banco or None)
    items = db.list_bank_transactions(limit=limit, offset=offset, **filters)
    return {
        "total": db.count_bank_transactions(**filters),
        "limit": limit,
        "offset": offset,
        "items": items,
        "mapeos_csv": db.get_csv_mappings(),
    }


@router.post("/bank/import")
def import_bank_statement(
    request: Request,
    file: UploadFile = File(...),
    formato: str = Form(default="auto"),
    banco: str = Form(default="Banco Principal"),
    columna_fecha: Optional[str] = Form(default=None),
    columna_concepto: Optional[str] = Form(default=None),
    columna_importe: Optional[str] = Form(default=None),
    columna_cargo: Optional[str] = Form(default=None),
    columna_abono: Optional[str] = Form(default=None),
    separador: str = Form(default=";"),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Importa un fichero de extracto bancario en formato Norma 43, CAMT.053 o CSV/Excel."""
    ensure_ingestion_allowed(request)
    content = _read_upload_limited(file, MAX_BANK_UPLOAD_BYTES)
    if not content:
        raise HTTPException(status_code=400, detail="El fichero importado está vacío.")
    if len(content) > MAX_BANK_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"El extracto pasa de {MAX_BANK_UPLOAD_BYTES // (1024 * 1024)} MB; divídelo en varios ficheros.",
        )

    csv_mapping: Optional[CSVMappingConfig] = None
    if columna_fecha and columna_concepto and (columna_importe or columna_cargo):
        csv_mapping = CSVMappingConfig(
            banco=banco,
            columna_fecha=columna_fecha,
            columna_concepto=columna_concepto,
            columna_importe=columna_importe or "",
            columna_cargo=columna_cargo,
            columna_abono=columna_abono,
            separador=separador or ";",
        )

    service = BankReconciliationService(db=db)
    try:
        result = service.import_statement(
            content=content,
            filename=file.filename or "extracto.txt",
            formato=formato,
            banco=banco,
            csv_mapping=csv_mapping,
            auto_reconcile=True,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Error al importar extracto bancario: {exc}") from exc


@router.post("/bank/auto-reconcile")
def trigger_auto_reconcile(db: Database = Depends(get_db)) -> Dict[str, Any]:
    service = BankReconciliationService(db=db)
    result = service.run_auto_reconciliation()
    if result.get("conciliados_automaticos") or result.get("propuestas_revision"):
        db.log_event(
            tipo="conciliacion",
            mensaje=(
                f"Búsqueda de pagos en el banco: {result.get('conciliados_automaticos', 0)} cuadrados solos y "
                f"{result.get('propuestas_revision', 0)} propuestas para revisar."
            ),
            detalle=result,
        )
    return result


@router.post("/bank/transactions/{movimiento_id}/confirm-proposal")
def confirm_reconciliation_proposal(
    movimiento_id: int,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    service = BankReconciliationService(db=db)
    try:
        return service.confirm_proposal(movimiento_id)
    except ReconciliationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReconciliationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bank/transactions/{movimiento_id}/reject-proposal")
def reject_reconciliation_proposal(
    movimiento_id: int,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    service = BankReconciliationService(db=db)
    try:
        return service.reject_proposal(movimiento_id)
    except ReconciliationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/bank/reconcile-manual")
def manual_reconcile_endpoint(
    payload: ManualReconciliationRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    service = BankReconciliationService(db=db)
    try:
        return service.reconcile_manual(
            movimiento_id=payload.movimiento_id,
            asignaciones=payload.asignaciones,
            notas=payload.notas,
            aprender_regla=payload.aprender_regla,
        )
    except ReconciliationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReconciliationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# =====================================================================
# 4. Correo, Reglas y Configuración
# =====================================================================


@router.get("/mail/accounts")
def get_mail_accounts(
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    accounts = db.list_mail_accounts()
    for acc in accounts:
        email_addr = acc["email"]
        if acc.get("tipo_conexion") in ("oauth_gmail", "oauth_outlook"):
            tokens = creds.get_oauth_tokens(email_addr)
            acc["credencial_configurada"] = bool(tokens.get("access_token") or tokens.get("refresh_token"))
        else:
            acc["credencial_configurada"] = bool(creds.get_mail_password(email_addr))
    return {"items": accounts}


@router.post("/mail/accounts")
def save_mail_account(
    payload: MailAccountCreate,
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    acc_id = db.upsert_mail_account(payload.model_dump())
    backend_used = "none"

    if payload.tipo_conexion in ("oauth_gmail", "oauth_outlook"):
        if payload.password_o_token or payload.oauth_refresh_token or payload.oauth_client_id:
            existing_tokens = creds.get_oauth_tokens(payload.email)
            if payload.password_o_token:
                existing_tokens["access_token"] = payload.password_o_token
            if payload.oauth_refresh_token:
                existing_tokens["refresh_token"] = payload.oauth_refresh_token
            if payload.oauth_client_id:
                existing_tokens["client_id"] = payload.oauth_client_id
            if payload.oauth_client_secret:
                existing_tokens["client_secret"] = payload.oauth_client_secret
            backend_used = creds.set_oauth_tokens(payload.email, existing_tokens)
    elif payload.password_o_token:
        backend_used = creds.set_mail_password(payload.email, payload.password_o_token)

    return {"id": acc_id, "email": payload.email, "almacen_credenciales": backend_used}


@router.delete("/mail/accounts/{account_id}")
def remove_mail_account(
    account_id: int,
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    account = next((a for a in db.list_mail_accounts() if int(a["id"]) == account_id), None)
    ok = db.delete_mail_account(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cuenta de correo no encontrada.")
    email_addr = ((account or {}).get("email") or "").lower().strip()
    if email_addr:
        # Borrar también la contraseña y los tokens guardados de esa cuenta.
        for key in (f"mail_password:{email_addr}", f"mail_oauth:{email_addr}"):
            try:
                creds.delete_secret(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo borrar el secreto %s: %s", key, exc)
    return {"deleted": True, "id": account_id}


@router.post("/mail/accounts/{account_id}/retry-errors")
def retry_mail_errors(
    account_id: int,
    request: Request,
    db: Database = Depends(get_db),
    poller: MailPollerService = Depends(get_mail_poller),
) -> Dict[str, Any]:
    """Vuelve a intentar los correos de esta cuenta cuyos adjuntos no se pudieron procesar."""
    ensure_ingestion_allowed(request)
    account = next((a for a in db.list_mail_accounts() if int(a["id"]) == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Cuenta de correo no encontrada.")
    reiniciados = db.reset_error_messages(account_id)
    resultado = poller.sync_account(account)
    return {"reintentados": reiniciados, "sincronizacion": resultado}


OAUTH_STATE_TTL_SECONDS = 15 * 60


def _oauth_states(request: Request) -> Dict[str, Dict[str, Any]]:
    states = getattr(request.app.state, "oauth_states", None)
    if states is None:
        states = {}
        request.app.state.oauth_states = states
    return states


@router.post("/mail/oauth/authorize-url")
def get_oauth_authorize_url(
    request: Request,
    payload: Dict[str, Any],
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, str]:
    """
    Prepara la dirección de autorización OAuth2. El parámetro 'state' es un código aleatorio de un solo
    uso guardado en el servidor (no el correo), para que nadie pueda colar tokens en otra cuenta.
    """
    provider = str(payload.get("provider") or "")
    client_id = str(payload.get("client_id") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if provider not in OAuth2MailConnector.PROVIDERS:
        raise HTTPException(status_code=400, detail="Proveedor OAuth desconocido.")
    if not client_id or not email:
        raise HTTPException(status_code=400, detail="Faltan el correo o el identificador de cliente (client_id).")
    tokens = creds.get_oauth_tokens(email)
    if tokens.get("client_id") != client_id:
        tokens["client_id"] = client_id
    tokens["provider"] = provider
    creds.set_oauth_tokens(email, tokens)

    states = _oauth_states(request)
    now = time.time()
    for key in [k for k, v in states.items() if now - v.get("creado", 0) > OAUTH_STATE_TTL_SECONDS]:
        states.pop(key, None)
    nonce = secrets.token_urlsafe(24)
    states[nonce] = {"email": email, "creado": now}
    url = OAuth2MailConnector.build_authorization_url(
        provider_key=provider,
        client_id=client_id,
        state=nonce,
    )
    return {"authorization_url": url}


def _oauth_result_page(titulo: str, mensaje: str, ok: bool, status_code: int = 200) -> HTMLResponse:
    color = "#15803d" if ok else "#b91c1c"
    body = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{html.escape(titulo)}</title></head>
<body style="font-family:system-ui,sans-serif;max-width:560px;margin:3rem auto;padding:0 1rem;">
<h2 style="color:{color};">{html.escape(titulo)}</h2>
<p>{html.escape(mensaje)}</p>
<p>Ya puedes cerrar esta ventana y volver a Archivero.</p>
</body></html>"""
    return HTMLResponse(content=body, status_code=status_code)


@router.get("/mail/oauth/callback")
def oauth_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> HTMLResponse:
    """Recibe la redirección OAuth2 (Gmail/Outlook), canjea el código por tokens y los guarda."""
    entry = _oauth_states(request).pop(state or "", None) if state else None
    if entry and time.time() - entry.get("creado", 0) > OAUTH_STATE_TTL_SECONDS:
        entry = None
    email_addr = (entry or {}).get("email", "")
    if state and not entry and not error:
        return _oauth_result_page(
            "Autorización caducada o no reconocida",
            "Este enlace de autorización ya no es válido. Vuelve a pulsar «Conectar» desde Archivero.",
            False,
            400,
        )
    if error:
        return _oauth_result_page("No se completó la autorización", f"El proveedor devolvió: {error}", False, 400)
    if not code or not email_addr:
        return _oauth_result_page(
            "Faltan datos de autorización",
            "La respuesta no incluye el código o la cuenta de correo. Vuelve a iniciar la conexión desde Archivero.",
            False,
            400,
        )

    tokens = creds.get_oauth_tokens(email_addr)
    provider = tokens.get("provider") or ""
    if provider not in OAuth2MailConnector.PROVIDERS:
        account = next((a for a in db.list_mail_accounts() if (a.get("email") or "").lower() == email_addr), None)
        provider = (account or {}).get("tipo_conexion") or "oauth_gmail"
    if provider not in OAuth2MailConnector.PROVIDERS:
        provider = "oauth_gmail"

    client_id = tokens.get("client_id") or ""
    if not client_id:
        return _oauth_result_page(
            "Falta el identificador de cliente",
            f"No hay un client_id guardado para {email_addr}. Guarda la cuenta con su client_id y vuelve a intentarlo.",
            False,
            400,
        )

    try:
        token_data = OAuth2MailConnector.exchange_authorization_code(
            provider_key=provider,
            code=code,
            client_id=client_id,
            client_secret=tokens.get("client_secret") or "",
        )
    except Exception as exc:
        logger.warning("Fallo canjeando el código OAuth2 de %s: %s", email_addr, exc)
        return _oauth_result_page(
            "No se pudo completar la conexión",
            "El proveedor rechazó el código de autorización. Comprueba el client_id/client_secret y vuelve a intentarlo.",
            False,
            502,
        )

    if not token_data.get("access_token"):
        return _oauth_result_page(
            "No se pudo completar la conexión",
            "El proveedor no devolvió un token de acceso.",
            False,
            502,
        )

    tokens["access_token"] = token_data["access_token"]
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    tokens["provider"] = provider
    creds.set_oauth_tokens(email_addr, tokens)
    return _oauth_result_page(
        "Cuenta de correo conectada",
        f"La cuenta {email_addr} ha quedado autorizada correctamente.",
        True,
    )


@router.post("/mail/sync")
def sync_mail_now(request: Request, poller: MailPollerService = Depends(get_mail_poller)) -> Dict[str, Any]:
    ensure_ingestion_allowed(request)
    results = poller.sync_all_active_accounts()
    return {"sincronizaciones": results}


@router.get("/rules")
def list_rules(db: Database = Depends(get_db)) -> Dict[str, Any]:
    return {"items": db.list_rules(), "proveedores": db.list_suppliers()}


@router.post("/rules")
def create_rule_endpoint(payload: RuleCreate, db: Database = Depends(get_db)) -> Dict[str, Any]:
    rule_id = db.create_rule(
        tipo_regla=payload.tipo_regla,
        patron=payload.patron,
        proveedor_id=payload.proveedor_id,
        proveedor_nombre=payload.proveedor_nombre or "",
        categoria=payload.categoria or "",
        tipo_documento=payload.tipo_documento or "",
        prioridad=payload.prioridad,
        aprendida=payload.aprendida,
        activa=payload.activa,
    )
    return {"id": rule_id, "status": "ok"}


@router.delete("/rules/{rule_id}")
def delete_rule_endpoint(rule_id: int, db: Database = Depends(get_db)) -> Dict[str, Any]:
    ok = db.delete_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Regla no encontrada.")
    return {"deleted": True, "id": rule_id}


@router.get("/settings")
def get_settings_endpoint(
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    settings = db.get_all_settings()
    provider = settings.get("proveedor_modelo") or "google_ai_studio"
    if provider != "heuristico_local":
        settings["modelo_llm"] = resolve_llm_model(provider, settings.get("modelo_llm"))
    settings["catalogo_proveedores"] = LLM_PROVIDERS
    settings["estado_secretos"] = {
        "llm_api_key_configurada": bool(creds.get_llm_api_key(provider)),
        "gdrive_token_configurado": bool(creds.get_gdrive_token()),
        "github_token_configurado": bool(creds.get_github_token()),
    }
    # Solo informativo: se cambia con POST /api/settings/data-dir
    settings["carpeta_datos"] = str(db.db_path.parent.resolve())
    return settings


@router.put("/settings")
def update_settings_endpoint(
    payload: Dict[str, Any],
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """
    Actualiza la configuración general.
    Los secretos (api_key_llm, gdrive_access_token, github_token) se extraen y guardan
    exclusivamente en el almacén seguro del sistema (keyring/bóveda cifrada), nunca en SQLite.
    """
    raw = dict(payload)
    api_key = raw.pop("llm_api_key", None)
    gdrive_token = raw.pop("gdrive_access_token", None)
    github_token = raw.pop("github_token", None)
    # Campos solo informativos que la pantalla devuelve tal cual: se ignoran.
    for readonly in ("estado_secretos", "catalogo_proveedores", "carpeta_datos"):
        raw.pop(readonly, None)
    try:
        validated = SettingsUpdate.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_settings_error_message(exc)) from exc
    clean_payload = validated.model_dump(exclude_unset=True)

    if "github_repo" in clean_payload and (clean_payload["github_repo"] or "").strip() != OFFICIAL_GITHUB_REPO:
        raise HTTPException(
            status_code=400,
            detail="El repositorio de actualizaciones no se puede cambiar: Archivero solo se actualiza desde su repositorio oficial.",
        )

    provider = clean_payload.get("proveedor_modelo") or db.get_setting("proveedor_modelo") or "google_ai_studio"
    if provider not in LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Proveedor de IA desconocido: {provider}")
    if "modelo_llm" in clean_payload and provider != "heuristico_local":
        clean_payload["modelo_llm"] = resolve_llm_model(provider, clean_payload.get("modelo_llm"))
    if "llm_endpoint_url" in clean_payload:
        endpoint = str(clean_payload.get("llm_endpoint_url") or "").strip().rstrip("/")
        # Guardar vacío cuando es la dirección oficial: así un cambio de proveedor no arrastra la anterior.
        if endpoint and endpoint == resolve_llm_endpoint(provider, None):
            endpoint = ""
        clean_payload["llm_endpoint_url"] = endpoint
    if api_key is not None and str(api_key).strip():
        creds.set_llm_api_key(provider, str(api_key).strip())
    if gdrive_token is not None and str(gdrive_token).strip():
        creds.set_gdrive_token(str(gdrive_token).strip())
    if github_token is not None and str(github_token).strip():
        creds.set_github_token(str(github_token).strip())

    previous = db.get_all_settings()
    updated = db.set_settings(clean_payload)
    _log_settings_changes(db, previous, updated, api_key_changed=bool(api_key and str(api_key).strip()))
    updated["estado_secretos"] = {
        "llm_api_key_configurada": bool(creds.get_llm_api_key(provider)),
        "gdrive_token_configurado": bool(creds.get_gdrive_token()),
        "github_token_configurado": bool(creds.get_github_token()),
    }
    return updated


_SETTINGS_LABELS = {
    "umbral_confianza": "El umbral de confianza debe estar entre 0,5 y 0,99",
    "intervalo_correo_minutos": "El intervalo del correo debe estar entre 5 y 1440 minutos",
    "plazo_pago_defecto_dias": "El plazo de pago general debe estar entre 0 y 365 días",
}


def _settings_error_message(exc: ValidationError) -> str:
    mensajes = []
    for err in exc.errors():
        campo = str(err.get("loc", ["?"])[0])
        if err.get("type") == "extra_forbidden":
            mensajes.append(f"Opción desconocida: «{campo}»")
        elif campo in _SETTINGS_LABELS:
            mensajes.append(_SETTINGS_LABELS[campo])
        else:
            msg = str(err.get("msg") or "valor no válido").removeprefix("Value error, ")
            mensajes.append(f"{campo}: {msg}")
    return "Configuración no válida. " + "; ".join(dict.fromkeys(mensajes)) + "."


_WATCHED_SETTINGS = {
    "proveedor_modelo": "proveedor de IA",
    "modelo_llm": "modelo de IA",
    "llm_endpoint_url": "dirección de la IA",
    "carpeta_salida": "carpeta de facturas",
    "capa_almacenamiento": "lugar de archivo",
    "plazo_pago_defecto_dias": "plazo de pago general",
    "intervalo_correo_minutos": "intervalo de revisión del correo",
}


def _log_settings_changes(
    db: Database, previous: Dict[str, Any], updated: Dict[str, Any], api_key_changed: bool = False
) -> None:
    cambios = [
        f"{label}: «{previous.get(key) or '-'}» → «{updated.get(key) or '-'}»"
        for key, label in _WATCHED_SETTINGS.items()
        if previous.get(key) != updated.get(key)
    ]
    if api_key_changed:
        cambios.append("clave de la IA actualizada")
    if cambios:
        db.log_event(
            tipo="config_cambiada",
            mensaje="Configuración cambiada: " + "; ".join(cambios) + ".",
            detalle={"cambios": cambios},
        )


@router.post("/settings/data-dir")
def change_data_dir_endpoint(
    request: Request,
    payload: Dict[str, Any],
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
    poller: MailPollerService = Depends(get_mail_poller),
) -> Dict[str, Any]:
    """Cambia la carpeta de datos (base de datos y claves cifradas). Requiere reiniciar Archivero."""
    try:
        result = change_data_dir(
            raw_path=str(payload.get("ruta") or ""),
            current_db_path=db.db_path,
            current_vault_dir=creds.vault_dir,
        )
        db.log_event(
            tipo="config_cambiada",
            nivel="aviso",
            mensaje=f"Carpeta de datos cambiada a «{payload.get('ruta')}». Reinicia Archivero para empezar a usarla.",
        )
        # Hasta reiniciar, no se aceptan facturas ni movimientos nuevos: irían a la base de datos antigua.
        request.app.state.restart_required = True
        try:
            poller.stop_background_worker()
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo parar la revisión del correo: %s", exc)
        result = dict(result)
        result["reinicio_necesario"] = True
        result["mensaje_reinicio"] = RESTART_REQUIRED_DETAIL
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudieron copiar los datos: {exc}")


@router.post("/settings/test-llm")
def test_llm_connection_endpoint(
    payload: Dict[str, Any],
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """
    Comprueba en vivo si la clave o proveedor seleccionado responde correctamente
    procesando una factura mínima de prueba.
    """
    from app.services.llm_processor import LLMProcessor
    from app.services.pdf_extractor import PDFContent

    provider = payload.get("proveedor_modelo") or db.get_setting("proveedor_modelo") or "google_ai_studio"
    typed_key = (payload.get("llm_api_key") or "").strip()
    effective_key = typed_key or creds.get_llm_api_key(provider) or ""
    model = payload.get("modelo_llm") or db.get_setting("modelo_llm") or ""
    if "llm_endpoint_url" in payload:
        endpoint_url = payload.get("llm_endpoint_url") or None
    else:
        endpoint_url = db.get_setting("llm_endpoint_url") or None

    if provider == "heuristico_local":
        return {
            "ok": True,
            "mensaje": "Modo básico sin Internet activo. Funciona directamente en tu ordenador sin necesitar clave.",
        }

    if not effective_key and provider in ("google_ai_studio", "openai_compatible", "anthropic"):
        is_local_ai = provider == "openai_compatible" and resolve_llm_endpoint(provider, endpoint_url) != resolve_llm_endpoint(provider, None)
        if not is_local_ai:
            return {
                "ok": False,
                "mensaje": "Falta pegar la clave de acceso (API Key) o guardarla antes de hacer la prueba.",
            }

    sample_pdf = PDFContent(
        pdf_bytes=b"%PDF-1.4 test",
        hash_sha256="0" * 64,
        text=(
            "Proveedor: Distribuidora de Prueba S.L.\n"
            "NIF: B12345678\n"
            "Factura Nº: TEST-001\n"
            "Fecha de emisión: 2026-11-01\n"
            "Base Imponible: 100,00 EUR\n"
            "IVA 21%: 21,00 EUR\n"
            "Total Factura: 121,00 EUR"
        ),
        page_count=1,
        has_text_layer=True,
        extraction_method="texto_nativo",
        filename="prueba_conexion.pdf",
    )

    proc = LLMProcessor(
        provider_type=provider,
        api_key=effective_key,
        model=model,
        endpoint_url=endpoint_url,
    )
    try:
        prov_instance = proc._build_provider()
        ext = prov_instance.extract_invoice(sample_pdf)
        if typed_key:
            creds.set_llm_api_key(provider, typed_key)
        modelo_usado = getattr(prov_instance, "model", "")
        aviso_modelo = ""
        if modelo_usado and modelo_usado != proc.model:
            aviso_modelo = f" Aviso: el modelo '{proc.model}' ya no existe; se ha usado '{modelo_usado}'. Guarda las opciones para usarlo siempre."
        return {
            "ok": True,
            "modelo": modelo_usado,
            "mensaje": (
                f"¡Conexión verificada con éxito! El modelo {modelo_usado or prov_instance.provider_name} "
                f"leyó correctamente la factura de prueba ({ext.total:.2f} €).{aviso_modelo}"
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "mensaje": f"No se pudo usar el proveedor elegido ({provider}, modelo {proc.model}). {describe_llm_error(exc)}",
        }


@router.post("/database/backup")
def create_manual_db_backup(db: Database = Depends(get_db)) -> Dict[str, Any]:
    backup_path = db.create_backup(reason="manual_ui")
    if backup_path is not None:
        db.log_event(
            tipo="copia_seguridad",
            mensaje=f"Copia de seguridad guardada en {backup_path}.",
            detalle={"ruta": str(backup_path)},
        )
    return {
        "created": backup_path is not None,
        "backup_path": str(backup_path) if backup_path else "",
    }


# =====================================================================
# 4b. Actividad, avisos y estado del programa
# =====================================================================


@router.get("/activity")
def list_activity(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    nivel: Optional[str] = Query(default=None),
    tipo: Optional[str] = Query(default=None),
    solo_no_leidos: bool = Query(default=False),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    return db.list_events(
        limit=limit, offset=offset, nivel=nivel or None, tipo=tipo or None, solo_no_leidos=solo_no_leidos
    )


@router.get("/activity/unread-count")
def activity_unread_count(db: Database = Depends(get_db)) -> Dict[str, int]:
    return db.count_unread()


@router.post("/activity/mark-read")
def activity_mark_read(payload: Dict[str, Any], db: Database = Depends(get_db)) -> Dict[str, Any]:
    if payload.get("all"):
        marked = db.mark_read(all_events=True)
    else:
        raw_ids = payload.get("ids") or []
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="'ids' debe ser una lista de números.")
        try:
            ids = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'ids' debe ser una lista de números.")
        marked = db.mark_read(ids=ids)
    return {"marcados": marked, **db.count_unread()}


@router.get("/activity/health")
def activity_health(request: Request, db: Database = Depends(get_db)) -> Dict[str, Any]:
    poller = getattr(request.app.state, "mail_poller", None)
    running = poller.is_running() if poller is not None and hasattr(poller, "is_running") else None
    return compute_health(db, poller_running=running)


# =====================================================================
# 4c. Liquidez y plazos de pago por proveedor
# =====================================================================


@router.get("/liquidity")
def read_liquidity(
    horizonte: int = Query(default=90, ge=7, le=365),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    return compute_liquidity(db, horizonte=horizonte)


@router.get("/suppliers/payment-terms")
def list_supplier_payment_terms(db: Database = Depends(get_db)) -> Dict[str, Any]:
    return supplier_payment_terms(db)


@router.put("/suppliers/{supplier_id}/payment-term")
def update_supplier_payment_term(
    supplier_id: int, payload: Dict[str, Any], db: Database = Depends(get_db)
) -> Dict[str, Any]:
    raw = payload.get("plazo_pago_dias")
    dias: Optional[int]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        dias = None
    else:
        try:
            dias = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="El plazo debe ser un número de días.")
        if not 0 <= dias <= 365:
            raise HTTPException(status_code=400, detail="El plazo debe estar entre 0 y 365 días.")
    supplier = db.get_supplier(supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado.")
    db.set_supplier_payment_term(supplier_id, dias)
    db.log_event(
        tipo="config_cambiada",
        mensaje=(
            f"Plazo de pago de {supplier['nombre']}: "
            + (f"{dias} días." if dias is not None else "se vuelve a calcular solo.")
        ),
    )
    item = next((t for t in supplier_payment_terms(db)["items"] if t["proveedor_id"] == supplier_id), None)
    return item or {"proveedor_id": supplier_id, "plazo_pago_dias": dias}


# =====================================================================
# 5. Exportación a Excel y Actualizador
# =====================================================================


@router.get("/export/excel")
def export_accounting_excel(
    mes: Optional[str] = Query(default=None),
    categoria: Optional[str] = Query(default=None),
    estado_revision: Optional[str] = Query(default=None),
    db: Database = Depends(get_db),
) -> Response:
    mes = (mes or "").strip() or None
    if mes and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes):
        raise HTTPException(status_code=400, detail="El mes debe tener el formato AAAA-MM (por ejemplo 2026-03).")
    exporter = AccountingExcelExporter(db=db)
    xlsx_bytes = exporter.generate_workbook_bytes(
        mes=mes,
        categoria=categoria,
        estado_revision=estado_revision,
    )
    filename = f"contabilidad_farmacia_{mes or 'completa'}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/updater/check")
def check_updates_endpoint(
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    repo = db.get_setting("github_repo", "abel-eiras/archivero")
    token = creds.get_github_token()
    updater = UpdaterService(github_repo=repo, github_token=token)
    info = updater.check_for_updates()
    remote = info.get("version_remota")
    if info.get("hay_actualizacion") and remote and db.get_setting("ultima_version_avisada", "") != remote:
        db.set_settings({"ultima_version_avisada": remote})
        db.log_event(
            tipo="actualizacion",
            mensaje=f"Hay una versión nueva de Archivero disponible ({remote}). Instálala desde Configuración.",
            detalle={"version_remota": remote, "version_actual": APP_VERSION},
        )
    return info


def _shutdown_for_update(request: Request, delay_seconds: float = 1.5) -> None:
    """Cierra la aplicación poco después de responder para que el actualizador pueda reemplazar el .exe."""

    def _exit_later() -> None:
        time.sleep(delay_seconds)
        try:
            request.app.state.mail_poller.stop_background_worker()
        except Exception:
            pass
        logger.info("Cerrando Archivero para instalar la actualización.")
        os._exit(0)

    threading.Thread(target=_exit_later, name="Archivero-UpdateShutdown", daemon=True).start()


@router.post("/updater/install")
def install_update_endpoint(
    request: Request,
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """
    Descarga la última versión, verifica su SHA-256 (SHA256SUMS.txt de la release),
    lanza el actualizador independiente y cierra la aplicación.
    """
    if not getattr(sys, "frozen", False):
        raise HTTPException(
            status_code=400,
            detail="La instalación automática solo está disponible en la versión empaquetada (Archivero.exe).",
        )
    repo = db.get_setting("github_repo", "abel-eiras/archivero")
    updater = UpdaterService(github_repo=repo, github_token=creds.get_github_token())
    info = updater.check_for_updates()
    if info.get("error"):
        raise HTTPException(status_code=502, detail=info["error"])
    if not info.get("hay_actualizacion"):
        return {"instalando": False, "mensaje": "Ya tienes instalada la última versión."}
    if not info.get("download_url") or not info.get("asset_name"):
        raise HTTPException(status_code=404, detail="La release no incluye un ejecutable para este sistema.")
    if not info.get("sha256_esperado"):
        raise HTTPException(status_code=422, detail="La release no publica el hash SHA-256 del ejecutable; no se instala.")

    try:
        downloaded = updater.download_and_verify_update(
            download_url=info["download_url"],
            expected_sha256=info["sha256_esperado"],
            filename=f"update_{info['asset_name']}",
        )
        updater.launch_standalone_updater(
            downloaded_file=downloaded,
            target_executable=Path(sys.executable).resolve(),
            expected_sha256=info["sha256_esperado"],
        )
    except Exception as exc:
        logger.error("No se pudo preparar la actualización: %s", exc)
        raise HTTPException(status_code=500, detail=f"No se pudo instalar la actualización: {exc}")

    _shutdown_for_update(request)
    return {
        "instalando": True,
        "version_remota": info.get("version_remota"),
        "mensaje": "Actualización descargada y verificada. Archivero se cerrará y se volverá a abrir en unos segundos.",
    }


# =====================================================================
# 6. Generador e Ingesta de Casos Reales de Muestra (Fase 0)
# =====================================================================


@router.post("/demo/load-sample-data")
def load_phase0_sample_data(
    request: Request,
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """
    Genera e ingiere el lote de prueba de la Fase 0 (facturas reales variadas de farmacia
    y extractos bancarios en Norma 43, CAMT.053 y CSV) para probar todas las pantallas.
    """
    ensure_ingestion_allowed(request)
    from scripts.generate_sample_data import generate_and_ingest_sample_dataset

    return generate_and_ingest_sample_dataset(db=db, creds=creds)


@router.delete("/demo/sample-data")
def delete_sample_data(db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Borra las facturas y movimientos de ejemplo (y sus PDF archivados). No toca los datos reales."""
    from scripts.generate_sample_data import DEMO_BANK_FILE_PREFIX

    res = db.delete_demo_data(DEMO_BANK_FILE_PREFIX)
    base = Path(db.get_setting("carpeta_salida") or "archivo_facturas").resolve()
    pdf_borrados = 0
    for ruta in res.pop("rutas_pdf", []) or []:
        try:
            path = Path(ruta).resolve()
            if path.is_file() and (base == path.parent or base in path.parents):
                path.unlink()
                pdf_borrados += 1
                remove_empty_parents(path.parent, base)
        except OSError as exc:
            logger.warning("No se pudo borrar el PDF de ejemplo %s: %s", ruta, exc)
    res["pdf_borrados"] = pdf_borrados
    db.log_event(
        tipo="config_cambiada",
        mensaje=(
            f"Datos de ejemplo eliminados: {res.get('facturas_borradas', 0)} factura(s) y "
            f"{res.get('movimientos_borrados', 0)} movimiento(s) del banco."
        ),
    )
    return res
