"""Tests de Fase 1: Extracción de PDF, Procesador LLM y Validación Aritmética y Temporal."""

from __future__ import annotations

from app.models.schemas import InvoiceExtraction
from app.services.llm_processor import LLMProcessor, validate_invoice_extraction
from app.services.pdf_extractor import compute_pdf_hash, extract_pdf_content, looks_like_invoice
from scripts.generate_sample_data import build_invoice_pdf_bytes, get_sample_invoices_specs


def test_pdf_hash_and_text_extraction():
    spec = get_sample_invoices_specs()[0]
    pdf_bytes = build_invoice_pdf_bytes(spec["lines"])

    h1 = compute_pdf_hash(pdf_bytes)
    h2 = compute_pdf_hash(pdf_bytes)
    assert len(h1) == 64
    assert h1 == h2

    content = extract_pdf_content(pdf_bytes, filename="cofares.pdf")
    assert content.has_text_layer is True
    assert content.extraction_method == "texto_nativo"
    assert "Cofares" in content.text
    assert looks_like_invoice(content, filename="cofares.pdf") is True


def test_scanned_pdf_without_text_uses_vision_fallback():
    # PDF vacío sin texto -> se marca para vision_llm
    empty_pdf = build_invoice_pdf_bytes([])
    content = extract_pdf_content(empty_pdf, filename="escaneada.pdf", ocr_strategy="vision_llm")
    assert content.has_text_layer is False
    assert content.extraction_method == "vision_llm"
    assert looks_like_invoice(content, filename="documento_adjunto.pdf") is True


def test_llm_processor_extracts_pharmacy_invoice_with_recargo_equivalencia():
    spec = get_sample_invoices_specs()[0]  # Cofares: Base 1200 + IVA 48 + RE 6 = 1254
    pdf_bytes = build_invoice_pdf_bytes(spec["lines"])

    processor = LLMProcessor(provider_type="heuristico_local", umbral_confianza=0.85)
    result = processor.process_pdf(pdf_bytes, filename=spec["filename"])

    assert result.extraccion.proveedor.startswith("Cofares")
    assert result.extraccion.nif == "F28012345"
    assert result.extraccion.numero_factura == "FRA-2026-1001"
    assert result.extraccion.fecha_emision == "2026-11-02"
    assert result.extraccion.fecha_vencimiento == "2026-11-15"
    assert result.extraccion.base_imponible == 1200.00
    assert result.extraccion.iva == 48.00
    assert result.extraccion.datos_extra["recargo_equivalencia"] == 6.00
    assert result.extraccion.total == 1254.00
    assert result.extraccion.categoria == "compras_mercancia"
    assert result.validacion.valido is True
    assert result.validacion.cuadra_importes is True
    assert result.validacion.estado_revision == "confirmada"


def test_validator_detects_math_mismatch_and_sends_to_review():
    # Factura con descuadre aritmético: 100 + 21 != 135
    ext = InvoiceExtraction(
        proveedor="FarmaPack S.L.",
        nif="B12345678",
        numero_factura="FRA-4012",
        fecha_emision="2026-11-14",
        fecha_vencimiento="2026-11-28",
        base_imponible=100.0,
        iva=21.0,
        total=135.0,
        nivel_confianza=0.95,
    )
    val = validate_invoice_extraction(ext, umbral_confianza=0.85)
    assert val.cuadra_importes is False
    assert val.valido is False
    assert val.estado_revision == "pendiente_revision"
    assert any("Descuadre aritmético" in e for e in val.errores)


def test_validator_detects_incoherent_dates():
    ext = InvoiceExtraction(
        proveedor="Proveedor Test",
        nif="B12345678",
        numero_factura="FRA-01",
        fecha_emision="2026-11-20",
        fecha_vencimiento="2026-11-05",  # Vencimiento anterior a emisión
        base_imponible=100.0,
        iva=21.0,
        total=121.0,
        nivel_confianza=0.95,
    )
    val = validate_invoice_extraction(ext, umbral_confianza=0.85)
    assert val.fechas_coherentes is False
    assert val.estado_revision == "pendiente_revision"
