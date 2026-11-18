"""Módulo de procesado con modelo de lenguaje (LLM) y validación de facturas (Fase 1)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx

from app.config import CATEGORIAS_INICIALES, TIPOS_DOCUMENTO
from app.models.schemas import InvoiceExtraction, ProcessedPDFResult, ValidationResult
from app.services.pdf_extractor import PDFContent, extract_pdf_content

logger = logging.getLogger(__name__)

NIF_REGEX = re.compile(
    r"\b([A-HJNP-SUVW][-\s]?\d{7}[-\s]?[0-9A-J]|\d{8}[-\s]?[A-Z]|[XYZ][-\s]?\d{7}[-\s]?[A-Z])\b",
    re.IGNORECASE,
)


def build_extraction_prompt(categories: Optional[List[Dict[str, str]]] = None) -> str:
    """Construye el prompt estructurado para el modelo de lenguaje."""
    cats = categories or CATEGORIAS_INICIALES
    cat_desc = ", ".join(f"'{c['codigo']}' ({c['nombre']})" for c in cats)
    doc_types = ", ".join(f"'{k}' ({v})" for k, v in TIPOS_DOCUMENTO.items())

    return (
        "Eres un experto contable especializado en farmacias en España.\n"
        "Analiza la siguiente factura de proveedor y devuelve EXCLUSIVAMENTE un objeto JSON válido "
        "con la siguiente estructura exacta:\n"
        "{\n"
        '  "es_factura": true,\n'
        '  "proveedor": "Nombre o razón social del proveedor emisor (no el nombre de la farmacia cliente)",\n'
        '  "nif": "NIF o CIF del proveedor emisor sin espacios ni guiones",\n'
        '  "numero_factura": "Número o serie de factura",\n'
        '  "fecha_emision": "YYYY-MM-DD",\n'
        '  "fecha_vencimiento": "YYYY-MM-DD o null si no figura",\n'
        '  "base_imponible": 0.00,\n'
        '  "iva": 0.00,\n'
        '  "total": 0.00,\n'
        '  "moneda": "EUR",\n'
        f'  "tipo_documento": "Uno de: {doc_types}",\n'
        f'  "categoria": "Una de: {cat_desc}",\n'
        '  "nivel_confianza": 0.95,\n'
        '  "datos_extra": {\n'
        '    "recargo_equivalencia": 0.00,\n'
        '    "irpf": 0.00,\n'
        '    "porcentaje_iva": 21,\n'
        '    "forma_pago": "domiciliacion | transferencia | tarjeta | giro | efectivo",\n'
        '    "iban_pago": ""\n'
        "  }\n"
        "}\n"
        "Reglas importantes:\n"
        "1. Si es un abono o factura rectificativa, usa tipo_documento='abono' y pon los importes en negativo.\n"
        "2. Si la factura incluye Recargo de Equivalencia (muy común en farmacias: 5.2%, 1.4%, 0.5%), "
        "ponlo en datos_extra.recargo_equivalencia.\n"
        "3. Si la factura incluye retención de IRPF (alquileres o profesionales), pon el importe retenido "
        "en positivo dentro de datos_extra.irpf.\n"
        "4. Asegúrate de que las fechas estén en formato ISO YYYY-MM-DD.\n"
    )


class BaseLLMProvider(ABC):
    """Interfaz abstracta para proveedores de modelos de lenguaje."""

    provider_name: str = "base"

    @abstractmethod
    def extract_invoice(
        self,
        pdf_content: PDFContent,
        categories: Optional[List[Dict[str, str]]] = None,
    ) -> InvoiceExtraction:
        """Extrae los datos estructurados de un PDF de factura."""


class GoogleAIStudioProvider(BaseLLMProvider):
    """Proveedor oficial de Google AI Studio (Gemini API) con soporte multimodal para PDFs."""

    provider_name = "google_ai_studio"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        endpoint_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 45.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint_url = endpoint_url.rstrip("/")
        self.timeout = timeout

    def extract_invoice(
        self,
        pdf_content: PDFContent,
        categories: Optional[List[Dict[str, str]]] = None,
    ) -> InvoiceExtraction:
        prompt = build_extraction_prompt(categories)
        parts: List[Dict[str, Any]] = [{"text": prompt}]

        if pdf_content.has_text_layer and pdf_content.text.strip():
            parts.append(
                {
                    "text": (
                        f"Nombre del archivo: {pdf_content.filename}\n\n"
                        f"Texto extraído del PDF:\n{pdf_content.text}"
                    )
                }
            )
        else:
            # Envío directo del PDF al modelo con visión cuando es escaneado o sin capa de texto
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "application/pdf",
                        "data": pdf_content.base64_pdf,
                    }
                }
            )

        url = f"{self.endpoint_url}/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, params={"key": self.api_key}, json=payload)
            resp.raise_for_status()
            data = resp.json()

        raw_text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "{}")
        )
        parsed = _parse_json_from_llm_output(raw_text)
        return InvoiceExtraction(**parsed)


class OpenAICompatibleProvider(BaseLLMProvider):
    """Proveedor compatible con la API de OpenAI (OpenAI, Groq, Ollama local, Azure, etc.)."""

    provider_name = "openai_compatible"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        endpoint_url: str = "https://api.openai.com/v1",
        timeout: float = 45.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint_url = endpoint_url.rstrip("/")
        self.timeout = timeout

    def extract_invoice(
        self,
        pdf_content: PDFContent,
        categories: Optional[List[Dict[str, str]]] = None,
    ) -> InvoiceExtraction:
        prompt = build_extraction_prompt(categories)
        url = f"{self.endpoint_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        user_content = (
            f"Nombre del archivo: {pdf_content.filename}\n\n"
            f"Contenido de la factura:\n{pdf_content.text}"
        )
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        raw_text = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        parsed = _parse_json_from_llm_output(raw_text)
        return InvoiceExtraction(**parsed)


class AnthropicProvider(BaseLLMProvider):
    """Proveedor oficial de Anthropic (Claude API) con soporte nativo para PDFs y texto."""

    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-haiku-latest",
        endpoint_url: str = "https://api.anthropic.com/v1",
        timeout: float = 45.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint_url = endpoint_url.rstrip("/")
        self.timeout = timeout

    def extract_invoice(
        self,
        pdf_content: PDFContent,
        categories: Optional[List[Dict[str, str]]] = None,
    ) -> InvoiceExtraction:
        prompt = build_extraction_prompt(categories)
        url = f"{self.endpoint_url}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        content_blocks: List[Dict[str, Any]] = []
        if pdf_content.has_text_layer and pdf_content.text.strip():
            content_blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"Nombre del archivo: {pdf_content.filename}\n\n"
                        f"Texto extraído del PDF:\n{pdf_content.text}"
                    ),
                }
            )
        else:
            content_blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_content.base64_pdf,
                    },
                }
            )
        content_blocks.append({"type": "text", "text": prompt})

        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": content_blocks}],
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        blocks = data.get("content") or []
        raw_text = blocks[0].get("text", "{}") if blocks else "{}"
        parsed = _parse_json_from_llm_output(raw_text)
        return InvoiceExtraction(**parsed)


class HeuristicInvoiceExtractor(BaseLLMProvider):
    """
    Extractor determinista y heurístico local para facturas españolas.
    Funciona sin conexión a internet ni clave de API (ideal para Fase 0/1, tests y fallback local).
    """

    provider_name = "heuristico_local"

    def extract_invoice(
        self,
        pdf_content: PDFContent,
        categories: Optional[List[Dict[str, str]]] = None,
    ) -> InvoiceExtraction:
        text = pdf_content.text or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        if not text.strip():
            # PDF escaneado sin texto ni modelo de visión activo
            return InvoiceExtraction(
                es_factura=True,
                proveedor="",
                nif="",
                numero_factura="",
                fecha_emision=None,
                fecha_vencimiento=None,
                base_imponible=0.0,
                iva=0.0,
                total=0.0,
                tipo_documento="otros",
                categoria="otros",
                nivel_confianza=0.25,
                datos_extra={"nota_extraccion": "PDF sin capa de texto; requiere revisión manual o clave Vision LLM"},
            )

        # 1. Proveedor y NIF
        supplier = _extract_supplier(lines, text)
        nif = _extract_nif(text)

        # 2. Número de factura
        invoice_number = _extract_invoice_number(text)

        # 3. Fechas de emisión y vencimiento
        issue_date, due_date = _extract_dates(text)

        # 4. Tipo de documento y Categoría
        doc_type, category = _classify_document_and_category(text, supplier)
        is_credit_note = doc_type == "abono"

        # 5. Importes (Base imponible, IVA, Recargo equivalencia, IRPF, Total)
        amounts = _extract_amounts(text, is_credit_note=is_credit_note)

        # 6. Confianza base según campos localizados
        confidence = 0.55
        if supplier:
            confidence += 0.10
        if nif:
            confidence += 0.10
        if invoice_number:
            confidence += 0.10
        if issue_date:
            confidence += 0.08
        if abs(amounts["total"]) > 0:
            confidence += 0.05

        datos_extra: Dict[str, Any] = {
            "recargo_equivalencia": amounts["recargo_equivalencia"],
            "irpf": amounts["irpf"],
            "forma_pago": _extract_payment_method(text),
        }
        iban_match = re.search(r"\b(ES\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4})\b", text)
        if iban_match:
            datos_extra["iban_pago"] = re.sub(r"[\s-]", "", iban_match.group(1))

        return InvoiceExtraction(
            es_factura=True,
            proveedor=supplier,
            nif=nif,
            numero_factura=invoice_number,
            fecha_emision=issue_date,
            fecha_vencimiento=due_date,
            base_imponible=amounts["base_imponible"],
            iva=amounts["iva"],
            total=amounts["total"],
            moneda="EUR",
            tipo_documento=doc_type,
            categoria=category,
            nivel_confianza=round(min(confidence, 0.98), 2),
            datos_extra=datos_extra,
        )


def validate_invoice_extraction(
    extraction: InvoiceExtraction,
    umbral_confianza: float = 0.85,
    tolerance: float = 0.05,
) -> ValidationResult:
    """
    Valida el JSON extraído del modelo según la Sección 5 de las especificaciones:
    - Comprueba que base_imponible + iva (+ recargo_equivalencia - irpf) cuadra con el total.
    - Comprueba que las fechas son válidas y coherentes (vencimiento >= emisión).
    - Comprueba que los campos obligatorios existen.
    - Ajusta el nivel de confianza y decide si queda 'confirmada' o 'pendiente_revision'.
    """
    errores: List[str] = []
    advertencias: List[str] = []
    confianza = float(extraction.nivel_confianza or 0.0)

    # 1. Campos obligatorios
    campos_obligatorios_ok = True
    if not extraction.proveedor or not extraction.proveedor.strip():
        errores.append("Falta el nombre del proveedor.")
        campos_obligatorios_ok = False
        confianza -= 0.25

    if not extraction.numero_factura or not extraction.numero_factura.strip():
        errores.append("Falta el número de factura.")
        campos_obligatorios_ok = False
        confianza -= 0.20

    if not extraction.fecha_emision:
        errores.append("Falta la fecha de emisión.")
        campos_obligatorios_ok = False
        confianza -= 0.20

    if abs(extraction.total) < 0.001:
        errores.append("El importe total es 0 o no se ha podido extraer.")
        campos_obligatorios_ok = False
        confianza -= 0.30

    if not extraction.nif or not extraction.nif.strip():
        advertencias.append("No se ha detectado el NIF/CIF del proveedor.")
        confianza -= 0.08

    # 2. Validación de fechas y su coherencia
    fechas_coherentes = True
    dt_emision: Optional[dt.date] = None
    dt_vencimiento: Optional[dt.date] = None

    if extraction.fecha_emision:
        dt_emision = _parse_iso_date(extraction.fecha_emision)
        if not dt_emision:
            errores.append(f"Fecha de emisión inválida: '{extraction.fecha_emision}'.")
            fechas_coherentes = False
            confianza -= 0.20
        elif dt_emision > dt.date.today() + dt.timedelta(days=60):
            advertencias.append("La fecha de emisión está demasiado lejos en el futuro.")
            confianza -= 0.10

    if extraction.fecha_vencimiento:
        dt_vencimiento = _parse_iso_date(extraction.fecha_vencimiento)
        if not dt_vencimiento:
            advertencias.append(f"Fecha de vencimiento con formato no válido: '{extraction.fecha_vencimiento}'.")
            fechas_coherentes = False
            confianza -= 0.10
        elif dt_emision and dt_vencimiento < dt_emision:
            errores.append(
                f"Incoherencia de fechas: vencimiento ({extraction.fecha_vencimiento}) "
                f"anterior a emisión ({extraction.fecha_emision})."
            )
            fechas_coherentes = False
            confianza -= 0.25

    # 3. Validación aritmética de importes: base + iva + recargo - irpf == total
    recargo = float((extraction.datos_extra or {}).get("recargo_equivalencia") or 0.0)
    irpf = float((extraction.datos_extra or {}).get("irpf") or 0.0)

    # En abonos con base negativa e irpf positivo, ajustamos signo si procede
    if extraction.total < 0 and extraction.base_imponible > 0:
        extraction.base_imponible = -abs(extraction.base_imponible)
        extraction.iva = -abs(extraction.iva)
        recargo = -abs(recargo)

    if extraction.base_imponible < 0 and irpf > 0:
        expected_total = round(extraction.base_imponible + extraction.iva + recargo + irpf, 2)
    else:
        expected_total = round(extraction.base_imponible + extraction.iva + recargo - irpf, 2)

    diff = abs(expected_total - round(extraction.total, 2))
    cuadra_importes = diff <= tolerance

    if not cuadra_importes:
        errores.append(
            f"Descuadre aritmético: Base ({extraction.base_imponible:.2f}) + IVA ({extraction.iva:.2f})"
            + (f" + R.E. ({recargo:.2f})" if recargo else "")
            + (f" - IRPF ({irpf:.2f})" if irpf else "")
            + f" = {expected_total:.2f} €, pero el Total indicado es {extraction.total:.2f} € (dif: {diff:.2f} €)."
        )
        confianza -= 0.30

    # Si es un abono pero tiene importe positivo, advertir
    if extraction.tipo_documento == "abono" and extraction.total > 0:
        advertencias.append("El documento es un abono pero el importe total figura en positivo.")
        confianza -= 0.10

    confianza_ajustada = round(max(0.0, min(1.0, confianza)), 2)
    valido = campos_obligatorios_ok and fechas_coherentes and cuadra_importes and len(errores) == 0

    if valido and confianza_ajustada >= umbral_confianza:
        estado_revision = "confirmada"
    else:
        estado_revision = "pendiente_revision"

    return ValidationResult(
        valido=valido,
        cuadra_importes=cuadra_importes,
        fechas_coherentes=fechas_coherentes,
        campos_obligatorios_completos=campos_obligatorios_ok,
        errores=errores,
        advertencias=advertencias,
        confianza_ajustada=confianza_ajustada,
        estado_revision=estado_revision,
    )


class LLMProcessor:
    """Orquestador del procesado de facturas PDF con LLM + fallback y validación."""

    def __init__(
        self,
        provider_type: str = "google_ai_studio",
        api_key: Optional[str] = None,
        model: str = "gemini-2.0-flash",
        endpoint_url: Optional[str] = None,
        ocr_strategy: str = "vision_llm",
        umbral_confianza: float = 0.85,
        categories: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        self.provider_type = provider_type
        self.api_key = (api_key or "").strip()
        self.model = model
        self.endpoint_url = endpoint_url
        self.ocr_strategy = ocr_strategy
        self.umbral_confianza = umbral_confianza
        self.categories = categories or CATEGORIAS_INICIALES

    def _build_provider(self) -> BaseLLMProvider:
        if self.provider_type == "heuristico_local" or not self.api_key:
            return HeuristicInvoiceExtractor()
        if self.provider_type == "openai_compatible":
            return OpenAICompatibleProvider(
                api_key=self.api_key,
                model=self.model or "gpt-4o-mini",
                endpoint_url=self.endpoint_url or "https://api.openai.com/v1",
            )
        if self.provider_type == "anthropic":
            return AnthropicProvider(
                api_key=self.api_key,
                model=self.model or "claude-3-5-haiku-latest",
                endpoint_url=self.endpoint_url or "https://api.anthropic.com/v1",
            )
        return GoogleAIStudioProvider(
            api_key=self.api_key,
            model=self.model or "gemini-2.0-flash",
            endpoint_url=self.endpoint_url or "https://generativelanguage.googleapis.com/v1beta",
        )

    def process_pdf(
        self,
        pdf_source: Union[bytes, Path, str],
        filename: str = "documento.pdf",
        sender_rule_overrides: Optional[Dict[str, Any]] = None,
    ) -> ProcessedPDFResult:
        """
        Ejecuta el flujo completo de la Fase 1 para un PDF:
        1. Extrae texto o prepara visión/OCR y calcula hash SHA-256.
        2. Invoca al proveedor de modelo configurado (con fallback heurístico si no hay clave o falla la red).
        3. Aplica reglas por remitente/proveedor si existen.
        4. Valida importes, fechas y campos obligatorios, calculando la confianza final y estado de revisión.
        """
        pdf_content = extract_pdf_content(
            pdf_source=pdf_source,
            filename=filename,
            ocr_strategy=self.ocr_strategy,
        )

        provider = self._build_provider()
        provider_used = provider.provider_name
        fallback_warning: Optional[str] = None

        try:
            extraction = provider.extract_invoice(pdf_content, categories=self.categories)
        except Exception as exc:
            logger.warning(
                "Error llamando al proveedor LLM '%s' (%s). Usando extractor heurístico local de respaldo.",
                provider.provider_name,
                exc,
            )
            fallback = HeuristicInvoiceExtractor()
            extraction = fallback.extract_invoice(pdf_content, categories=self.categories)
            provider_used = f"{provider.provider_name}->heuristico_local"
            fallback_warning = f"Se usó extracción local de respaldo por error en API externa: {exc}"

        # Aplicar reglas por remitente o proveedor si se han configurado
        if sender_rule_overrides:
            if sender_rule_overrides.get("proveedor") and not extraction.proveedor:
                extraction.proveedor = sender_rule_overrides["proveedor"]
            elif sender_rule_overrides.get("forzar_proveedor") and sender_rule_overrides.get("proveedor"):
                extraction.proveedor = sender_rule_overrides["proveedor"]
            if sender_rule_overrides.get("nif") and not extraction.nif:
                extraction.nif = sender_rule_overrides["nif"]
            if sender_rule_overrides.get("categoria"):
                extraction.categoria = sender_rule_overrides["categoria"]
            if sender_rule_overrides.get("tipo_documento"):
                extraction.tipo_documento = sender_rule_overrides["tipo_documento"]

        validacion = validate_invoice_extraction(
            extraction=extraction,
            umbral_confianza=self.umbral_confianza,
        )
        if fallback_warning:
            validacion.advertencias.append(fallback_warning)

        extraction.nivel_confianza = validacion.confianza_ajustada

        return ProcessedPDFResult(
            hash_pdf=pdf_content.hash_sha256,
            metodo_extraccion_texto=pdf_content.extraction_method,
            texto_extraido_longitud=len(pdf_content.text),
            extraccion=extraction,
            validacion=validacion,
            proveedor_llm_usado=provider_used,
        )


# =====================================================================
# Funciones auxiliares de parseo heurístico y limpieza de JSON
# =====================================================================


def _parse_json_from_llm_output(raw_text: str) -> Dict[str, Any]:
    """Extrae y parsea el objeto JSON devuelto por un LLM, limpiando bloques markdown si existen."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("La respuesta del modelo no es un objeto JSON.")
    return data


