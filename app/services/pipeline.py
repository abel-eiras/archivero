"""Pipeline de ingesta de facturas: Hash -> Duplicados -> LLM + Reglas -> Archivo -> SQLite."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

from app.db.database import Database
from app.services.activity import fmt_eur
from app.services.llm_processor import LLMProcessor, apply_basic_reader_policy, validate_invoice_extraction
from app.services.pdf_extractor import compute_pdf_hash
from app.services.storage import LocalFolderStorage, StorageBackend, archive_category_for

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    """Resultado de ingerir un PDF en el sistema."""

    status: str  # "insertada" | "duplicada" | "ignorada_no_factura"
    invoice_id: Optional[int]
    invoice: Optional[Dict[str, Any]]
    hash_pdf: str
    detail: str


def _safe_threshold(settings: Dict[str, Any]) -> float:
    try:
        value = float(settings.get("umbral_confianza") or 0.85)
    except (TypeError, ValueError):
        return 0.85
    return value if 0.5 <= value <= 0.99 else 0.85


class InvoiceIngestionPipeline:
    """Orquesta el flujo completo de procesado, archivo seguro y registro en SQLite."""

    def __init__(
        self,
        db: Database,
        llm_api_key: Optional[str] = None,
        gdrive_access_token: Optional[str] = None,
        storage_override: Optional[StorageBackend] = None,
    ) -> None:
        self.db = db
        self.llm_api_key = llm_api_key
        self.gdrive_access_token = gdrive_access_token
        self.storage_override = storage_override

    def _build_storage(self, settings: Dict[str, Any]) -> StorageBackend:
        if self.storage_override is not None:
            return self.storage_override

        output_folder = Path(settings.get("carpeta_salida") or "archivo_facturas")
        local_storage = LocalFolderStorage(base_output_dir=output_folder)

        # La subida directa a Google Drive está desactivada (los tokens caducan en 1 hora): se usa la carpeta
        # local, que puede ser la carpeta sincronizada de Google Drive para escritorio. Se avisa una sola vez.
        if settings.get("capa_almacenamiento") == "gdrive" and not settings.get("aviso_gdrive_mostrado"):
            self.db.set_settings({"aviso_gdrive_mostrado": True})
            self.db.log_event(
                tipo="config_cambiada",
                nivel="aviso",
                mensaje=(
                    "La subida directa a Google Drive aún no está disponible: las facturas se guardan en la carpeta "
                    f"local {output_folder}. Puedes elegir como carpeta la de Google Drive para escritorio."
                ),
            )
        return local_storage

    def _build_llm_processor(self, settings: Dict[str, Any]) -> LLMProcessor:
        return LLMProcessor(
            provider_type=settings.get("proveedor_modelo") or "google_ai_studio",
            api_key=self.llm_api_key,
            model=settings.get("modelo_llm") or "",
            endpoint_url=settings.get("llm_endpoint_url"),
            ocr_strategy=settings.get("estrategia_ocr") or "vision_llm",
            umbral_confianza=_safe_threshold(settings),
            categories=settings.get("categorias"),
        )

    def ingest_pdf(
        self,
        pdf_source: Union[bytes, Path, str],
        filename: str = "factura.pdf",
        sender_email: str = "",
        message_id: str = "",
    ) -> IngestionResult:
        """
        Procesa un PDF candidato:
        1. Calcula su hash SHA-256 y comprueba si ya existe en SQLite.
        2. Consulta reglas por remitente.
        3. Extrae y valida los datos con el procesador LLM.
        4. Consulta reglas adicionales por NIF/proveedor.
        5. Archiva el PDF en la estructura fija (Año/Mes/Categoría/Proveedor).
        6. Inserta la fila en SQLite.
        """
        pdf_bytes = Path(pdf_source).read_bytes() if isinstance(pdf_source, (Path, str)) else pdf_source
        pdf_hash = compute_pdf_hash(pdf_bytes)

        # 1. Detección de duplicados por contenido binario (hash SHA-256)
        existing = self.db.get_invoice_by_hash(pdf_hash)
        if existing:
            self.db.log_event(
                tipo="factura_duplicada",
                mensaje=(
                    f"PDF repetido ignorado ({filename}): ya estaba guardado como factura "
                    f"{existing.get('numero_factura') or '#' + str(existing['id'])} de "
                    f"{existing.get('proveedor_nombre') or 'proveedor desconocido'}."
                ),
                detalle={"fichero": filename, "remitente": sender_email},
                entidad="factura",
                entidad_id=existing["id"],
            )
            return IngestionResult(
                status="duplicada",
                invoice_id=existing["id"],
                invoice=existing,
                hash_pdf=pdf_hash,
                detail=f"Factura duplicada (ya registrada con ID {existing['id']}).",
            )

        settings = self.db.get_all_settings()
        sender_overrides = self.db.match_rules_for_invoice(sender_email=sender_email)

        processor = self._build_llm_processor(settings)
        processed = processor.process_pdf(
            pdf_source=pdf_bytes,
            filename=filename,
            sender_rule_overrides=sender_overrides,
        )
        ext = processed.extraccion
        val = processed.validacion

        if "->heuristico_local" in (getattr(processed, "proveedor_llm_usado", "") or ""):
            motivo = next(
                (w for w in val.advertencias if w.startswith("Se usó extracción local")),
                "La IA externa no respondió.",
            )
            self.db.log_event(
                tipo="error_ia",
                nivel="aviso",
                mensaje=f"La IA no pudo leer {filename}; se usó el lector básico del ordenador. {motivo}"[:500],
                detalle={"fichero": filename, "proveedor": processed.proveedor_llm_usado},
            )

        if not ext.es_factura:
            return IngestionResult(
                status="ignorada_no_factura",
                invoice_id=None,
                invoice=None,
                hash_pdf=pdf_hash,
                detail="El modelo determinó que el PDF no es una factura.",
            )

        # Comprobar si hay reglas por NIF o nombre de proveedor extraído
        post_overrides = self.db.match_rules_for_invoice(
            sender_email=sender_email,
            supplier_name=ext.proveedor,
            nif=ext.nif,
        )
        if post_overrides:
            if post_overrides.get("forzar_proveedor") and post_overrides.get("proveedor"):
                ext.proveedor = post_overrides["proveedor"]
            if post_overrides.get("categoria"):
                ext.categoria = post_overrides["categoria"]
            if post_overrides.get("tipo_documento"):
                ext.tipo_documento = post_overrides["tipo_documento"]
            previous_warnings = [w for w in val.advertencias if w.startswith(("Se usó extracción local", "Leída con el lector básico"))]
            val = validate_invoice_extraction(extraction=ext, umbral_confianza=_safe_threshold(settings))
            for w in previous_warnings:
                if w not in val.advertencias:
                    val.advertencias.append(w)
        if getattr(processed, "lectura_basica_sustituta", False):
            apply_basic_reader_policy(val)

        # Archivar de forma segura
        storage = self._build_storage(settings)
        storage_res = storage.save_invoice_pdf(
            pdf_bytes=pdf_bytes,
            fecha_emision=ext.fecha_emision,
            categoria=archive_category_for(ext.tipo_documento, ext.categoria),
            proveedor=ext.proveedor,
            numero_factura=ext.numero_factura,
            hash_pdf=pdf_hash,
        )

        errores_y_avisos = list(val.errores) + [f"Aviso: {w}" for w in val.advertencias]
        estado_revision = val.estado_revision

        # Factura rectificativa: mismo proveedor y mismo número que una factura activa -> la sustituye
        replaced = self.db.find_replaceable_invoice(
            numero_factura=ext.numero_factura,
            nif=ext.nif,
            proveedor_nombre=ext.proveedor,
        )
        if replaced:
            errores_y_avisos.append(
                f"Aviso: Sustituye a la factura #{replaced['id']} (mismo proveedor y número)."
            )
            if self.db.invoice_has_reconciliation_links(int(replaced["id"])):
                estado_revision = "pendiente_revision"
                errores_y_avisos.append(
                    f"Aviso: La factura sustituida #{replaced['id']} tenía pagos del banco unidos; "
                    "revisa la conciliación y une el pago a esta factura si corresponde."
                )
        invoice_id = self.db.insert_invoice(
            {
                "proveedor_nombre": ext.proveedor,
                "nif": ext.nif,
                "numero_factura": ext.numero_factura,
                "fecha_emision": ext.fecha_emision,
                "fecha_vencimiento": ext.fecha_vencimiento,
                "base_imponible": ext.base_imponible,
                "iva": ext.iva,
                "total": ext.total,
                "moneda": ext.moneda,
                "tipo_documento": ext.tipo_documento,
                "categoria": ext.categoria,
                "estado_revision": estado_revision,
                "estado_pago": "pendiente",
                "nivel_confianza": val.confianza_ajustada,
                "hash_pdf": pdf_hash,
                "ruta_archivo": storage_res.local_path,
                "drive_file_id": storage_res.drive_file_id,
                "mensaje_origen_id": message_id,
                "datos_extra": ext.datos_extra,
                "errores_validacion": errores_y_avisos,
            }
        )
        detail = f"Factura procesada ({estado_revision}, confianza {val.confianza_ajustada:.0%})."
        if replaced:
            self.db.mark_invoice_replaced(old_id=int(replaced["id"]), new_id=invoice_id)
            detail += f" Sustituye a la factura #{replaced['id']}."
        saved_invoice = self.db.get_invoice_by_id(invoice_id)
        self._log_invoice_event(invoice_id, ext, estado_revision, replaced, filename, sender_email)
        return IngestionResult(
            status="insertada",
            invoice_id=invoice_id,
            invoice=saved_invoice,
            hash_pdf=pdf_hash,
            detail=detail,
        )

    def _log_invoice_event(
        self,
        invoice_id: int,
        ext: Any,
        estado_revision: str,
        replaced: Optional[Dict[str, Any]],
        filename: str,
        sender_email: str,
    ) -> None:
        """Anota en el registro de actividad la llegada de una factura (nueva, a revisar o sustituta)."""
        proveedor = ext.proveedor or "proveedor desconocido"
        numero = ext.numero_factura or "sin número"
        origen = f"recibida por correo de {sender_email}" if sender_email else "añadida a mano"
        mensaje = f"Factura {numero} de {proveedor} ({fmt_eur(float(ext.total or 0))}) {origen}."
        if replaced:
            mensaje += f" Sustituye a la factura #{replaced['id']}."
        if ext.tipo_documento == "albaran":
            mensaje = f"Albarán {numero} de {proveedor} {origen}. Se ha guardado aparte: no cuenta como factura ni como pago."
        if estado_revision == "pendiente_revision":
            tipo, nivel = "factura_revision", "aviso"
            mensaje += " Tiene algún dato dudoso: revísala en la Bandeja de Revisión."
        elif replaced:
            tipo, nivel = "factura_sustituida", "info"
        else:
            tipo, nivel = "factura_nueva", "info"
        self.db.log_event(
            tipo=tipo,
            nivel=nivel,
            mensaje=mensaje,
            detalle={"fichero": filename, "remitente": sender_email, "sustituye_a": replaced["id"] if replaced else None},
            entidad="factura",
            entidad_id=invoice_id,
        )
