"""Módulo de almacenamiento y archivo ordenado en dos capas (Carpeta Local y Google Drive API)."""

from __future__ import annotations

import datetime as dt
import hashlib
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

from app.config import ALBARANES_FOLDER, DEFAULT_ARCHIVE_DIR, DEFAULT_STAGING_DIR, MESES_NOMBRES, TIPO_ALBARAN

logger = logging.getLogger(__name__)


@dataclass
class StorageResult:
    """Resultado de archivar una factura PDF."""

    local_path: str
    relative_path: str
    drive_file_id: str = ""
    backend_used: str = "local"


WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)
# Longitud máxima de la ruta relativa dentro del archivo (deja margen para la carpeta base en Windows)
MAX_RELATIVE_PATH_LEN = 200


def _avoid_reserved_name(segment: str) -> str:
    """'CON', 'nul.txt', 'Com1'... son nombres prohibidos en Windows: se les añade '_'."""
    stem = segment.split(".", 1)[0]
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        return stem + "_" + segment[len(stem):]
    return segment


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
    cleaned = cleaned[:max_len].rstrip("._ ") or fallback
    return _avoid_reserved_name(cleaned)


def archive_category_for(tipo_documento: Optional[str], categoria: str) -> str:
    """Los albaranes van a su propia carpeta (Año/Mes/Albaranes/Proveedor), separados de las facturas."""
    return ALBARANES_FOLDER if (tipo_documento or "") == TIPO_ALBARAN else categoria


def _short_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


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
    La ruta relativa se limita a ~200 caracteres recortando proveedor y número de forma
    determinista (con un pequeño código para que dos nombres largos distintos no choquen).
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
    cat_seg = sanitize_path_segment(categoria, fallback="otros", max_len=40)
    prov_seg = sanitize_path_segment(proveedor, fallback="Proveedor_Desconocido")
    num_seg = sanitize_path_segment(numero_factura, fallback=hash_pdf[:8] if hash_pdf else "SIN_NUM")
    date_prefix = parsed_date.isoformat()

    def total_len(prov: str, num: str) -> int:
        # año/mes/categoría/proveedor/fecha_proveedor_número.pdf (+ 9 de reserva para el sufijo de colisión)
        return len(year_str) + len(month_str) + len(cat_seg) + len(prov) + 4 + len(f"{date_prefix}_{prov}_{num}.pdf") + 9

    if total_len(prov_seg, num_seg) > MAX_RELATIVE_PATH_LEN:
        prov_full, num_full = prov_seg, num_seg
        prov_seg = (prov_full[:30].rstrip("._ ") + "_" + _short_digest(prov_full)) if len(prov_full) > 37 else prov_full
        num_seg = (num_full[:30].rstrip("._ ") + "_" + _short_digest(num_full)) if len(num_full) > 37 else num_full
        prov_seg = _avoid_reserved_name(prov_seg)

    filename = f"{date_prefix}_{prov_seg}_{num_seg}.pdf"
    return (year_str, month_str, cat_seg, prov_seg), filename


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_collision(target_path: Path, hash_pdf: str) -> Path:
    """
    Evita sobrescribir otro PDF distinto con el mismo nombre. Si el archivo existente es el mismo
    (mismo hash) se reutiliza su ruta; si no, se añade un sufijo con el hash y, si hiciera falta, un número.
    """
    if not target_path.exists():
        return target_path
    if hash_pdf:
        try:
            if file_sha256(target_path) == hash_pdf:
                return target_path
        except OSError:
            pass
    suffix = (hash_pdf or uuid.uuid4().hex)[:8]
    candidate = target_path.with_name(f"{target_path.stem}_{suffix}{target_path.suffix}")
    counter = 2
    while candidate.exists():
        if hash_pdf:
            try:
                if file_sha256(candidate) == hash_pdf:
                    return candidate
            except OSError:
                pass
        candidate = target_path.with_name(f"{target_path.stem}_{suffix}_{counter}{target_path.suffix}")
        counter += 1
    return candidate


def remove_empty_parents(start_dir: Path, stop_dir: Path) -> None:
    """Borra carpetas vacías desde start_dir hacia arriba sin salir nunca de stop_dir (la raíz del archivo)."""
    try:
        stop = stop_dir.resolve()
        current = start_dir.resolve()
    except OSError:
        return
    while current != stop and stop in current.parents:
        try:
            current.rmdir()  # solo funciona si está vacía
        except OSError:
            return
        current = current.parent


@dataclass
class RelocationResult:
    moved: bool
    new_path: str
    message: str = ""
    error: bool = False


def relocate_archived_pdf(
    current_path: str,
    base_output_dir: Path | str,
    fecha_emision: Optional[str],
    categoria: str,
    proveedor: str,
    numero_factura: str,
    hash_pdf: str = "",
    tipo_documento: str = "",
) -> RelocationResult:
    """
    Mueve el PDF archivado a la ruta que le corresponde según sus datos actuales (la misma regla que al
    archivarlo). Nunca pierde el archivo: copia, comprueba el hash, sustituye y solo entonces borra el antiguo.
    """
    base = Path(base_output_dir)
    source = Path(current_path) if current_path else None
    if source is None or not source.is_file():
        return RelocationResult(False, current_path or "", "El PDF archivado no se encuentra; no se ha movido.", error=True)

    folder_parts, filename = build_archive_relative_parts(
        fecha_emision=fecha_emision,
        categoria=archive_category_for(tipo_documento, categoria),
        proveedor=proveedor,
        numero_factura=numero_factura,
        hash_pdf=hash_pdf,
    )
    target_dir = base / Path(*folder_parts)
    try:
        if source.resolve().parent == target_dir.resolve() and (
            source.name == filename or source.stem.startswith(Path(filename).stem + "_")
        ):
            return RelocationResult(False, str(source), "El PDF ya está en su sitio.")
    except OSError:
        pass

    expected_hash = hash_pdf
    tmp_file: Optional[Path] = None
    try:
        source_hash = file_sha256(source)
        if expected_hash and source_hash != expected_hash:
            # El archivo cambió fuera del programa: se mueve igualmente, comprobando su propio hash
            logger.warning("El PDF %s no coincide con el hash registrado; se conserva tal cual.", source)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = resolve_collision(target_dir / filename, source_hash)
        if target.exists():
            # Ya existe una copia idéntica en destino: basta con borrar la antigua
            new_path = target
        else:
            tmp_file = target_dir / f".moviendo_{uuid.uuid4().hex}.tmp"
            shutil.copy2(str(source), str(tmp_file))
            if file_sha256(tmp_file) != source_hash:
                raise OSError("la copia no coincide con el original")
            os.replace(str(tmp_file), str(target))
            tmp_file = None
            new_path = target
        if new_path.resolve() != source.resolve():
            source.unlink()
            remove_empty_parents(source.parent, base)
        return RelocationResult(True, str(new_path.resolve()), "PDF movido a su carpeta correcta.")
    except Exception as exc:
        if tmp_file is not None:
            try:
                tmp_file.unlink()
            except OSError:
                pass
        return RelocationResult(False, str(source), f"No se pudo mover el PDF ({exc}); se conserva en su sitio.", error=True)


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
        return resolve_collision(target_path, hash_pdf)


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
        boundary = f"===============archivero_{uuid.uuid4().hex}=="
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