def _parse_iso_date(date_str: str) -> Optional[dt.date]:
    """Intenta parsear una fecha en formato YYYY-MM-DD o DD/MM/YYYY."""
    s = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_date_to_iso(date_str: str) -> Optional[str]:
    parsed = _parse_iso_date(date_str)
    return parsed.isoformat() if parsed else None


def _parse_spanish_number(raw: str) -> float:
    """Convierte un importe en formato español ('1.234,56' o '1234.56' o '- 45,20') a float."""
    s = raw.strip().replace("€", "").replace("EUR", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    return round(float(s), 2)


def _extract_nif(text: str) -> str:
    """Extrae el NIF/CIF del proveedor (evitando el NIF del cliente si está etiquetado)."""
    labeled = re.search(
        r"(?:Proveedor|Emisor).*?(?:NIF|CIF|C\.I\.F\.|N\.I\.F\.)\s*[:#]?\s*([A-Z0-9][-\s]?[0-9]{7}[-\s]?[A-Z0-9])",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if labeled:
        return re.sub(r"[-\s]", "", labeled.group(1)).upper()

    matches = NIF_REGEX.findall(text)
    if matches:
        return re.sub(r"[-\s]", "", matches[0]).upper()
    return ""


def _extract_supplier(lines: List[str], full_text: str) -> str:
    """Extrae el nombre del proveedor emisor de la factura."""
    explicit = re.search(
        r"(?:Proveedor|Emisor|Raz[oó]n\s+Social)\s*[:]\s*([^\n\r]+)",
        full_text,
        re.IGNORECASE,
    )
    if explicit:
        candidate = explicit.group(1).strip()
        # Si en la misma línea viene NIF/CIF al final, limpiarlo
        candidate = re.split(r"\s+(?:NIF|CIF)\s*[:]", candidate, flags=re.IGNORECASE)[0].strip()
        if candidate:
            return candidate

    skip_prefixes = ("factura", "abono", "rectificativa", "fecha", "cliente", "farmacia", "página", "pagina", "nif", "cif")
    for line in lines[:8]:
        low = line.lower()
        if not any(low.startswith(p) for p in skip_prefixes) and len(line) >= 3:
            return line
    return lines[0] if lines else ""


def _extract_invoice_number(text: str) -> str:
    """Extrae el número o código de factura."""
    patterns = [
        r"(?:N[úu]mero\s+de\s+factura|N[º°o]\s*Factura|Factura\s+N[º°o]?|Abono\s+N[º°o]?|Rectificativa\s+N[º°o]?|Fra\.?\s*N[º°o]?)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-_]{1,25})",
        r"\b((?:FRA|FAC|F|AB|RECT)[-/]\d{2,4}[-/]\d{1,8})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val.lower() not in ("de", "del", "fecha", "total", "cliente"):
                return val
    return ""


def _extract_dates(text: str) -> tuple[Optional[str], Optional[str]]:
    """Extrae fecha de emisión y fecha de vencimiento normalizadas a YYYY-MM-DD."""
    date_token = r"(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})"
    issue_match = re.search(
        rf"(?:Fecha\s+(?:de\s+)?emisi[oó]n|Fecha\s+factura|Fecha)\s*[:#]?\s*{date_token}",
        text,
        re.IGNORECASE,
    )
    due_match = re.search(
        rf"(?:Fecha\s+(?:de\s+)?vencimiento|Vencimiento|Vto\.?)\s*[:#]?\s*{date_token}",
        text,
        re.IGNORECASE,
    )

    issue_iso = _normalize_date_to_iso(issue_match.group(1)) if issue_match else None
    due_iso = _normalize_date_to_iso(due_match.group(1)) if due_match else None

    if not issue_iso:
        all_dates = re.findall(date_token, text)
        if all_dates:
            issue_iso = _normalize_date_to_iso(all_dates[0])
            if not due_iso and len(all_dates) > 1:
                due_iso = _normalize_date_to_iso(all_dates[1])

    return issue_iso, due_iso


def _extract_amounts(text: str, is_credit_note: bool = False) -> Dict[str, float]:
    """Extrae Base Imponible, IVA, Recargo de Equivalencia, IRPF y Total."""
    num_pat = r"([-+]?\s*\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|[-+]?\s*\d+[.,]\d{2})"

    def find_amount(labels: List[str]) -> Optional[float]:
        for lbl in labels:
            m = re.search(rf"{lbl}\s*[:=]?\s*{num_pat}", text, re.IGNORECASE)
            if m:
                try:
                    return _parse_spanish_number(m.group(1))
                except ValueError:
                    continue
        return None

    base = find_amount([
        r"Base\s+Imponible(?:\s+Total)?",
        r"Subtotal",
        r"Importe\s+Bruto",
        r"Base",
    ])
    iva = find_amount([
        r"Cuota\s+IVA",
        r"Importe\s+IVA",
        r"I\.?V\.?A\.?(?:\s+\d+(?:[.,]\d+)?\s*%)?",
    ])
    recargo = find_amount([
        r"Recargo\s+(?:de\s+)?Equivalencia(?:\s+\d+(?:[.,]\d+)?\s*%)?",
        r"Cuota\s+R\.?E\.?",
        r"R\.?\s*Equiv\.?",
    ]) or 0.0
    irpf = find_amount([
        r"Retenci[oó]n\s+I\.?R\.?P\.?F\.?(?:\s+\d+(?:[.,]\d+)?\s*%)?",
        r"I\.?R\.?P\.?F\.?(?:\s+\d+(?:[.,]\d+)?\s*%)?",
    ]) or 0.0
    total = find_amount([
        r"Total\s+Factura",
        r"Total\s+a\s+Pagar",
        r"Importe\s+Total",
        r"Total\s+Abono",
        r"Total\s+EUR",
        r"\bTotal\b",
    ])

    base_val = base if base is not None else 0.0
    iva_val = iva if iva is not None else 0.0
    total_val = total if total is not None else round(base_val + iva_val + recargo - abs(irpf), 2)

    if is_credit_note and total_val > 0 and "rectificativa" in text.lower() and "-" in text:
        pass

    return {
        "base_imponible": base_val,
        "iva": iva_val,
        "recargo_equivalencia": recargo,
        "irpf": abs(irpf),
        "total": total_val,
    }


def _classify_document_and_category(text: str, supplier: str) -> tuple[str, str]:
    """Clasifica el tipo de documento y la categoría según el contenido y proveedor."""
    combined = f"{supplier} {text}".lower()

    if any(w in combined for w in ("abono", "factura rectificativa", "nota de crédito", "devolución de mercancía")):
        return "abono", "compras_mercancia"

    if any(w in combined for w in ("arrendamiento", "alquiler local", "renta mensual", "alquiler de local")):
        return "alquiler", "alquiler"

    if any(
        w in combined
        for w in (
            "iberdrola",
            "endesa",
            "naturgy",
            "repsol",
            "canal de isabel",
            "aguas de",
            "telefónica",
            "telefonica",
            "movistar",
            "vodafone",
            "orange",
            "electricidad",
            "suministro eléctrico",
            "fibra óptica",
        )
    ):
        return "factura_servicios", "suministros"

    if any(w in combined for w in ("asesoría", "asesoria", "gestoría", "gestoria", "notaría", "abogado", "mantenimiento informático", "consultoría")):
        return "factura_servicios", "servicios_profesionales"

    if any(w in combined for w in ("mapfre", "allianz", "axa", "mutua", "póliza", "poliza de seguro", "seguro multirriesgo")):
        return "factura_servicios", "seguros"

    if any(w in combined for w in ("tasa municipal", "impuesto", "colegio oficial de farmacéuticos", "cuota colegial")):
        return "factura_gasto", "impuestos_tasas"

    if any(w in combined for w in ("bolsas", "etiquetas", "papel térmico", "material de oficina", "mobiliario", "tóner")):
        return "factura_gasto", "gastos_material"

    if any(
        w in combined
        for w in (
            "distribuidora",
            "cooperativa farmaceutica",
            "cooperativa farmacéutica",
            "almacen farmaceutico",
            "almacén farmacéutico",
            "laboratorio",
            "laboratorios",
            "medicamentos",
            "parafarmacia",
            "especialidades farmacéuticas",
            "especialidades farmaceuticas",
            "recargo de equivalencia",
        )
    ):
        return "factura_mercancia", "compras_mercancia"

    return "factura_mercancia", "compras_mercancia"


def _extract_payment_method(text: str) -> str:
    low = text.lower()
    if "domiciliaci" in low or "recibo domiciliado" in low or "adeudo sepa" in low:
        return "domiciliacion"
    if "transferencia" in low:
        return "transferencia"
    if "tarjeta" in low:
        return "tarjeta"
    if "giro" in low:
        return "giro"
    return "domiciliacion"


def main_cli(argv: Optional[List[str]] = None) -> int:
    """CLI de Fase 1: procesa un archivo PDF o carpeta de PDFs y muestra el JSON validado."""
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("Uso: python -m app.services.llm_processor <ruta_pdf>")
        return 1

    processor = LLMProcessor()
    for arg in args:
        path = Path(arg)
        result = processor.process_pdf(path, filename=path.name)
        print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
