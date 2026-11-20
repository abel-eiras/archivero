"""Tests de reglas de negocio: facturas sustituidas, carpeta de datos, remitentes exactos y protección del servidor local."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import openpyxl
import pytest
from fastapi.testclient import TestClient

import app.config as config
from app.db.database import Database, sender_matches_pattern
from app.main import create_app
from app.models.schemas import InvoiceExtraction
from app.services.credentials import CredentialManager
from app.services.data_location import change_data_dir
from app.services.excel_exporter import AccountingExcelExporter
from app.services.llm_processor import validate_invoice_extraction
from app.services.mail_poller import MailPollerService
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.reconciliation import BankReconciliationService
from app.services.storage import LocalFolderStorage


# ---------------------------------------------------------------------
# A) Factura rectificativa que sustituye a la anterior
# ---------------------------------------------------------------------


class _FakeProcessor:
    def __init__(self, ext: InvoiceExtraction) -> None:
        self.ext = ext

    def process_pdf(self, pdf_source: Any, filename: str = "", sender_rule_overrides: Any = None) -> Any:
        ext = self.ext.model_copy(deep=True)
        return SimpleNamespace(extraccion=ext, validacion=validate_invoice_extraction(ext, umbral_confianza=0.5))


def _ext(numero: str, nif: str = "B12345678", proveedor: str = "Cofares S.A.", total: float = 121.0) -> InvoiceExtraction:
    return InvoiceExtraction(
        es_factura=True,
        proveedor=proveedor,
        nif=nif,
        numero_factura=numero,
        fecha_emision="2026-01-10",
        fecha_vencimiento="2026-02-10",
        base_imponible=100.0,
        iva=21.0,
        total=total,
        nivel_confianza=0.99,
    )


def _ingest(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ext: InvoiceExtraction, payload: bytes):
    monkeypatch.setattr(InvoiceIngestionPipeline, "_build_llm_processor", lambda self, s: _FakeProcessor(ext))
    storage = LocalFolderStorage(base_output_dir=tmp_path / "archivo", staging_dir=tmp_path / "staging")
    pipeline = InvoiceIngestionPipeline(db=db, storage_override=storage)
    return pipeline.ingest_pdf(b"%PDF-1.4 " + payload, filename="f.pdf")


def test_same_supplier_and_number_replaces_previous_invoice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(db_path=tmp_path / "db.sqlite", backup_dir=tmp_path / "backups")
    assert db.get_schema_version() >= 2

    r1 = _ingest(db, tmp_path, monkeypatch, _ext("FRA-2026/001"), b"v1")
    # Mismo número con otro formato (mayúsculas/espacios/guiones) y PDF distinto
    r2 = _ingest(db, tmp_path, monkeypatch, _ext("fra 2026 001", total=110.0), b"v2")
    assert r1.status == r2.status == "insertada"

    old = db.get_invoice_by_id(r1.invoice_id)
    new = db.get_invoice_by_id(r2.invoice_id)
    assert old["sustituida_por"] == r2.invoice_id
    assert new["sustituye_a"] == r1.invoice_id
    assert any(f"Sustituye a la factura #{r1.invoice_id}" in e for e in new["errores_validacion"])
    assert Path(old["ruta_archivo"]).is_file()  # la antigua conserva su archivo

    # PDF idéntico sigue siendo duplicado
    assert _ingest(db, tmp_path, monkeypatch, _ext("FRA-2026/001"), b"v2").status == "duplicada"

    # Excluida por defecto de listados, bandeja, dashboard, Excel y conciliación
    ids = [i["id"] for i in db.list_invoices()]
    assert r1.invoice_id not in ids and r2.invoice_id in ids
    assert r1.invoice_id in [i["id"] for i in db.list_invoices(incluir_sustituidas=True)]
    assert r1.invoice_id not in [i["id"] for i in db.list_invoices(estado_revision=old["estado_revision"])]
    metrics = db.get_dashboard_metrics()
    assert metrics["resumen"]["total_facturas"] == 1
    assert r1.invoice_id not in [i["id"] for i in metrics["pendientes_pago"]]

    wb = openpyxl.load_workbook(io.BytesIO(AccountingExcelExporter(db=db).generate_workbook_bytes()))
    ids_excel = [row[0].value for row in wb["Facturas Recibidas"].iter_rows(min_row=2)]
    assert r1.invoice_id not in ids_excel and r2.invoice_id in ids_excel

    db.insert_bank_transaction(
        {"banco": "B", "fecha": "2026-02-10", "importe": -121.0, "concepto": "PAGO", "hash_movimiento": "h1"}
    )
    BankReconciliationService(db).run_auto_reconciliation()
    with db.connect() as conn:
        linked = {r["factura_id"] for r in conn.execute("SELECT factura_id FROM vinculos_conciliacion")}
    assert r1.invoice_id not in linked

    # API: parámetro incluir_sustituidas
    app = create_app(db_path=db.db_path, backup_dir=tmp_path / "backups", start_background_poller=False)
    app.state.credentials = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert r1.invoice_id not in [i["id"] for i in client.get("/api/invoices").json()["items"]]
    assert r1.invoice_id in [
        i["id"] for i in client.get("/api/invoices?incluir_sustituidas=true").json()["items"]
    ]


def test_replacement_of_paid_invoice_forces_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(db_path=tmp_path / "db.sqlite", backup_dir=tmp_path / "backups")
    r1 = _ingest(db, tmp_path, monkeypatch, _ext("A-1", nif=""), b"v1")
    tx_id, _ = db.insert_bank_transaction(
        {"banco": "B", "fecha": "2026-02-10", "importe": -121.0, "concepto": "PAGO", "hash_movimiento": "h1"}
    )
    BankReconciliationService(db).reconcile_manual(
        tx_id, [{"factura_id": r1.invoice_id, "importe_asignado": 121.0}], aprender_regla=False
    )
    # Sin NIF: se compara por nombre de proveedor normalizado
    r2 = _ingest(db, tmp_path, monkeypatch, _ext("a1", nif="", proveedor="COFARES, S.A."), b"v2")
    new = db.get_invoice_by_id(r2.invoice_id)
    assert new["sustituye_a"] == r1.invoice_id
    assert new["estado_revision"] == "pendiente_revision"
    assert any("pagos del banco" in e for e in new["errores_validacion"])
    # Los vínculos NO se mueven automáticamente
    with db.connect() as conn:
        rows = conn.execute("SELECT factura_id FROM vinculos_conciliacion").fetchall()
    assert [r["factura_id"] for r in rows] == [r1.invoice_id]


def test_different_supplier_same_number_is_not_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(db_path=tmp_path / "db.sqlite", backup_dir=tmp_path / "backups")
    r1 = _ingest(db, tmp_path, monkeypatch, _ext("001", nif="B11111111"), b"v1")
    r2 = _ingest(db, tmp_path, monkeypatch, _ext("001", nif="B22222222"), b"v2")
    assert db.get_invoice_by_id(r1.invoice_id)["sustituida_por"] is None
    assert db.get_invoice_by_id(r2.invoice_id)["sustituye_a"] is None


def test_migration_v2_on_existing_v1_database_creates_backup(tmp_path: Path):
    from app.db import database as dbmod

    db_file = tmp_path / "old.sqlite"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(dbmod.MIGRATIONS[0][2])
        conn.execute("INSERT INTO schema_versions VALUES (1, 'v1', '2026-01-01')")
    db = Database(db_path=db_file, backup_dir=tmp_path / "backups")
    assert db.get_schema_version() >= 2
    assert list((tmp_path / "backups").glob("*pre_migration_v1*"))


# ---------------------------------------------------------------------
# B) Carpeta de datos seleccionable
# ---------------------------------------------------------------------


def test_data_dir_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bootstrap = tmp_path / "cfg" / "ubicacion.json"
    monkeypatch.setenv("ARCHIVERO_BOOTSTRAP_FILE", str(bootstrap))
    monkeypatch.setattr(config, "LEGACY_DATA_DIR", tmp_path / "legacy")
    monkeypatch.setattr(config, "default_user_data_dir", lambda: tmp_path / "porusuario")

    monkeypatch.setenv("ARCHIVERO_DATA_DIR", str(tmp_path / "env"))
    assert config.resolve_data_dir() == tmp_path / "env"
    monkeypatch.delenv("ARCHIVERO_DATA_DIR")

    assert config.resolve_data_dir() == tmp_path / "porusuario"
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "facturas.sqlite").write_bytes(b"")
    assert config.resolve_data_dir() == tmp_path / "legacy"

    config.write_bootstrap_data_dir(tmp_path / "elegida")
    assert json.loads(bootstrap.read_text(encoding="utf-8"))["data_dir"] == str(tmp_path / "elegida")
    assert config.resolve_data_dir() == tmp_path / "elegida"


def test_change_data_dir_copies_db_and_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARCHIVERO_BOOTSTRAP_FILE", str(tmp_path / "cfg" / "ubicacion.json"))
    monkeypatch.delenv("ARCHIVERO_DATA_DIR", raising=False)
    old_dir = tmp_path / "viejo"
    app = create_app(db_path=old_dir / "facturas.sqlite", backup_dir=old_dir / "backups", start_background_poller=False)
    creds = CredentialManager(vault_dir=old_dir, force_encrypted_fallback=True)
    creds.set_secret("clave", "secreta")
    app.state.credentials = creds
    app.state.db.set_settings({"marca_test": "ok"})
    client = TestClient(app, base_url="http://127.0.0.1")

    assert client.get("/api/settings").json()["carpeta_datos"] == str(old_dir.resolve())
    assert client.post("/api/settings/data-dir", json={"ruta": "relativa/no"}).status_code == 400

    new_dir = tmp_path / "nuevo"
    r = client.post("/api/settings/data-dir", json={"ruta": str(new_dir)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requiere_reinicio"] is True and "Cierra y vuelve a abrir Archivero" in body["mensaje"]
    assert body["base_datos_existente"] is False
    assert Database(db_path=new_dir / "facturas.sqlite", backup_dir=new_dir / "b").get_setting("marca_test") == "ok"
    assert CredentialManager(vault_dir=new_dir, force_encrypted_fallback=True).get_secret("clave") == "secreta"
    assert (old_dir / "facturas.sqlite").is_file()  # la carpeta antigua no se borra
    assert config.read_bootstrap_data_dir() == new_dir.resolve()

    # Si la carpeta destino ya tiene base de datos, se usa sin copiar
    res = change_data_dir(str(new_dir), current_db_path=tmp_path / "otra" / "facturas.sqlite", current_vault_dir=old_dir)
    assert res["base_datos_existente"] is True and res["archivos_copiados"] == []
    assert "existente" in res["mensaje"]


# ---------------------------------------------------------------------
# C) Remitentes permitidos con coincidencia exacta
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,sender,expected",
    [
        ("cofares.es", "x@cofares.es", True),
        ("cofares.es", "x@pedidos.cofares.es", True),
        ("cofares.es", "x@cofares.es.evil.com", False),
        ("cofares.es", "x@notcofares.es", False),
        ("@cofares.es", "X@COFARES.ES", True),
        ("facturas@cofares.es", "Facturas@Cofares.es", True),
        ("facturas@cofares.es", "otras.facturas@cofares.es", False),
        ("facturas@cofares.es", "facturas@cofares.es.evil.com", False),
        ("", "x@cofares.es", False),
    ],
)
def test_sender_matching_is_exact(pattern: str, sender: str, expected: bool):
    assert sender_matches_pattern(pattern, sender) is expected


def test_whitelist_and_sender_rules_use_exact_matching(tmp_path: Path):
    db = Database(db_path=tmp_path / "db.sqlite", backup_dir=tmp_path / "backups")
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    db.set_settings({"remitentes_globales": ["asesoria.es"]})
    poller = MailPollerService(db=db, credentials=creds)
    account: Dict[str, Any] = {"remitentes_permitidos": ["cofares.es", "jefe@hefame.es"]}
    assert poller.is_sender_in_whitelist("a@pedidos.cofares.es", account)
    assert poller.is_sender_in_whitelist("a@asesoria.es", account)
    assert poller.is_sender_in_whitelist("JEFE@hefame.es", account)
    assert not poller.is_sender_in_whitelist("a@cofares.es.evil.com", account)
    assert not poller.is_sender_in_whitelist("otro@hefame.es", account)

    db.create_rule(tipo_regla="remitente_categoria", patron="energia.es", categoria="suministros")
    assert db.match_rules_for_invoice(sender_email="f@energia.es").get("categoria") == "suministros"
    assert db.match_rules_for_invoice(sender_email="f@energia.es.evil.com") == {}


# ---------------------------------------------------------------------
# D) Protección del servidor local (DNS rebinding / CSRF)
# ---------------------------------------------------------------------


@pytest.fixture()
def local_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "web.sqlite", backup_dir=tmp_path / "backups", start_background_poller=False)
    app.state.credentials = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_evil_host_is_rejected(local_client: TestClient):
    r = local_client.get("/api/dashboard", headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    assert "Acceso denegado" in r.json()["detail"]
    assert local_client.get("/health", headers={"Host": "127.0.0.1.evil.com:8000"}).status_code == 403
    assert local_client.get("/health", headers={"Host": "localhost:8000"}).status_code == 200
    assert local_client.get("/health", headers={"Host": "[::1]:8000"}).status_code == 200


def test_cross_origin_post_is_rejected(local_client: TestClient):
    r = local_client.post("/api/bank/auto-reconcile", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    r = local_client.post("/api/bank/auto-reconcile", headers={"Referer": "http://evil.example.com/x"})
    assert r.status_code == 403
    assert local_client.post("/api/bank/auto-reconcile", headers={"Origin": "null"}).status_code == 403


def test_same_origin_requests_are_allowed(local_client: TestClient):
    assert local_client.get("/").status_code == 200
    assert local_client.get("/api/dashboard").status_code == 200
    assert local_client.post("/api/bank/auto-reconcile").status_code == 200
    assert (
        local_client.post("/api/bank/auto-reconcile", headers={"Origin": "http://127.0.0.1:8000"}).status_code
        == 200
    )
    assert (
        local_client.post(
            "/api/bank/auto-reconcile", headers={"Referer": "http://localhost:8000/#conciliacion"}
        ).status_code
        == 200
    )
    # El callback OAuth (GET de navegación desde Google/Microsoft) no se bloquea por su Referer
    r = local_client.get(
        "/api/mail/oauth/callback",
        params={"error": "access_denied", "state": "a@b.es"},
        headers={"Referer": "https://accounts.google.com/"},
    )
    assert r.status_code == 400 and "autorización" in r.text
