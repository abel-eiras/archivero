"""Tests del registro de actividad (avisos, estado del programa) y del panel de liquidez y plazos de pago."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import List

import pytest
from fastapi.testclient import TestClient

from app.db.database import MIGRATIONS, Database
from app.main import create_app
from app.services.activity import compute_health, generate_due_date_notice, record_heartbeat
from app.services.bank_parsers import parse_camt053_balances, parse_csv_balance, parse_norma43_balances
from app.services.credentials import CredentialManager
from app.services.liquidity import compute_liquidity, supplier_payment_terms
from app.services.mail_poller import EmailEnvelope, MailConnector, MailPollerService, PDFAttachment
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.reconciliation import BankReconciliationService
from app.services.storage import LocalFolderStorage
from scripts.generate_sample_data import build_invoice_pdf_bytes, get_sample_invoices_specs

TODAY = dt.date.today()


def _d(days: int) -> str:
    return (TODAY + dt.timedelta(days=days)).isoformat()


def _db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "db.sqlite", backup_dir=tmp_path / "backups")


def _invoice(db: Database, numero: str, total: float, proveedor: str = "Distribuidora Norte", **extra) -> int:
    data = {
        "proveedor_nombre": proveedor,
        "nif": extra.pop("nif", ""),
        "numero_factura": numero,
        "fecha_emision": extra.pop("fecha_emision", _d(-5)),
        "fecha_vencimiento": extra.pop("fecha_vencimiento", None),
        "base_imponible": total,
        "iva": 0.0,
        "total": total,
        "hash_pdf": f"hash-{numero}-{proveedor}",
        "estado_revision": "confirmada",
    }
    data.update(extra)
    return db.insert_invoice(data)


def _pay(db: Database, invoice_id: int, amount: float, fecha: str | None = None) -> None:
    """Simula un pago bancario confirmado unido a la factura."""
    mov_id, _ = db.insert_bank_transaction(
        {"fecha": fecha or _d(-1), "importe": -amount, "concepto": "pago", "hash_movimiento": f"mov-{invoice_id}-{amount}"}
    )
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO vinculos_conciliacion (movimiento_id, factura_id, importe_asignado, tipo_vinculo, confianza, confirmado, created_at)"
            " VALUES (?, ?, ?, 'manual', 1.0, 1, ?)",
            (mov_id, invoice_id, amount, "2026-01-01T00:00:00+00:00"),
        )
    db.recalculate_invoice_payment_status(invoice_id)


def _client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "web.sqlite", backup_dir=tmp_path / "backups", start_background_poller=False)
    return TestClient(app, base_url="http://127.0.0.1")


# ---------------------------------------------------------------------
# Migración
# ---------------------------------------------------------------------


def test_migration_v3_on_existing_v2_database(tmp_path: Path):
    db_file = tmp_path / "old.sqlite"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(MIGRATIONS[0][2])
        conn.executescript(MIGRATIONS[1][2])
        conn.execute("INSERT INTO schema_versions VALUES (1, 'v1', '2026-01-01')")
        conn.execute("INSERT INTO schema_versions VALUES (2, 'v2', '2026-01-01')")
    db = Database(db_path=db_file, backup_dir=tmp_path / "backups")
    assert db.get_schema_version() >= 3
    assert list((tmp_path / "backups").glob("*pre_migration_v2*"))
    with db.connect() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        prov_cols = {r["name"] for r in conn.execute("PRAGMA table_info(proveedores)")}
        mail_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cuentas_correo)")}
    assert {"eventos", "saldos_bancarios"} <= tables
    assert "plazo_pago_dias" in prov_cols
    assert "ultima_sincronizacion_ok" in mail_cols
    assert db.get_setting("plazo_pago_defecto_dias") == 30


# ---------------------------------------------------------------------
# Registro de actividad
# ---------------------------------------------------------------------


def test_event_log_filters_unread_and_purge(tmp_path: Path):
    db = _db(tmp_path)
    e1 = db.log_event("factura_nueva", "Factura nueva", entidad="factura", entidad_id=5)
    e2 = db.log_event("error_correo", "Fallo de correo", nivel="error")
    db.log_event("inicio_programa", "Arranque", leido=True)
    db.log_event("raro", "Nivel desconocido", nivel="tremendo")

    assert db.count_unread() == {"no_leidos": 3, "importantes": 1}
    assert db.list_events(nivel="error")["total"] == 1
    assert db.list_events(tipo="factura_nueva")["items"][0]["entidad_id"] == 5
    assert db.list_events(nivel="info", solo_no_leidos=True)["total"] == 2
    page = db.list_events(limit=2, offset=0)
    assert page["total"] == 4 and len(page["items"]) == 2

    assert db.mark_read(ids=[e1, e2]) == 2
    assert db.count_unread()["importantes"] == 0
    assert db.mark_read(all_events=True) == 1
    assert db.count_unread()["no_leidos"] == 0

    with db.connect() as conn:
        conn.execute("UPDATE eventos SET fecha = '2020-01-01T00:00:00+00:00' WHERE id = ?", (e1,))
    assert db.purge_old_events(days=180) == 1
    assert db.list_events()["total"] == 3


def test_activity_endpoints_and_startup_event(tmp_path: Path):
    app = create_app(db_path=tmp_path / "web.sqlite", backup_dir=tmp_path / "backups", start_background_poller=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        db: Database = app.state.db
        items = client.get("/api/activity").json()["items"]
        assert any(ev["tipo"] == "inicio_programa" and ev["leido"] for ev in items)

        ev_id = db.log_event("error_ia", "La IA falló", nivel="aviso")
        assert client.get("/api/activity/unread-count").json() == {"no_leidos": 1, "importantes": 1}
        assert client.get("/api/activity", params={"nivel": "aviso"}).json()["total"] == 1
        assert client.get("/api/activity", params={"solo_no_leidos": "true"}).json()["items"][0]["id"] == ev_id

        r = client.post("/api/activity/mark-read", json={"ids": [ev_id]})
        assert r.status_code == 200 and r.json()["no_leidos"] == 0
        db.log_event("factura_nueva", "otra")
        assert client.post("/api/activity/mark-read", json={"all": True}).json()["marcados"] == 1
        assert client.post("/api/activity/mark-read", json={"ids": "x"}).status_code == 400


def test_manual_backup_and_settings_change_are_logged(tmp_path: Path):
    client = _client(tmp_path)
    db: Database = client.app.state.db
    db.insert_invoice({"proveedor_nombre": "X", "numero_factura": "1", "total": 1, "hash_pdf": "h"})
    assert client.post("/api/database/backup").json()["created"] is True
    r = client.put("/api/settings", json={"proveedor_modelo": "heuristico_local"})
    assert r.status_code == 200
    tipos = [ev["tipo"] for ev in db.list_events()["items"]]
    assert "copia_seguridad" in tipos and "config_cambiada" in tipos
    assert client.put("/api/settings", json={"plazo_pago_defecto_dias": 999}).status_code == 400


# ---------------------------------------------------------------------
# Eventos de ingesta de facturas y de correo
# ---------------------------------------------------------------------


def test_pipeline_logs_new_duplicate_and_llm_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _db(tmp_path)
    storage = LocalFolderStorage(base_output_dir=tmp_path / "archivo", staging_dir=tmp_path / "staging")
    spec = get_sample_invoices_specs()[0]
    pdf_bytes = build_invoice_pdf_bytes(spec["lines"])

    pipeline = InvoiceIngestionPipeline(db=db, storage_override=storage)
    res = pipeline.ingest_pdf(pdf_bytes, filename="f.pdf", sender_email=spec["sender"])
    assert res.status == "insertada"
    pipeline.ingest_pdf(pdf_bytes, filename="f.pdf")

    events = db.list_events()["items"]
    tipos = {ev["tipo"] for ev in events}
    assert "factura_duplicada" in tipos
    assert tipos & {"factura_nueva", "factura_revision"}
    inv_event = next(ev for ev in events if ev["tipo"] in ("factura_nueva", "factura_revision"))
    assert inv_event["entidad"] == "factura" and inv_event["entidad_id"] == res.invoice_id

    # Fallo de la IA externa -> aviso error_ia con el motivo
    from app.services import llm_processor

    def boom(self, *a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(llm_processor.GoogleAIStudioProvider, "extract_invoice", boom)
    db.set_settings({"proveedor_modelo": "google_ai_studio"})
    pipeline_ia = InvoiceIngestionPipeline(db=db, llm_api_key="clave-falsa", storage_override=storage)
    pipeline_ia.ingest_pdf(build_invoice_pdf_bytes(get_sample_invoices_specs()[1]["lines"]), filename="g.pdf")
    ia_events = db.list_events(tipo="error_ia")["items"]
    assert len(ia_events) == 1 and ia_events[0]["nivel"] == "aviso"
    assert "g.pdf" in ia_events[0]["mensaje"]


class _FakeConnector(MailConnector):
    def __init__(self, envelopes: List[EmailEnvelope], fail: bool = False) -> None:
        self.envelopes = envelopes
        self.fail = fail

    def fetch_recent_envelopes(self, limit: int = 50) -> List[EmailEnvelope]:
        if self.fail:
            raise ConnectionError("contraseña incorrecta")
        return self.envelopes


def test_mail_sync_events_and_health(tmp_path: Path):
    db = _db(tmp_path)
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    acc_id = db.upsert_mail_account({"nombre": "General", "email": "farmacia@example.com"})
    state = {"fail": True, "envelopes": []}
    poller = MailPollerService(
        db=db, credentials=creds, connector_factory=lambda acc: _FakeConnector(state["envelopes"], state["fail"])
    )

    # 1. Error de conexión: un solo aviso aunque se repita
    poller.sync_all_active_accounts()
    poller.sync_all_active_accounts()
    assert db.list_events(tipo="error_correo")["total"] == 1
    health = compute_health(db, poller_running=True)
    assert health["estado"] == "error"
    assert health["buzones"][0]["estado"] == "error"

    # 2. Recuperada y sin novedades: solo un aviso de recuperación, ninguna línea de resumen
    state["fail"] = False
    poller.sync_all_active_accounts()
    assert db.list_events(tipo="correo_revisado")["total"] == 1
    poller.sync_all_active_accounts()
    assert db.list_events(tipo="correo_revisado")["total"] == 1
    record_heartbeat(db)
    health = compute_health(db, poller_running=True)
    assert health["estado"] == "ok" and health["mensaje"] == "El programa está funcionando"
    assert health["buzones"][0]["ultima_revision_ok"]

    # 3. Correo con factura -> resumen informativo
    pdf = build_invoice_pdf_bytes(get_sample_invoices_specs()[0]["lines"])
    state["envelopes"] = [
        EmailEnvelope(
            uid="1", message_id="<m1@x>", sender_name="Norte", sender_email="facturas@norte.es",
            subject="Factura", date_str="", pdf_attachments=[PDFAttachment(filename="f.pdf", content=pdf)],
        )
    ]
    db.set_settings({"remitentes_globales": ["norte.es"], "carpeta_salida": str(tmp_path / "archivo")})
    poller.sync_all_active_accounts()
    resumen = db.list_events(tipo="correo_revisado")["items"][0]
    assert "1 mensaje(s) nuevo(s)" in resumen["mensaje"] and resumen["detalle"]["facturas_insertadas"] == 1

    # 4. Buzón parado: última revisión correcta hace demasiado -> aviso
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE cuentas_correo SET ultima_sincronizacion_ok = ? WHERE id = ?", (old, acc_id))
    health = compute_health(db, poller_running=True)
    assert health["estado"] == "aviso" and health["buzones"][0]["estado"] == "aviso"

    # 5. Vigilante parado y fallos de IA repetidos
    for _ in range(3):
        db.log_event("error_ia", "fallo", nivel="aviso")
    health = compute_health(db, poller_running=False)
    textos = " ".join(p["mensaje"] for p in health["problemas"])
    assert "no está en marcha" in textos and "La IA ha fallado 3 veces" in textos


def test_health_endpoint(tmp_path: Path):
    client = _client(tmp_path)
    data = client.get("/api/activity/health").json()
    assert data["estado"] == "ok" and data["buzones"] == []
    assert data["umbral_minutos"] == 60


def test_due_date_notice_once_per_day(tmp_path: Path):
    db = _db(tmp_path)
    _invoice(db, "V1", 100.0, fecha_vencimiento=_d(-2))
    _invoice(db, "V2", 50.0, fecha_vencimiento=_d(2))
    _invoice(db, "V3", 70.0, fecha_vencimiento=_d(20))
    paid = _invoice(db, "V4", 30.0, fecha_vencimiento=_d(-1))
    _pay(db, paid, 30.0)

    ev_id = generate_due_date_notice(db)
    assert ev_id is not None
    ev = db.list_events(tipo="vencimiento")["items"][0]
    assert ev["nivel"] == "aviso"
    assert "1 factura(s) vencida(s)" in ev["mensaje"] and "100,00 €" in ev["mensaje"]
    assert "50,00 €" in ev["mensaje"]
    assert generate_due_date_notice(db) is None  # mismo día: no se repite
    assert generate_due_date_notice(db, today=TODAY + dt.timedelta(days=1)) is not None


# ---------------------------------------------------------------------
# Saldos de los extractos bancarios
# ---------------------------------------------------------------------


def _n43(saldo_inicial: str, sign_ini: str, movs: List[tuple], final: str | None, sign_final: str) -> str:
    l11 = ("11" + "2100" + "0418" + "1234567890" + "261101" + "261130" + sign_ini + saldo_inicial + "978" + "3" + "FARMACIA").ljust(80)
    lines = [l11]
    for fecha, signo, importe in movs:
        lines.append(("22" + "    " + "0418" + fecha + fecha + "03" + "001" + signo + importe + "0" * 10).ljust(80))
    final_part = (sign_final + final) if final is not None else "XXXXXXXXXXXXXXX"
    l33 = ("33" + "2100" + "0418" + "1234567890" + "00001" + "0" * 14 + "00000" + "0" * 14 + final_part + "978").ljust(80)
    lines += [l33, "88".ljust(80, "9")]
    return "\n".join(lines) + "\n"


def test_norma43_balance_sign_and_fallback():
    # Saldo final deudor (código 1) -> negativo
    content = _n43("00000000010000", "2", [("261115", "1", "00000000035050")], "00000000025050", "1")
    saldos = parse_norma43_balances(content, banco_default="Mi Banco")
    assert saldos == [
        {"banco": "Mi Banco", "cuenta": "2100-0418-1234567890", "fecha_saldo": "2026-11-30", "saldo": -250.50, "moneda": "EUR"}
    ]
    # Saldo final acreedor (código 2) -> positivo
    content = _n43("00000001500000", "2", [("261115", "1", "00000000229185")], "00000001270815", "2")
    assert parse_norma43_balances(content)[0]["saldo"] == 12708.15
    # Registro 33 ilegible -> saldo inicial + movimientos
    content = _n43("00000001500000", "2", [("261115", "1", "00000000229185")], None, "2")
    assert parse_norma43_balances(content)[0]["saldo"] == 12708.15


def test_camt053_closing_balance_and_csv_balance():
    xml = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
  <Acct><Id><IBAN>ES9121000418450200051332</IBAN></Id></Acct>
  <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">900.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-11-01</Dt></Dt></Bal>
  <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">320.40</Amt><CdtDbtInd>DBIT</CdtDbtInd><Dt><Dt>2026-11-30</Dt></Dt></Bal>
</Stmt></BkToCstmrStmt></Document>"""
    saldos = parse_camt053_balances(xml.encode("utf-8"))
    assert saldos[0]["cuenta"] == "ES9121000418450200051332"
    assert saldos[0]["saldo"] == -320.40 and saldos[0]["fecha_saldo"] == "2026-11-30"

    csv_desc = "Fecha;Concepto;Importe;Saldo\n28/11/2026;B;-10,00;1.490,00\n28/11/2026;A;-5,00;1.500,00\n20/11/2026;C;100,00;1.505,00\n"
    assert parse_csv_balance(csv_desc, banco_default="Caja")[0]["saldo"] == 1490.00
    csv_asc = "Fecha;Concepto;Importe;Saldo\n20/11/2026;C;100,00;1.505,00\n28/11/2026;A;-5,00;1.500,00\n28/11/2026;B;-10,00;1.490,00\n"
    assert parse_csv_balance(csv_asc)[0]["fecha_saldo"] == "2026-11-28"
    assert parse_csv_balance(csv_asc)[0]["saldo"] == 1490.00
    assert parse_csv_balance("Fecha;Concepto;Importe\n20/11/2026;C;100,00\n") == []


