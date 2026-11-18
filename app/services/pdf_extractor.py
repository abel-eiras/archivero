"""Módulo de lectura de PDFs, cálculo de hash SHA-256 y detección de capa de texto u OCR."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from pypdf import PdfReader

logger = logging.getLogger(__name__)

INVOICE_KEYWORDS = [
    "factura",
    "base imponible",
    "i.v.a",
    "iva",
    "total factura",
    "vencimiento",
    "recargo equivalencia",
    "albarán",
    "albaran",
    "abono",
    "rectificativa",
    "nif",
    "cif",
    "importe total",
    "subtotal",
]


@dataclass
class PDFContent:
    """Resultado de la inspección y extracción inicial de un PDF."""

    pdf_bytes: bytes
    hash_sha256: str
    text: str
    page_count: int
    has_text_layer: bool
    extraction_method: str  # "texto_nativo" | "ocr_local" | "vision_llm"
    filename: str = "documento.pdf"

    @property
    def base64_pdf(self) -> str:
        return base64.b64encode(self.pdf_bytes).decode("ascii")


def compute_pdf_hash(pdf_data: Union[bytes, Path, str]) -> str:
    """Calcula el hash SHA-256 del contenido binario del PDF para detectar duplicados."""
    if isinstance(pdf_data, (Path, str)):
        data = Path(pdf_data).read_bytes()
    else:
        data = pdf_data
    return hashlib.sha256(data).hexdigest()


def extract_pdf_content(
    pdf_source: Union[bytes, Path, str],
    filename: str = "documento.pdf",
    ocr_strategy: str = "vision_llm",
    min_chars_for_text_layer: int = 35,
) -> PDFContent:
    """
    Extrae el texto del PDF. Si no tiene capa de texto suficiente:
    - Si ocr_strategy == 'ocr_local' e instalaciones como tesseract están disponibles, aplica OCR local.
    - En caso contrario (o si ocr_strategy == 'vision_llm'), marca el documento para envío multimodal al modelo con visión.
    """
    if isinstance(pdf_source, (Path, str)):
        path = Path(pdf_source)
        pdf_bytes = path.read_bytes()
        filename = path.name
    else:
        pdf_bytes = pdf_source

    pdf_hash = compute_pdf_hash(pdf_bytes)
    extracted_pages: list[str] = []
    page_count = 0

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                extracted_pages.append(page_text.strip())
    except Exception as exc:
        logger.warning("No se pudo leer capa de texto con pypdf en %s: %s", filename, exc)

    full_text = "\n\n".join(extracted_pages).strip()
    clean_alnum = re.sub(r"\s+", "", full_text)
    has_text_layer = len(clean_alnum) >= min_chars_for_text_layer

    if has_text_layer:
        return PDFContent(
            pdf_bytes=pdf_bytes,
            hash_sha256=pdf_hash,
            text=full_text,
            page_count=max(page_count, 1),
            has_text_layer=True,
            extraction_method="texto_nativo",
            filename=filename,
        )

    # Si no tiene capa de texto suficiente, probamos OCR local si se configuró o como apoyo
    ocr_text: Optional[str] = None
    if ocr_strategy == "ocr_local":
        ocr_text = _try_local_tesseract_ocr(pdf_bytes)

    if ocr_text and len(re.sub(r"\s+", "", ocr_text)) >= min_chars_for_text_layer:
        return PDFContent(
            pdf_bytes=pdf_bytes,
            hash_sha256=pdf_hash,
            text=ocr_text,
            page_count=max(page_count, 1),
            has_text_layer=False,
            extraction_method="ocr_local",
            filename=filename,
        )

    # Fallback o configuración por defecto: el PDF se enviará directamente al modelo con visión
    return PDFContent(
        pdf_bytes=pdf_bytes,
        hash_sha256=pdf_hash,
        text=full_text,
        page_count=max(page_count, 1),
        has_text_layer=False,
        extraction_method="vision_llm",
        filename=filename,
    )


def _try_local_tesseract_ocr(pdf_bytes: bytes) -> Optional[str]:
    """Intenta aplicar OCR local usando pdftoppm + tesseract si están instalados en el sistema."""
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pdf_file = tmp_path / "input.pdf"
            pdf_file.write_bytes(pdf_bytes)
            subprocess.run(
                ["pdftoppm", "-png", "-r", "200", str(pdf_file), str(tmp_path / "page")],
                check=True,
                capture_output=True,
                timeout=30,
            )
            texts: list[str] = []
            for img_path in sorted(tmp_path.glob("page*.png")):
                proc = subprocess.run(
                    ["tesseract", str(img_path), "stdout", "-l", "spa+eng"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    texts.append(proc.stdout.strip())
            return "\n\n".join(texts) if texts else None
    except Exception as exc:
        logger.debug("OCR local no disponible o falló: %s", exc)
        return None


def looks_like_invoice(
    pdf_content: PDFContent,
    filename: str = "",
    subject: str = "",
) -> bool:
    """
    Determina si un PDF adjunto parece una factura por su nombre, asunto del correo o texto extraído.
    Si el PDF es escaneado (sin capa de texto), se considera candidato para que el modelo con visión lo evalúe.
    """
    combined_header = f"{filename} {subject}".lower()
    header_keywords = ["factura", "fra", "abono", "recibo", "albaran", "rectificativa", "invoice", "cargo"]
    if any(k in combined_header for k in header_keywords):
        return True

    if not pdf_content.has_text_layer:
        # Un PDF escaneado sin texto nativo se acepta como candidato para que el modelo con visión decida
        return True

    lowered_text = pdf_content.text.lower()
    matches = sum(1 for kw in INVOICE_KEYWORDS if kw in lowered_text)
    return matches >= 2
