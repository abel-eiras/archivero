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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.config import (
    APP_VERSION,
    CATEGORIAS_INICIALES,
    LLM_PROVIDERS,
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
)
from app.services.credentials import CredentialManager
from app.services.data_location import change_data_dir
from app.services.excel_exporter import AccountingExcelExporter
from app.services.llm_processor import describe_llm_error, validate_invoice_extraction
from app.services.mail_poller import MailPollerService, OAuth2MailConnector
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.reconciliation import BankReconciliationService
from app.services.updater_service import UpdaterService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_credentials(request: Request) -> CredentialManager:
    return request.app.state.credentials


def get_mail_poller(request: Request) -> MailPollerService:
    return request.app.state.mail_poller


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
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    items = db.list_invoices(
        incluir_sustituidas=incluir_sustituidas,
        estado_revision=estado_revision,
        estado_pago=estado_pago,
        categoria=categoria,
        proveedor=proveedor,
        mes=mes,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        busqueda=busqueda,
    )
    return {"total": len(items), "items": items}


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db: Database = Depends(get_db)) -> Dict[str, Any]:
    inv = db.get_invoice_by_id(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")
    return inv


@router.post("/invoices/upload")
async def upload_invoice_pdfs(
    files: List[UploadFile] = File(...),
    remitente: str = Form(default=""),
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """Sube y procesa uno o varios archivos PDF de factura a través del pipeline completo."""
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

    for f in files:
        content = await f.read()
        if not content:
            continue
        res = pipeline.ingest_pdf(
            pdf_source=content,
            filename=f.filename or "factura.pdf",
            sender_email=remitente,
        )
        if res.status == "insertada":
            insertadas += 1
        elif res.status == "duplicada":
            duplicadas += 1

        resultados.append(
            {
                "filename": f.filename,
                "status": res.status,
                "invoice_id": res.invoice_id,
                "hash_pdf": res.hash_pdf,
                "detail": res.detail,
                "invoice": res.invoice,
            }
        )

    return {
        "procesados": len(resultados),
        "insertadas": insertadas,
        "duplicadas": duplicadas,
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
    return updated or {}


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
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    items = db.list_bank_transactions(estado_conciliacion=estado_conciliacion, banco=banco)
    return {
        "total": len(items),
        "items": items,
        "mapeos_csv": db.get_csv_mappings(),
    }


@router.post("/bank/import")
async def import_bank_statement(
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
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El fichero importado está vacío.")

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
    return service.run_auto_reconciliation()


@router.post("/bank/transactions/{movimiento_id}/confirm-proposal")
def confirm_reconciliation_proposal(
    movimiento_id: int,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    service = BankReconciliationService(db=db)
    return service.confirm_proposal(movimiento_id)


@router.post("/bank/transactions/{movimiento_id}/reject-proposal")
def reject_reconciliation_proposal(
    movimiento_id: int,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    service = BankReconciliationService(db=db)
    return service.reject_proposal(movimiento_id)


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
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
) -> Dict[str, Any]:
    ok = db.delete_mail_account(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cuenta de correo no encontrada.")
    return {"deleted": True, "id": account_id}


@router.get("/mail/oauth/authorize-url")
def get_oauth_authorize_url(
    provider: str = Query(..., description="oauth_gmail o oauth_outlook"),
    client_id: str = Query(...),
    email: str = Query(default=""),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, str]:
    # Recordar client_id y proveedor para poder canjear el código en el callback
    if email:
        tokens = creds.get_oauth_tokens(email)
        if client_id and tokens.get("client_id") != client_id:
            tokens["client_id"] = client_id
        tokens["provider"] = provider
        creds.set_oauth_tokens(email, tokens)
    url = OAuth2MailConnector.build_authorization_url(
        provider_key=provider,
        client_id=client_id,
        state=email,
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
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> HTMLResponse:
    """Recibe la redirección OAuth2 (Gmail/Outlook), canjea el código por tokens y los guarda."""
    email_addr = (state or "").strip().lower()
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
def sync_mail_now(poller: MailPollerService = Depends(get_mail_poller)) -> Dict[str, Any]:
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
    clean_payload = dict(payload)
    api_key = clean_payload.pop("llm_api_key", None)
    gdrive_token = clean_payload.pop("gdrive_access_token", None)
    github_token = clean_payload.pop("github_token", None)
    clean_payload.pop("estado_secretos", None)
    clean_payload.pop("catalogo_proveedores", None)
    clean_payload.pop("carpeta_datos", None)

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

    updated = db.set_settings(clean_payload)
    updated["estado_secretos"] = {
        "llm_api_key_configurada": bool(creds.get_llm_api_key(provider)),
        "gdrive_token_configurado": bool(creds.get_gdrive_token()),
        "github_token_configurado": bool(creds.get_github_token()),
    }
    return updated


@router.post("/settings/data-dir")
def change_data_dir_endpoint(
    payload: Dict[str, Any],
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """Cambia la carpeta de datos (base de datos y claves cifradas). Requiere reiniciar Archivero."""
    try:
        return change_data_dir(
            raw_path=str(payload.get("ruta") or ""),
            current_db_path=db.db_path,
            current_vault_dir=creds.vault_dir,
        )
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
    return {
        "created": backup_path is not None,
        "backup_path": str(backup_path) if backup_path else "",
    }


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
    return updater.check_for_updates()


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
    db: Database = Depends(get_db),
    creds: CredentialManager = Depends(get_credentials),
) -> Dict[str, Any]:
    """
    Genera e ingiere el lote de prueba de la Fase 0 (facturas reales variadas de farmacia
    y extractos bancarios en Norma 43, CAMT.053 y CSV) para probar todas las pantallas.
    """
    from scripts.generate_sample_data import generate_and_ingest_sample_dataset

    return generate_and_ingest_sample_dataset(db=db, creds=creds)