def test_import_statement_stores_balance_upsert_and_logs(tmp_path: Path):
    db = _db(tmp_path)
    service = BankReconciliationService(db=db)
    content = _n43("00000001500000", "2", [("261115", "1", "00000000229185")], "00000001270815", "2")
    res = service.import_statement(content, filename="extracto.n43", formato="norma43", banco="Banco Farmacia")
    assert res["saldos_importados"][0]["saldo"] == 12708.15
    service.import_statement(content, filename="extracto.n43", formato="norma43", banco="Banco Farmacia")
    balances = db.latest_bank_balances()
    assert len(balances) == 1 and balances[0]["saldo"] == 12708.15
    ev = db.list_events(tipo="extracto_importado")["items"]
    assert len(ev) == 2 and "12.708,15 €" in ev[0]["mensaje"]


# ---------------------------------------------------------------------
# Plazos de pago y previsión de liquidez
# ---------------------------------------------------------------------


def test_supplier_payment_terms_learned_explicit_and_default(tmp_path: Path):
    db = _db(tmp_path)
    db.set_settings({"plazo_pago_defecto_dias": 45})
    for i, days in enumerate((30, 60, 60)):
        _invoice(db, f"L{i}", 10.0, proveedor="Cofares", fecha_emision="2026-01-01",
                 fecha_vencimiento=(dt.date(2026, 1, 1) + dt.timedelta(days=days)).isoformat())
    _invoice(db, "N1", 10.0, proveedor="Sin Historial")

    terms = {t["nombre"]: t for t in supplier_payment_terms(db)["items"]}
    assert terms["Cofares"]["plazo_efectivo"] == 60 and terms["Cofares"]["origen"] == "aprendido"
    assert terms["Sin Historial"]["plazo_efectivo"] == 45 and terms["Sin Historial"]["origen"] == "general"

    db.set_supplier_payment_term(terms["Cofares"]["proveedor_id"], 15)
    terms = {t["nombre"]: t for t in supplier_payment_terms(db)["items"]}
    assert terms["Cofares"]["plazo_efectivo"] == 15 and terms["Cofares"]["origen"] == "fijado"


