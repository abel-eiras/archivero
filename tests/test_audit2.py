"""Tests de la segunda auditoría: lector básico, conciliación, configuración, correo, subidas, archivo,
Excel, paginación, carpeta de datos, demo, copias diarias, OAuth, albaranes y abonos."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.config import OFFICIAL_GITHUB_REPO
from app.db.database import Database
from app.main import create_app
from app.models.schemas import InvoiceUpdatePayload
from app.services.credentials import CredentialManager
from app.services.excel_exporter import AccountingExcelExporter, tax_breakdown
from app.services.llm_processor import BASIC_READER_NOTICE, LLMProcessor, is_delivery_note_text
from app.services.mail_poller import EmailEnvelope, MailConnector, MailPollerService, PDFAttachment
from app.services.pdf_extractor import PDFContent, looks_like_invoice
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.reconciliation import BankReconciliationService, ReconciliationError, ReconciliationNotFound
from app.services.storage import (
    MAX_RELATIVE_PATH_LEN,
    build_archive_relative_parts,
    relocate_archived_pdf,
    sanitize_path_segment,
)
from scripts.generate_sample_data import build_invoice_pdf_bytes, get_sample_invoices_specs


def _db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "db.sqlite", backup_dir=tmp_path / "backups")


def _app(tmp_path: Path):
    app = create_app(db_path=tmp_path / "web.sqlite", backup_dir=tmp_path / "backups", start_background_poller=False)
    app.state.credentials = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    app.state.db.set_settings({"carpeta_salida": str(tmp_path / "archivo")})
    return app


def _client(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _invoice(db: Database, numero: str, total: float, **extra) -> int:
    data = {
        "proveedor_nombre": extra.pop("proveedor_nombre", "Distribuidora Norte"),
        "nif": extra.pop("nif", "B12345678"),
        "numero_factura": numero,
        "fecha_emision": extra.pop("fecha_emision", "2026-11-05"),
        "base_imponible": extra.pop("base_imponible", total),
        "iva": extra.pop("iva", 0.0),
        "total": total,
        "hash_pdf": f"hash-{numero}",
        "estado_revision": "confirmada",
    }
    data.update(extra)
    return db.insert_invoice(data)


def _mov(db: Database, importe: float, ref: str) -> int:
    mov_id, _ = db.insert_bank_transaction(
        {"fecha": "2026-11-10", "importe": importe, "concepto": f"pago {ref}", "hash_movimiento": f"mov-{ref}"}
    )
    return int(mov_id)


def _pdf(idx: int = 0) -> bytes:
    return build_invoice_pdf_bytes(get_sample_invoices_specs()[idx]["lines"])


# ---------------------------------------------------------------------
# 1. Lector básico en lugar de la IA elegida
# ---------------------------------------------------------------------


def test_missing_cloud_key_forces_review_and_caps_confidence():
    proc = LLMProcessor(provider_type="google_ai_studio", api_key="", umbral_confianza=0.5)
    res = proc.process_pdf(pdf_source=_pdf(0), filename="f.pdf")
    assert res.lectura_basica_sustituta is True
    assert res.validacion.estado_revision == "pendiente_revision"
    assert res.validacion.confianza_ajustada <= 0.6
    assert any(w.startswith(BASIC_READER_NOTICE) for w in res.validacion.advertencias)


def test_explicit_local_reader_keeps_its_own_scoring():
    proc = LLMProcessor(provider_type="heuristico_local", umbral_confianza=0.5)
    res = proc.process_pdf(pdf_source=_pdf(0), filename="f.pdf")
    assert res.lectura_basica_sustituta is False
    assert not any(w.startswith(BASIC_READER_NOTICE) for w in res.validacion.advertencias)
    # Respeta el umbral: con 0.99 nunca se confirma sola
    strict = LLMProcessor(provider_type="heuristico_local", umbral_confianza=0.99).process_pdf(_pdf(0), "f.pdf")
    assert strict.validacion.estado_revision == "pendiente_revision"


# ---------------------------------------------------------------------
# 2. Conciliación transaccional
# ---------------------------------------------------------------------


def test_manual_reconcile_rejects_excess_and_unknown_ids(tmp_path: Path):
    db = _db(tmp_path)
    inv = _invoice(db, "F-1", 100.0)
    mov = _mov(db, -150.0, "a")
    svc = BankReconciliationService(db=db)
    with pytest.raises(ReconciliationNotFound):
        svc.reconcile_manual(movimiento_id=99999, asignaciones=[{"factura_id": inv, "importe_asignado": 10}])
    with pytest.raises(ReconciliationNotFound):
        svc.reconcile_manual(movimiento_id=mov, asignaciones=[{"factura_id": 99999, "importe_asignado": 10}])
    with pytest.raises(ReconciliationError) as exc:
        svc.reconcile_manual(movimiento_id=mov, asignaciones=[{"factura_id": inv, "importe_asignado": 150}])
    assert "mayor que lo que le queda por pagar" in str(exc.value)
    # Nada se ha guardado a medias
    assert db.get_invoice_by_id(inv)["estado_pago"] == "pendiente"
    svc.reconcile_manual(movimiento_id=mov, asignaciones=[{"factura_id": inv, "importe_asignado": 100}])
    assert db.get_invoice_by_id(inv)["estado_pago"] == "pagada"
    # Ya pagada: un segundo pago se rechaza
    mov2 = _mov(db, -100.0, "b")
    with pytest.raises(ReconciliationError):
        svc.reconcile_manual(movimiento_id=mov2, asignaciones=[{"factura_id": inv, "importe_asignado": 100}])


def test_confirm_proposal_drops_conflicts_and_api_returns_404(tmp_path: Path):
    app = _app(tmp_path)
    db: Database = app.state.db
    inv = _invoice(db, "F-2", 80.0)
    mov_a = _mov(db, -80.0, "pa")
    mov_b = _mov(db, -80.0, "pb")
    now = "2026-11-10T00:00:00+00:00"
    with db.connect() as conn:
        for mov in (mov_a, mov_b):
            conn.execute(
                "INSERT INTO vinculos_conciliacion (movimiento_id, factura_id, importe_asignado, tipo_vinculo, confianza, confirmado, created_at)"
                " VALUES (?, ?, 80, 'automatico', 0.8, 0, ?)",
                (mov, inv, now),
            )
            conn.execute("UPDATE movimientos_bancarios SET estado_conciliacion = 'propuesto' WHERE id = ?", (mov,))
    client = _client(app)
    assert client.post("/api/bank/transactions/99999/confirm-proposal").status_code == 404
    r = client.post(f"/api/bank/transactions/{mov_a}/confirm-proposal")
    assert r.status_code == 200, r.text
    with db.connect() as conn:
        left = conn.execute("SELECT COUNT(*) FROM vinculos_conciliacion WHERE movimiento_id = ?", (mov_b,)).fetchone()[0]
        estado_b = conn.execute("SELECT estado_conciliacion FROM movimientos_bancarios WHERE id = ?", (mov_b,)).fetchone()[0]
    assert left == 0 and estado_b == "pendiente"
    r_manual = client.post(
        "/api/bank/reconcile-manual",
        json={"movimiento_id": mov_b, "asignaciones": [{"factura_id": inv, "importe_asignado": 80}]},
    )
    assert r_manual.status_code == 400


# ---------------------------------------------------------------------
# 3. Configuración con lista cerrada
# ---------------------------------------------------------------------


def test_settings_whitelist_ranges_and_repo(tmp_path: Path):
    client = _client(_app(tmp_path))
    assert client.put("/api/settings", json={"umbral_confianza": 0.3}).status_code == 400
    assert client.put("/api/settings", json={"intervalo_correo_minutos": 1}).status_code == 400
    r_unknown = client.put("/api/settings", json={"clave_rara": 1})
    assert r_unknown.status_code == 400 and "desconocida" in r_unknown.json()["detail"]
    assert client.put("/api/settings", json={"github_repo": "malo/repo"}).status_code == 400
    assert client.put("/api/settings", json={"github_repo": OFFICIAL_GITHUB_REPO}).status_code == 200
    ok = client.put("/api/settings", json={"umbral_confianza": 0.9, "intervalo_correo_minutos": 30, "estado_secretos": {}})
    assert ok.status_code == 200 and ok.json()["umbral_confianza"] == 0.9


def test_poller_loop_survives_bad_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _db(tmp_path)
    db.set_settings({"poller_activo": True})
    poller = MailPollerService(db=db, credentials=CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True))

    def boom(**_):
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr(poller, "sync_all_active_accounts", boom)
    calls = {"n": 0}

    def fake_wait(timeout=None):
        calls["n"] += 1
        return calls["n"] > 1  # una vuelta y luego parar

    monkeypatch.setattr(poller._stop_event, "wait", fake_wait)
    poller._worker_loop()  # no lanza
    assert any(e["nivel"] == "error" for e in db.list_events(limit=20)["items"])


# ---------------------------------------------------------------------
# 4. Validación de la edición de facturas
# ---------------------------------------------------------------------


def test_invoice_update_payload_validation():
    assert InvoiceUpdatePayload(fecha_emision="5/3/2026").fecha_emision == "2026-03-05"
    assert InvoiceUpdatePayload(fecha_emision="2026-03-05").fecha_emision == "2026-03-05"
    with pytest.raises(ValueError):
        InvoiceUpdatePayload(fecha_emision="31/02/2026")
    with pytest.raises(ValueError):
        InvoiceUpdatePayload(estado_pago="regalada")
    with pytest.raises(ValueError):
        InvoiceUpdatePayload(total=float("nan"))
    with pytest.raises(ValueError):
        InvoiceUpdatePayload(tipo_documento="inventado")


# ---------------------------------------------------------------------
# 5. Correos con error: reintentos y endpoint
# ---------------------------------------------------------------------


class _OneMail(MailConnector):
    def __init__(self) -> None:
        self.envelopes = [
            EmailEnvelope(
                message_id="<m1@x>",
                uid="1",
                sender="prov@x.es",
                subject="Factura",
                date=None,
                attachments=[PDFAttachment(filename="factura.pdf", content=b"%PDF-roto")],
            )
        ]

    def fetch_recent_envelopes(self, limit: int = 50, is_processed=None):
        return [e for e in self.envelopes if not (is_processed and is_processed(e.message_id))]


def test_retry_errors_endpoint_resets_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _app(tmp_path)
    db: Database = app.state.db
    acc_id = db.upsert_mail_account({"nombre": "Buzón", "email": "f@x.es", "tipo_conexion": "imap"})
    for _ in range(3):
        db.mark_message_processed(acc_id, "<m1@x>", estado="error", detalle="roto")
    assert db.is_message_processed(acc_id, "<m1@x>") is True
    poller: MailPollerService = app.state.mail_poller
    monkeypatch.setattr(poller, "sync_account", lambda account: {"facturas_insertadas": 0})
    r = _client(app).post(f"/api/mail/accounts/{acc_id}/retry-errors")
    assert r.status_code == 200 and r.json()["reintentados"] == 1
    assert db.is_message_processed(acc_id, "<m1@x>") is False
    accounts = _client(app).get("/api/mail/accounts").json()["items"]
    assert accounts[0]["mensajes_con_error"] == 1


def test_delete_mail_account_removes_credentials(tmp_path: Path):
    app = _app(tmp_path)
    creds: CredentialManager = app.state.credentials
    acc_id = app.state.db.upsert_mail_account({"nombre": "B", "email": "borrar@x.es", "tipo_conexion": "imap"})
    creds.set_mail_password("borrar@x.es", "secreto")
    assert _client(app).delete(f"/api/mail/accounts/{acc_id}").status_code == 200
    assert creds.get_mail_password("borrar@x.es") is None


# ---------------------------------------------------------------------
# 6. Subidas: validación por archivo
# ---------------------------------------------------------------------


def test_upload_reports_errors_per_file(tmp_path: Path):
    client = _client(_app(tmp_path))
    files = [
        ("files", ("buena.pdf", _pdf(0), "application/pdf")),
        ("files", ("falsa.pdf", b"esto no es un pdf", "application/pdf")),
        ("files", ("vacia.pdf", b"", "application/pdf")),
    ]
    r = client.post("/api/invoices/upload", files=files)
    assert r.status_code == 200
    body = r.json()
    estados = {x["filename"]: x["status"] for x in body["resultados"]}
    assert estados["buena.pdf"] == "insertada"
    assert estados["falsa.pdf"] == "error" and estados["vacia.pdf"] == "error"
    assert body["errores"] == 2


def test_upload_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.api.routes as routes

    monkeypatch.setattr(routes, "MAX_PDF_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(routes, "MAX_BANK_UPLOAD_BYTES", 10)
    client = _client(_app(tmp_path))
    r = client.post("/api/invoices/upload", files=[("files", ("g.pdf", b"%PDF" + b"0" * 2000, "application/pdf"))])
    assert r.json()["resultados"][0]["status"] == "error" and "MB" in r.json()["resultados"][0]["detail"]
    r_bank = client.post("/api/bank/import", files={"file": ("x.csv", b"a;b;c\n" * 10, "text/csv")})
    assert r_bank.status_code == 413


# ---------------------------------------------------------------------
# 7. Nombres válidos en Windows
# ---------------------------------------------------------------------


def test_windows_reserved_names_and_long_paths():
    assert sanitize_path_segment("CON") == "CON_"
    assert sanitize_path_segment("nul.txt").lower().startswith("nul_")
    assert not sanitize_path_segment("Proveedor S.L. . ").endswith((".", " "))
    folders, name = build_archive_relative_parts("2026-01-01", "otros", "P" * 300, "N" * 300, hash_pdf="ab" * 32)
    rel = "/".join(folders) + "/" + name
    assert len(rel) <= MAX_RELATIVE_PATH_LEN
    again = build_archive_relative_parts("2026-01-01", "otros", "P" * 300, "N" * 300, hash_pdf="ab" * 32)
    assert again == (folders, name)


# ---------------------------------------------------------------------
# 8. Mover el PDF al editar
# ---------------------------------------------------------------------


def test_edit_invoice_relocates_pdf(tmp_path: Path):
    app = _app(tmp_path)
    client = _client(app)
    up = client.post("/api/invoices/upload", files=[("files", ("a.pdf", _pdf(0), "application/pdf"))]).json()
    inv_id = up["resultados"][0]["invoice_id"]
    old_path = Path(app.state.db.get_invoice_by_id(inv_id)["ruta_archivo"])
    assert old_path.is_file()
    r = client.put(f"/api/invoices/{inv_id}", json={"proveedor_nombre": "Proveedor Nuevo", "fecha_emision": "02/01/2025"})
    assert r.status_code == 200, r.text
    new_path = Path(app.state.db.get_invoice_by_id(inv_id)["ruta_archivo"])
    assert new_path != old_path and new_path.is_file() and not old_path.exists()
    assert "2025" in new_path.parts and "Proveedor_Nuevo" in str(new_path)


def test_relocate_missing_file_keeps_record(tmp_path: Path):
    res = relocate_archived_pdf(str(tmp_path / "no.pdf"), tmp_path, "2026-01-01", "otros", "P", "N")
    assert res.error and not res.moved


# ---------------------------------------------------------------------
# 9. Excel
# ---------------------------------------------------------------------


def test_excel_filters_formats_vat_book_and_abonos(tmp_path: Path):
    db = _db(tmp_path)
    _invoice(db, "F-21", 121.0, base_imponible=100.0, iva=21.0, fecha_emision="2026-03-10")
    _invoice(
        db,
        "F-MIX",
        114.0,
        base_imponible=100.0,
        iva=14.0,
        fecha_emision="2026-03-11",
        datos_extra={"desglose_iva": [{"tipo": 21, "base": 50, "cuota": 10.5}, {"tipo": 4, "base": 50, "cuota": 2.0}]},
    )
    _invoice(db, "AB-1", 12.1, base_imponible=10.0, iva=2.1, tipo_documento="abono", fecha_emision="2026-03-12")
    _invoice(db, "ALB-1", 50.0, tipo_documento="albaran", fecha_emision="2026-03-12")
    _invoice(db, "F-ABR", 10.0, fecha_emision="2026-04-01")

    wb = openpyxl.load_workbook(io.BytesIO(AccountingExcelExporter(db=db).generate_workbook_bytes(mes="2026-03")))
    ws = wb["Facturas Recibidas"]
    headers = [c.value for c in ws[1]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    numeros = [r[3] for r in rows if r[0]]
    assert sorted(numeros) == ["AB-1", "F-21", "F-MIX"]  # filtro de mes y sin albaranes
    assert isinstance(ws.cell(row=2, column=2).value, dt.datetime)
    assert ws.cell(row=2, column=2).number_format == "dd/mm/yyyy"
    assert ws.cell(row=2, column=13).number_format == "#,##0.00 €"
    abono = next(r for r in rows if r[3] == "AB-1")
    assert abono[headers.index("Total Factura (€)")] == pytest.approx(-12.1)

    libro = wb["Libro registro recibidas"]
    lrows = [r for r in libro.iter_rows(min_row=2, values_only=True) if r[2]]
    mix_rows = [r for r in lrows if r[2] == "F-MIX"]
    assert len(mix_rows) == 2 and {r[6] for r in mix_rows} == {21.0, 4.0}
    assert sum(1 for r in mix_rows if r[10] is not None) == 1  # total solo en la primera fila
    assert [r[2] for r in wb["Albaranes"].iter_rows(min_row=2, values_only=True)] == ["ALB-1"]
    resumen = [r for r in wb["Resumen Mensual y Categorías"].iter_rows(values_only=True) if r and r[0] == "2026-03"]
    assert resumen and resumen[0][1] == 3


def test_tax_breakdown_inference():
    assert tax_breakdown({"base_imponible": 100, "iva": 10, "datos_extra": {}})["lineas"][0]["tipo_iva"] == 10.0
    odd = tax_breakdown({"base_imponible": 100, "iva": 13, "datos_extra": {}})
    assert odd["lineas"][0]["tipo_iva"] is None and odd["origen"] == "mixto/sin desglose"
    re_line = tax_breakdown({"base_imponible": 100, "iva": 21, "datos_extra": {"recargo_equivalencia": 5.2}})["lineas"][0]
    assert re_line["tipo_re"] == 5.2


def test_excel_endpoint_validates_month(tmp_path: Path):
    client = _client(_app(tmp_path))
    assert client.get("/api/export/excel", params={"mes": 'x"; evil'}).status_code == 400
    ok = client.get("/api/export/excel", params={"mes": "2026-03"})
    assert ok.status_code == 200 and "2026-03" in ok.headers["content-disposition"]


# ---------------------------------------------------------------------
# 10. Paginación
# ---------------------------------------------------------------------


def test_invoice_and_bank_pagination_and_unpaid_without_cap(tmp_path: Path):
    app = _app(tmp_path)
    db: Database = app.state.db
    for i in range(7):
        _invoice(db, f"P-{i}", 10.0 + i)
        _mov(db, -(10.0 + i), f"m{i}")
    client = _client(app)
    page = client.get("/api/invoices", params={"limit": 3, "offset": 3}).json()
    assert page["total"] == 7 and len(page["items"]) == 3
    bank = client.get("/api/bank/transactions", params={"limit": 5, "offset": 5}).json()
    assert bank["total"] == 7 and len(bank["items"]) == 2
    assert len(db.list_unpaid_invoices()) == 7


# ---------------------------------------------------------------------
# 11. Cambio de carpeta de datos
# ---------------------------------------------------------------------


def test_data_dir_change_blocks_ingestion_until_restart(tmp_path: Path):
    app = _app(tmp_path)
    client = _client(app)
    r = client.post("/api/settings/data-dir", json={"ruta": str(tmp_path / "nueva")})
    assert r.status_code == 200, r.text
    assert r.json()["reinicio_necesario"] is True
    blocked = client.post("/api/invoices/upload", files=[("files", ("a.pdf", _pdf(0), "application/pdf"))])
    assert blocked.status_code == 409 and "Reinicia Archivero" in blocked.json()["detail"]
    assert client.post("/api/mail/sync").status_code == 409
    assert client.post("/api/bank/import", files={"file": ("x.csv", b"a;b\n", "text/csv")}).status_code == 409


# ---------------------------------------------------------------------
# 12. Google Drive desactivado
# ---------------------------------------------------------------------


def test_gdrive_setting_is_treated_as_local_with_single_notice(tmp_path: Path):
    db = _db(tmp_path)
    db.set_settings({"carpeta_salida": str(tmp_path / "archivo"), "capa_almacenamiento": "gdrive"})
    pipe = InvoiceIngestionPipeline(db=db, gdrive_access_token="tok")
    pipe.ingest_pdf(_pdf(0), filename="a.pdf")
    pipe.ingest_pdf(_pdf(1), filename="b.pdf")
    avisos = [e for e in db.list_events(limit=50)["items"] if "Google Drive" in e["mensaje"]]
    assert len(avisos) == 1
    assert all(Path(i["ruta_archivo"]).is_file() for i in db.list_invoices())


# ---------------------------------------------------------------------
# 13. Datos de ejemplo
# ---------------------------------------------------------------------


def test_demo_data_load_and_delete(tmp_path: Path):
    app = _app(tmp_path)
    db: Database = app.state.db
    real = _invoice(db, "REAL-1", 33.0)
    client = _client(app)
    loaded = client.post("/api/demo/load-sample-data").json()
    assert loaded["facturas_insertadas"] == 7
    demo = [i for i in db.list_invoices(limit=None) if (i.get("datos_extra") or {}).get("demo")]
    assert len(demo) == 7
    pdfs = [Path(i["ruta_archivo"]) for i in demo]
    r = client.delete("/api/demo/sample-data")
    assert r.status_code == 200
    body = r.json()
    assert body["facturas_borradas"] == 7 and body["movimientos_borrados"] >= 1
    assert all(not p.exists() for p in pdfs)
    restantes = db.list_invoices(limit=None, incluir_sustituidas=True)
    assert [i["id"] for i in restantes] == [real]
    assert db.count_bank_transactions() == 0


def test_dockerfile_copies_scripts_and_docs():
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "COPY scripts ./scripts" in text and "COPY docs ./docs" in text


# ---------------------------------------------------------------------
# 15. Copias diarias
# ---------------------------------------------------------------------


def test_daily_backup_rotation(tmp_path: Path):
    db = _db(tmp_path)
    _invoice(db, "B-1", 1.0)
    start = dt.date(2026, 1, 1)
    for i in range(16):
        db.create_daily_backup_if_needed(keep=14, today=start + dt.timedelta(days=i))
    assert db.create_daily_backup_if_needed(keep=14, today=start + dt.timedelta(days=15)) is None  # mismo día
    dailies = sorted((tmp_path / "backups").glob("facturas_backup_diaria_*.sqlite"))
    assert len(dailies) == 14
    assert any(e["tipo"] == "copia_seguridad" for e in db.list_events(limit=50)["items"])


# ---------------------------------------------------------------------
# 16. Detalles: detección de facturas y OAuth
# ---------------------------------------------------------------------


def _content(text: str) -> PDFContent:
    return PDFContent(
        pdf_bytes=b"%PDF", hash_sha256="x", text=text, page_count=1, has_text_layer=bool(text), extraction_method="texto_nativo"
    )


def test_looks_like_invoice_uses_word_boundaries_and_strong_signals():
    assert looks_like_invoice(_content(""), filename="Fra_2026_001.pdf") is True
    assert looks_like_invoice(_content("folleto informativo"), filename="tarifas_fraude.pdf") is False
    assert looks_like_invoice(_content("Boletín: el total de visitas y el IVA del mes")) is False
    assert looks_like_invoice(_content("FACTURA Nº 12\nCIF B12345678\nBase imponible 100\nIVA 21%\nTotal 121")) is True


def test_oauth_state_is_server_nonce(tmp_path: Path):
    app = _app(tmp_path)
    client = _client(app)
    assert client.get("/api/mail/oauth/authorize-url", params={"provider": "oauth_gmail", "client_id": "c"}).status_code == 405
    r = client.post("/api/mail/oauth/authorize-url", json={"provider": "oauth_gmail", "client_id": "c", "email": "A@x.es"})
    state = parse_qs(urlparse(r.json()["authorization_url"]).query)["state"][0]
    assert state in app.state.oauth_states and app.state.oauth_states[state]["email"] == "a@x.es"
    assert "@" not in state


# ---------------------------------------------------------------------
# A. Albaranes y B. Abonos
# ---------------------------------------------------------------------


def test_delivery_note_detection_and_exclusions(tmp_path: Path):
    assert is_delivery_note_text("ALBARÁN DE ENTREGA Nº 55\nCooperativa\n...") is True
    assert is_delivery_note_text("FACTURA\nAlbarán relacionado: 55") is False
    db = _db(tmp_path)
    alb = _invoice(db, "ALB-9", 500.0, tipo_documento="albaran")
    fac = _invoice(db, "F-9", 100.0)
    assert [i["id"] for i in db.list_unpaid_invoices()] == [fac]
    metrics = db.get_dashboard_metrics()
    resumen = metrics["resumen"]
    assert resumen["importe_pendiente_pago"] == pytest.approx(100.0)
    assert resumen["albaranes"] == 1
    ids = [i["id"] for i in db.list_invoices(tipo_documento="albaran")]
    assert ids == [alb]


def test_albaran_archived_in_own_folder(tmp_path: Path):
    db = _db(tmp_path)
    db.set_settings({"carpeta_salida": str(tmp_path / "archivo")})
    lines = ["ALBARÁN DE ENTREGA Nº AL-77", "Cooperativa Farmacéutica del Sur", "NIF: F12345678", "Fecha: 03/11/2026", "Total 50,00"]
    res = InvoiceIngestionPipeline(db=db).ingest_pdf(build_invoice_pdf_bytes(lines), filename="alb.pdf")
    inv = db.get_invoice_by_id(res.invoice_id)
    assert inv["tipo_documento"] == "albaran"
    assert "Albaranes" in Path(inv["ruta_archivo"]).parts


def test_abonos_net_pending_and_unpaid_count(tmp_path: Path):
    db = _db(tmp_path)
    _invoice(db, "F-A", 200.0)
    _invoice(db, "AB-A", 50.0, tipo_documento="abono")
    resumen = db.get_dashboard_metrics()["resumen"]
    assert resumen["importe_pendiente_pago"] == pytest.approx(150.0)
    assert resumen["facturas_impagadas"] == 1


def test_settings_payload_rejects_bad_endpoint(tmp_path: Path):
    client = _client(_app(tmp_path))
    r = client.put("/api/settings", json={"llm_endpoint_url": "ftp://x"})
    assert r.status_code == 400
    assert json.loads(r.text)["detail"].startswith("Configuración no válida")
