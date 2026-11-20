"""Tests de selección de modelo, direcciones de servicio y llamadas a proveedores de IA (sin red)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from app.config import (
    ANTHROPIC_ENDPOINT,
    GOOGLE_ENDPOINT,
    OPENAI_ENDPOINT,
    resolve_llm_endpoint,
    resolve_llm_model,
)
from app.services import llm_processor
from app.services.llm_processor import (
    AnthropicProvider,
    GoogleAIStudioProvider,
    HeuristicInvoiceExtractor,
    LLMProcessor,
    OpenAICompatibleProvider,
    describe_llm_error,
)
from app.services.pdf_extractor import PDFContent

VALID_JSON = json.dumps(
    {
        "es_factura": True,
        "proveedor": "Distribuidora S.L.",
        "nif": "B12345678",
        "numero_factura": "F-1",
        "fecha_emision": "2026-11-01",
        "base_imponible": 100.0,
        "iva": 21.0,
        "total": 121.0,
        "nivel_confianza": 0.95,
    }
)


def _sample_pdf() -> PDFContent:
    return PDFContent(
        pdf_bytes=b"%PDF-1.4",
        hash_sha256="0" * 64,
        text="Factura F-1 Total 121,00",
        page_count=1,
        has_text_layer=True,
        extraction_method="texto_nativo",
        filename="f.pdf",
    )


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> List[httpx.Request]:
    seen: List[httpx.Request] = []
    real_client = httpx.Client

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(recording_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_processor.httpx, "Client", fake_client)
    return seen


def test_resolve_model_replaces_retired_and_foreign_models() -> None:
    assert resolve_llm_model("google_ai_studio", "gemini-2.0-flash") == "gemini-flash-lite-latest"
    assert resolve_llm_model("google_ai_studio", "gemini-1.5-pro") == "gemini-flash-lite-latest"
    assert resolve_llm_model("google_ai_studio", "") == "gemini-flash-lite-latest"
    assert resolve_llm_model("google_ai_studio", "gemini-pro-latest") == "gemini-pro-latest"
    assert resolve_llm_model("google_ai_studio", "gemini-flash-latest") == "gemini-flash-latest"
    # Modelo de otro proveedor que quedó guardado al cambiar de proveedor
    assert resolve_llm_model("anthropic", "gemini-flash-lite-latest") == "claude-haiku-5-5"
    assert resolve_llm_model("anthropic", "claude-3-5-haiku-latest") == "claude-haiku-5-5"
    # Modelos personalizados (p. ej. Ollama) se respetan
    assert resolve_llm_model("openai_compatible", "llama3.1") == "llama3.1"


def test_resolve_endpoint_ignores_other_provider_default() -> None:
    assert resolve_llm_endpoint("openai_compatible", GOOGLE_ENDPOINT) == OPENAI_ENDPOINT
    assert resolve_llm_endpoint("anthropic", GOOGLE_ENDPOINT) == ANTHROPIC_ENDPOINT
    assert resolve_llm_endpoint("anthropic", "") == ANTHROPIC_ENDPOINT
    assert resolve_llm_endpoint("openai_compatible", "http://localhost:11434/v1/") == "http://localhost:11434/v1"


def test_processor_builds_right_provider() -> None:
    proc = LLMProcessor(provider_type="openai_compatible", api_key="sk-x", endpoint_url=GOOGLE_ENDPOINT)
    prov = proc._build_provider()
    assert isinstance(prov, OpenAICompatibleProvider)
    assert prov.endpoint_url == OPENAI_ENDPOINT

    # Ollama local sin clave debe usar la IA local, no el modo heurístico
    local = LLMProcessor(provider_type="openai_compatible", api_key="", endpoint_url="http://localhost:11434/v1", model="llama3.1")
    assert isinstance(local._build_provider(), OpenAICompatibleProvider)

    # OpenAI oficial sin clave -> modo básico
    assert isinstance(LLMProcessor(provider_type="openai_compatible", api_key="")._build_provider(), HeuristicInvoiceExtractor)

    google = LLMProcessor(provider_type="google_ai_studio", api_key="AIza", model="gemini-2.0-flash")._build_provider()
    assert isinstance(google, GoogleAIStudioProvider)
    assert google.model == "gemini-flash-lite-latest"


def test_google_key_sent_in_header_and_retries_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "gemini-flash-latest" in request.url.path:
            return httpx.Response(404, json={"error": {"message": "model not found"}})
        body = {
            "candidates": [
                {"content": {"parts": [{"text": "pensando...", "thought": True}, {"text": VALID_JSON}]}}
            ]
        }
        return httpx.Response(200, json=body)

    seen = _install_transport(monkeypatch, handler)
    prov = GoogleAIStudioProvider(api_key="AIzaSECRETO", model="gemini-flash-latest")
    ext = prov.extract_invoice(_sample_pdf())

    assert ext.total == 121.0
    assert prov.model == "gemini-flash-lite-latest"
    assert len(seen) == 2
    for req in seen:
        assert "AIzaSECRETO" not in str(req.url)
        assert req.headers["x-goog-api-key"] == "AIzaSECRETO"


def test_anthropic_payload_has_no_temperature_and_reads_text_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": VALID_JSON}],
            },
        )

    _install_transport(monkeypatch, handler)
    ext = AnthropicProvider(api_key="sk-ant-x").extract_invoice(_sample_pdf())
    assert ext.total == 121.0
    assert captured["model"] == "claude-haiku-5-5"
    assert "temperature" not in captured


def test_openai_reasoning_models_skip_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": VALID_JSON}}]})

    _install_transport(monkeypatch, handler)
    OpenAICompatibleProvider(api_key="sk", model="gpt-5-mini").extract_invoice(_sample_pdf())
    OpenAICompatibleProvider(api_key="", model="llama3.1", endpoint_url="http://localhost:11434/v1").extract_invoice(_sample_pdf())
    assert "temperature" not in payloads[0]
    assert payloads[1]["temperature"] == 0.1


def test_describe_llm_error_hides_url_and_explains_quota() -> None:
    req = httpx.Request("POST", "https://example.test/models/x:generateContent?key=AIzaSECRETO")
    resp = httpx.Response(429, request=req, json={"error": {"message": "Quota exceeded"}})
    exc = httpx.HTTPStatusError("boom " + str(req.url), request=req, response=resp)
    msg = describe_llm_error(exc)
    assert "AIzaSECRETO" not in msg
    assert "límite de usos" in msg