def test_liquidity_buckets_pending_amounts_and_alerts(tmp_path: Path):
    db = _db(tmp_path)

    # Sin saldo importado -> ámbar con aviso
    _invoice(db, "A", 100.0, fecha_vencimiento=_d(-3))
    liq = compute_liquidity(db)
    assert liq["saldo_disponible"] is False and liq["alerta"] == "ambar"

    partial = _invoice(db, "B", 500.0, fecha_vencimiento=_d(5))
    _pay(db, partial, 200.0)  # quedan 300
    _invoice(db, "C", 1000.0, fecha_vencimiento=_d(20))
    _invoice(db, "AB", -50.0, tipo_documento="abono", fecha_vencimiento=_d(20))
    _invoice(db, "D", 400.0, fecha_vencimiento=_d(45))
    _invoice(db, "E", 80.0, proveedor="Proveedor Nuevo", fecha_emision=_d(-10))  # sin vencimiento: emisión + 30 días = día 20 (estimado)
    _invoice(db, "F", 999.0, fecha_vencimiento=_d(200))  # fuera del horizonte
    paid = _invoice(db, "G", 70.0, fecha_vencimiento=_d(3))
    _pay(db, paid, 70.0)
    old = _invoice(db, "OLD", 333.0, fecha_vencimiento=_d(2))
    new = _invoice(db, "NEW", 333.0, fecha_vencimiento=_d(2), proveedor="Otro")
    db.mark_invoice_replaced(old_id=old, new_id=new)

    db.upsert_bank_balance(cuenta="ES00", fecha_saldo=_d(-1), saldo=2500.0, banco="Banco")
    liq = compute_liquidity(db, horizonte=90)
    assert liq["saldo_actual"] == 2500.0 and liq["saldo_antiguo"] is False
    res = liq["resumen"]
    assert res["vencido"] == 100.0
    assert res["proximos_7"] == 300.0 + 333.0
    assert res["proximos_30"] == 300.0 + 333.0 + 1000.0 - 50.0 + 80.0
    assert res["saldo_previsto_30"] == round(2500.0 - 100.0 - 300.0 - 333.0 - 1000.0 + 50.0 - 80.0, 2)
    assert liq["alerta"] == "verde"

    tramos = {t["clave"]: t for t in liq["tramos"]}
    assert tramos["vencido"]["facturas"] == 1
    assert tramos["dias_8_30"]["importe"] == 1030.0 and tramos["dias_8_30"]["facturas"] == 3
    assert tramos["dias_31_60"]["saldo_proyectado"] == round(2500.0 - 100 - 633 - 1030 - 400, 2)

    estimated = [p for p in liq["pagos"] if p["estimado"]]
    assert len(estimated) == 1 and estimated[0]["fecha"] == _d(20)
    assert [p["fecha"] for p in liq["pagos"]] == sorted(p["fecha"] for p in liq["pagos"])
    assert all(p["numero_factura"] not in ("F", "G", "OLD") for p in liq["pagos"])
    assert len(liq["serie_semanal"]) == 13
    assert liq["serie_semanal"][0]["pagos"] == 100.0 + 633.0  # lo vencido cuenta en la primera semana
    assert liq["serie_semanal"][-1]["saldo_proyectado"] == round(2500.0 - 100 - 633 - 1030 - 400, 2)

    # Saldo insuficiente a 30 días -> rojo; a 90 días -> ámbar
    db.upsert_bank_balance(cuenta="ES00", fecha_saldo=_d(0), saldo=1000.0, banco="Banco")
    assert compute_liquidity(db)["alerta"] == "rojo"
    db.upsert_bank_balance(cuenta="ES00", fecha_saldo=_d(0), saldo=1800.0, banco="Banco")
    assert compute_liquidity(db)["alerta"] == "ambar"

    # Saldo antiguo -> ámbar aunque alcance
    db2 = Database(db_path=tmp_path / "db2.sqlite", backup_dir=tmp_path / "b2")
    db2.upsert_bank_balance(cuenta="X", fecha_saldo=_d(-10), saldo=50.0)
    liq2 = compute_liquidity(db2)
    assert liq2["alerta"] == "ambar" and liq2["cuentas"][0]["antiguo"] is True


