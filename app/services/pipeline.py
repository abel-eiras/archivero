"""Pipeline de ingesta de facturas: Hash -> Duplicados -> LLM + Reglas -> Archivo -> SQLite."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

from app.db.database import Database
from app.services.llm_processor import LLMProcessor, validate_invoice_extraction
from app.services.pdf_extractor import compute_pdf_hash
from app.services.storage import GoogleDriveStorage, LocalFolderStorage, StorageBackend

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    """Resultado de ingerir un PDF en el sistema."""

    status: str  # "insertada" | "duplicada" | "ignorada_no_factura"
    invoice_id: Optional[int]
    invoice: Optional[Dict[str, Any]]
    hash_pdf: str
    detail: str


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

        if settings.get("capa_almacenamiento") == "gdrive" and self.gdrive_access_token:
            return GoogleDriveStorage(
                access_token=self.gdrive_access_token,
                root_folder_id=settings.get("gdrive_folder_id") or "",
                local_mirror=local_storage,
            )
        return local_storage

    def _build_llm_processor(self, settings: Dict[str, Any]) -> LLMProcessor:
        return LLMProcessor(
            provider_type=settings.get("proveedor_modelo") or "google_ai_studio",
            api_key=self.llm_api_key,
            model=settings.get("modelo_llm") or "gemini-2.0-flash",
            endpoint_url=settings.get("llm_endpoint_url"),
            ocr_strategy=settings.get("estrategia_ocr") or "vision_llm",
            umbral_confianza=float(settings.get("umbral_confianza") or 0.85),
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
            val = validate_invoice_extraction(
                extraction=ext,
                umbral_confianza=float(settings.get("umbral_confianza") or 0.85),
            )

        # Archivar de forma segura
        storage = self._build_storage(settings)
        storage_res = storage.save_invoice_pdf(
            pdf_bytes=pdf_bytes,
            fecha_emision=ext.fecha_emision,
            categoria=ext.categoria,
            proveedor=ext.proveedor,
            numero_factura=ext.numero_factura,
            hash_pdf=pdf_hash,
        )

        errores_y_avisos = list(val.errores) + [f"Aviso: {w}" for w in val.advertencias]
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
                "estado_revision": val.estado_revision,
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
        saved_invoice = self.db.get_invoice_by_id(invoice_id)
        return IngestionResult(
            status="insertada",
            invoice_id=invoice_id,
            invoice=saved_invoice,
            hash_pdf=pdf_hash,
            detail=f"Factura procesada ({val.estado_revision}, confianza {val.confianza_ajustada:.0%}).",
        )
