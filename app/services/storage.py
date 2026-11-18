"""Módulo de almacenamiento y archivo ordenado en dos capas (Carpeta Local y Google Drive API)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import httpx

from app.config import DEFAULT_ARCHIVE_DIR, DEFAULT_STAGING_DIR, MESES_NOMBRES

logger = logging.getLogger(__name__)


@dataclass
class StorageResult:
    """Resultado de archivar una factura PDF."""

    local_path: str
    relative_path: str
    drive_file_id: str = ""
    backend_used: str = "local"


def sanitize_path_segment(value: str, fallback: str = "Sin_Clasificar", max_len: int = 60) -> str:
    """Limpia un texto para que sea un nombre de carpeta o segmento de archivo válido en Windows, Mac y Linux."""
    if not value or not value.strip():
        return fallback
    normalized = unicodedata.normalize("NFKD", value.strip())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    # Reemplazar caracteres ilegales en Windows (<>:"/\|?*) y espacios múltiples
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", ascii_text)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._ ")
    if not cleaned:
        return fallback
    return cleaned[:max_len]


def build_archive_relative_parts(
    fecha_emision: Optional[str],
    categoria: str,
    proveedor: str,
    numero_factura: str,
    hash_pdf: str = "",
) -> tuple[tuple[str, str, str, str], str]:
    """
    Construye la estructura fija de carpetas (año, mes, categoría, proveedor)
    y el nombre de archivo ({fecha}_{proveedor}_{numero_factura}.pdf).
    """
    parsed_date: Optional[dt.date] = None
    if fecha_emision:
        try:
            parsed_date = dt.date.fromisoformat(fecha_emision[:10])
        except ValueError:
            parsed_date = None

    if not parsed_date:
        parsed_date = dt.date.today()

    year_str = f"{parsed_date.year:04d}"
    month_str = MESES_NOMBRES.get(parsed_date.month, f"{parsed_date.month:02d}")
    cat_seg = sanitize_path_segment(categoria, fallback="otros")
    prov_seg = sanitize_path_segment(proveedor, fallback="Proveedor_Desconocido")
    num_seg = sanitize_path_segment(numero_factura, fallback=hash_pdf[:8] if hash_pdf else "SIN_NUM")

    date_prefix = parsed_date.isoformat()
    filename = f"{date_prefix}_{prov_seg}_{num_seg}.pdf"
    return (year_str, month_str, cat_seg, prov_seg), filename


class StorageBackend(ABC):
    """Interfaz común de almacenamiento para Capa 1 (Carpeta Local) y Capa 2 (Google Drive API)."""

    @abstractmethod
    def save_invoice_pdf(
        self,
        pdf_bytes: bytes,
        fecha_emision: Optional[str],
        categoria: str,
        proveedor: str,
        numero_factura: str,
        hash_pdf: str = "",
    ) -> StorageResult:
        """Guarda el PDF aplicando las reglas de estructura fija y robustez."""


class LocalFolderStorage(StorageBackend):
    """
    Capa 1 (obligatoria): escritura en carpeta de salida configurable.
    - Escribe primero en carpeta temporal (.tmp_staging) y mueve al destino al final.
    - Reintenta con backoff ante fallos o lentitud de unidades sincronizadas (Drive/OneDrive/Dropbox).
    - Si tras todos los reintentos la unidad externa sigue inaccesible, guarda en carpeta de contingencia local
      para no perder nunca la factura (Principio 6).
    """

    def __init__(
        self,
        base_output_dir: Optional[Path | str] = None,
        staging_dir: Optional[Path | str] = None,
        max_retries: int = 3,
        retry_delay_seconds: float = 0.25,
        fallback_dir: Optional[Path | str] = None,
    ) -> None:
        self.base_output_dir = Path(base_output_dir or DEFAULT_ARCHIVE_DIR)
        self.staging_dir = Path(staging_dir or DEFAULT_STAGING_DIR)
        self.fallback_dir = Path(fallback_dir or (DEFAULT_STAGING_DIR.parent / "contingencia_facturas"))
        self.max_retries = max(1, max_retries)
        self.retry_delay_seconds = retry_delay_seconds

    def save_invoice_pdf(
        self,
        pdf_bytes: bytes,
        fecha_emision: Optional[str],
        categoria: str,
        proveedor: str,
        numero_factura: str,
        hash_pdf: str = "",
    ) -> StorageResult:
        folder_parts, filename = build_archive_relative_parts(
            fecha_emision=fecha_emision,
            categoria=categoria,
            proveedor=proveedor,
            numero_factura=numero_factura,
            hash_pdf=hash_pdf,
        )
        relative_dir = Path(*folder_parts)

        # 1. Escritura segura primero en carpeta temporal
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self.staging_dir / f"tmp_{uuid.uuid4().hex}_{filename}"
        temp_file.write_bytes(pdf_bytes)

        # 2. Mover a destino final con reintentos ante fallos de unidad
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                target_dir = self.base_output_dir / relative_dir
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = self._resolve_collision(target_dir / filename, hash_pdf)
                shutil.move(str(temp_file), str(target_path))
                return StorageResult(
                    local_path=str(target_path.resolve()),
                    relative_path=str(relative_dir / target_path.name),
                    backend_used="local",
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Intento %d/%d fallido al mover factura a %s: %s",
                    attempt,
                    self.max_retries,
                    self.base_output_dir,
                    exc,
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay_seconds * attempt)

        # 3. Contingencia: nunca se pierde la factura aunque la unidad de destino haya fallado
        logger.error(
            "La unidad de destino %s falló tras %d intentos (%s). Guardando en carpeta de contingencia.",
            self.base_output_dir,
            self.max_retries,
            last_error,
        )
        contingency_target_dir = self.fallback_dir / relative_dir
        contingency_target_dir.mkdir(parents=True, exist_ok=True)
        contingency_path = self._resolve_collision(contingency_target_dir / filename, hash_pdf)
        shutil.move(str(temp_file), str(contingency_path))
        return StorageResult(
            local_path=str(contingency_path.resolve()),
            relative_path=str(relative_dir / contingency_path.name),
            backend_used="local_contingency",
        )

    @staticmethod
    def _resolve_collision(target_path: Path, hash_pdf: str) -> Path:
        """Evita sobrescribir otro PDF distinto que casualmente comparta nombre de archivo."""
        if not target_path.exists():
            return target_path
        if hash_pdf:
            suffix = hash_pdf[:8]
            candidate = target_path.with_name(f"{target_path.stem}_{suffix}{target_path.suffix}")
            return candidate
        return target_path


class GoogleDriveStorage(StorageBackend):
    """
    Capa 2 (opcional): subida directa a Google Drive API v3 mediante OAuth2.
    Mantiene además una copia local en la carpeta configurada para acceso inmediato sin red.
    """

    DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
    DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"

    def __init__(
        self,
        access_token: str,
        root_folder_id: str = "",
        local_mirror: Optional[LocalFolderStorage] = None,
        timeout: float = 30.0,
    ) -> None:
        self.access_token = access_token
        self.root_folder_id = root_folder_id.strip()
        self.local_mirror = local_mirror or LocalFolderStorage()
        self.timeout = timeout
        self._folder_cache: Dict[tuple[str, str], str] = {}

    def save_invoice_pdf(
        self,
        pdf_bytes: bytes,
        fecha_emision: Optional[str],
        categoria: str,
        proveedor: str,
        numero_factura: str,
        hash_pdf: str = "",
    ) -> StorageResult:
        # Guardamos siempre primero en la capa local de forma atómica (principio 6: nada se pierde)
        local_res = self.local_mirror.save_invoice_pdf(
            pdf_bytes=pdf_bytes,
            fecha_emision=fecha_emision,
            categoria=categoria,
            proveedor=proveedor,
            numero_factura=numero_factura,
            hash_pdf=hash_pdf,
        )

        if not self.access_token:
            logger.warning("GoogleDriveStorage configurado sin access_token; se mantiene copia local.")
            return local_res

        folder_parts, filename = build_archive_relative_parts(
            fecha_emision=fecha_emision,
            categoria=categoria,
            proveedor=proveedor,
            numero_factura=numero_factura,
            hash_pdf=hash_pdf,
        )

        try:
            parent_id = self.root_folder_id or "root"
            for part in folder_parts:
                parent_id = self._ensure_drive_folder(part, parent_id)

            file_id = self._upload_pdf_to_drive(pdf_bytes, filename, parent_id)
            return StorageResult(
                local_path=local_res.local_path,
                relative_path=local_res.relative_path,
                drive_file_id=file_id,
                backend_used="gdrive",
            )
        except Exception as exc:
            logger.warning("Fallo al subir PDF a Google Drive API (%s). Se conserva copia local.", exc)
            return local_res

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _ensure_drive_folder(self, folder_name: str, parent_id: str) -> str:
        cache_key = (parent_id, folder_name)
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        safe_name = folder_name.replace("'", "\\'")
        query = (
            f"name = '{safe_name}' and '{parent_id}' in parents "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                self.DRIVE_FILES_URL,
                headers=self._headers(),
                params={"q": query, "fields": "files(id, name)"},
            )
            resp.raise_for_status()
            files = resp.json().get("files", [])
            if files:
                folder_id = files[0]["id"]
                self._folder_cache[cache_key] = folder_id
                return folder_id

            # Crear carpeta
            create_resp = client.post(
                self.DRIVE_FILES_URL,
                headers={**self._headers(), "Content-Type": "application/json"},
                json={
                    "name": folder_name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
            )
            create_resp.raise_for_status()
            folder_id = create_resp.json()["id"]
            self._folder_cache[cache_key] = folder_id
            return folder_id

    def _upload_pdf_to_drive(self, pdf_bytes: bytes, filename: str, parent_id: str) -> str:
        boundary = f"===============farmafacturas_{uuid.uuid4().hex}=="
        metadata = json.dumps({"name": filename, "parents": [parent_id]}).encode("utf-8")

        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + metadata + (
            f"\r\n--{boundary}\r\n"
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8") + pdf_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        headers = {
            **self._headers(),
            "Content-Type": f"multipart/related; boundary={boundary}",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.DRIVE_UPLOAD_URL, headers=headers, content=body)
            resp.raise_for_status()
            return resp.json().get("id", "")