def test_dashboard_uses_pending_amount(tmp_path: Path):
    db = _db(tmp_path)
    inv = _invoice(db, "P1", 500.0, fecha_vencimiento=_d(3))
    _pay(db, inv, 200.0)
    metrics = db.get_dashboard_metrics()
    assert metrics["resumen"]["importe_pendiente_pago"] == 300.0
    assert metrics["vencimientos_proximos"][0]["importe_pendiente"] == 300.0
    assert metrics["pendientes_pago"][0]["importe_pendiente"] == 300.0


def test_liquidity_and_payment_term_endpoints(tmp_path: Path):
    client = _client(tmp_path)
    db: Database = client.app.state.db
    _invoice(db, "E1", 80.0, proveedor="Hefame", fecha_emision=_d(-5))

    data = client.get("/api/liquidity", params={"horizonte": 90}).json()
    assert data["saldo_disponible"] is False and data["pagos"][0]["estimado"] is True
    assert client.get("/api/liquidity", params={"horizonte": 2}).status_code == 422

    terms = client.get("/api/suppliers/payment-terms").json()
    sid = terms["items"][0]["proveedor_id"]
    r = client.put(f"/api/suppliers/{sid}/payment-term", json={"plazo_pago_dias": 10})
    assert r.status_code == 200 and r.json()["plazo_efectivo"] == 10
    assert client.get("/api/liquidity").json()["pagos"][0]["fecha"] == _d(5)
    r = client.put(f"/api/suppliers/{sid}/payment-term", json={"plazo_pago_dias": None})
    assert r.json()["origen"] == "general"
    assert client.put(f"/api/suppliers/{sid}/payment-term", json={"plazo_pago_dias": 500}).status_code == 400
    assert client.put(f"/api/suppliers/{sid}/payment-term", json={"plazo_pago_dias": "abc"}).status_code == 400
    assert client.put("/api/suppliers/9999/payment-term", json={"plazo_pago_dias": 5}).status_code == 404
