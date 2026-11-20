"""Tests de regresión de los fallos corregidos tras la auditoría (conciliación, extractos, Excel, correo, actualizador...)."""

from __future__ import annotations

import hashlib
import imaplib
import io
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List

import openpyxl
import pytest
from fastapi.testclient import TestClient

import updater
from app.db.database import Database
from app.main import create_app
from app.services import credentials as credentials_module
from app.services import mail_poller as mail_poller_module
from app.services.bank_parsers import _parse_amount_cell, parse_camt053
from app.services.credentials import CredentialManager
from app.services.excel_exporter import AccountingExcelExporter
from app.services.mail_poller import (
    EmailEnvelope,
    IMAPMailConnector,
    MailConnector,
    MailPollerService,
    OAuth2MailConnector,
    PDFAttachment,
    parse_raw_email_message,
)
from app.services.reconciliation import BankReconciliationService
from app.services.updater_service import UpdaterService, parse_sha256sums


def _db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "audit.sqlite", backup_dir=tmp_path / "backups")


def _invoice(db: Database, n: int, total: float = 100.0, **extra: Any) -> int:
    data = {
        "proveedor_nombre": extra.pop("proveedor_nombre", f"Proveedor Genérico {n}"),
        "numero_factura": f"F-{n:04d}",
        "fecha_emision": "2026-01-10",
        "fecha_vencimiento": "2026-01-15",
        "base_imponible": round(total / 1.21, 2),
        "iva": round(total - total / 1.21, 2),
        "total": total,
        "hash_pdf": hashlib.sha256(f"pdf-{n}".encode()).hexdigest(),
    }
    data.update(extra)
    return db.insert_invoice(data)


def _tx(db: Database, n: int, importe: float = -100.0, fecha: str = "2026-01-15", concepto: str = "PAGO") -> int:
    tx_id, _ = db.insert_bank_transaction(
        {
            "banco": "Banco",
            "fecha": fecha,
            "importe": importe,
            "concepto": f"{concepto} {n}",
            "hash_movimiento": hashlib.sha256(f"tx-{n}".encode()).hexdigest(),
        }
    )
    return int(tx_id)


def _tx_state(db: Database, tx_id: int) -> str:
    with db.connect() as conn:
        return conn.execute(
            "SELECT estado_conciliacion FROM movimientos_bancarios WHERE id = ?", (tx_id,)
        ).fetchone()["estado_conciliacion"]


# ---------------------------------------------------------------------
# 1-3. Conciliación
# ---------------------------------------------------------------------


def test_proposed_invoice_is_not_reused_for_another_movement(tmp_path: Path):
    db = _db(tmp_path)
    inv_id = _invoice(db, 1)
    tx1 = _tx(db, 1)
    tx2 = _tx(db, 2)
    svc = BankReconciliationService(db)

    summary = svc.run_auto_reconciliation()
    assert summary["propuestas_revision"] == 1
    # Una segunda pasada tampoco debe proponer la misma factura para el otro movimiento
    svc.run_auto_reconciliation()

    with db.connect() as conn:
        links = conn.execute("SELECT * FROM vinculos_conciliacion WHERE factura_id = ?", (inv_id,)).fetchall()
    assert len(links) == 1
    states = sorted([_tx_state(db, tx1), _tx_state(db, tx2)])
    assert states == ["pendiente", "propuesto"]

    svc.confirm_proposal(int(links[0]["movimiento_id"]))
    inv = db.get_invoice_by_id(inv_id)
    assert inv["importe_conciliado"] == 100.0
    assert inv["estado_pago"] == "pagada"


def test_relinking_movement_recalculates_previous_invoice(tmp_path: Path):
    db = _db(tmp_path)
    inv_a = _invoice(db, 1)
    inv_b = _invoice(db, 2)
    tx = _tx(db, 1)
    svc = BankReconciliationService(db)

    svc.reconcile_manual(tx, [{"factura_id": inv_a, "importe_asignado": 100.0}], aprender_regla=False)
    assert db.get_invoice_by_id(inv_a)["estado_pago"] == "pagada"

    svc.reconcile_manual(tx, [{"factura_id": inv_b, "importe_asignado": 100.0}], aprender_regla=False)
    assert db.get_invoice_by_id(inv_a)["estado_pago"] == "pendiente"
    assert db.get_invoice_by_id(inv_b)["estado_pago"] == "pagada"


def test_deleting_invoice_resets_orphan_movement(tmp_path: Path):
    db = _db(tmp_path)
    inv = _invoice(db, 1)
    tx = _tx(db, 1)
    BankReconciliationService(db).reconcile_manual(
        tx, [{"factura_id": inv, "importe_asignado": 100.0}], aprender_regla=False
    )
    assert _tx_state(db, tx) == "conciliado"
    assert db.delete_invoice(inv) is True
    assert _tx_state(db, tx) == "pendiente"


# ---------------------------------------------------------------------
# 4-5. Extractos bancarios
# ---------------------------------------------------------------------

CAMT_SINGLE_QUOTES = """<?xml version='1.0' encoding='UTF-8'?>
<Document xmlns='urn:iso:std:iso:20022:tech:xsd:camt.053.001.02'
          xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'
          xsi:schemaLocation='urn:iso:std:iso:20022:tech:xsd:camt.053.001.02 camt.053.001.02.xsd'>
  <BkToCstmrStmt>
    <Stmt>
      <Acct><Id><IBAN>ES9121000418450200051332</IBAN></Id></Acct>
      <Ntry>
        <Amt Ccy='EUR'>121.00</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-01-15</Dt></BookgDt>
        <AddtlNtryInf>RECIBO COFARES</AddtlNtryInf>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def test_camt053_with_single_quotes_and_schema_location():
    movs = parse_camt053(CAMT_SINGLE_QUOTES.encode("utf-8"))
    assert len(movs) == 1
    assert movs[0]["importe"] == -121.0
    assert movs[0]["cuenta_iban"] == "ES9121000418450200051332"

    prefixed = CAMT_SINGLE_QUOTES.replace("<", "<c:").replace("<c:/", "</c:").replace("<c:?xml", "<?xml")
    prefixed = prefixed.replace("xmlns='urn", "xmlns:c='urn")
    movs2 = parse_camt053(prefixed)
    assert len(movs2) == 1 and movs2[0]["concepto"] == "RECIBO COFARES"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.234", 1234.0),
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("123,45-", -123.45),
        ("(12,00)", -12.0),
        ("-50,00 €", -50.0),
        ("12.50", 12.5),
        ("EUR 1 234,56", 1234.56),
        ("", 0.0),
    ],
)
def test_parse_amount_cell_spanish_formats(raw: str, expected: float):
    assert _parse_amount_cell(raw) == expected


# ---------------------------------------------------------------------
# 6. Exportación Excel
# ---------------------------------------------------------------------


def test_excel_export_neutralizes_formulas_and_control_chars(tmp_path: Path):
    db = _db(tmp_path)
    _invoice(
        db,
        1,
        total=-30.0,
        proveedor_nombre='=HYPERLINK("http://malo","x")',
        numero_factura="@SUM(A1)",
        notas="texto\x01con\x0bcontrol",
    )
    xlsx = AccountingExcelExporter(db=db).generate_workbook_bytes()
    wb = openpyxl.load_workbook(io.BytesIO(xlsx))
    ws = wb["Facturas Recibidas"]
    values = [c.value for row in ws.iter_rows() for c in row]
    assert not any(isinstance(v, str) and v.startswith(("=", "@")) for v in values)
    assert "'=HYPERLINK(\"http://malo\",\"x\")" in values
    assert -30.0 in values  # los números negativos siguen siendo números


# ---------------------------------------------------------------------
# 7. XSS en el frontend (comprobación estática)
# ---------------------------------------------------------------------


def test_frontend_does_not_inline_json_in_onclick():
    js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "selectTxForManual(${JSON.stringify" not in js
    assert ".replace(/'/g, \"&#39;\")" in js
    assert "<td>${inv.fecha_emision" not in js
    assert "${inv.fecha_vencimiento ||" not in js


# ---------------------------------------------------------------------
# 8. OAuth2: refresco + persistencia y callback
# ---------------------------------------------------------------------


class _FakeOAuthImap:
    def __init__(self, valid_token: str) -> None:
        self.valid_token = valid_token

    def authenticate(self, mech: str, cb: Any) -> None:
        raw = cb(b"").decode()
        if f"Bearer {self.valid_token}" not in raw:
            raise imaplib.IMAP4.error("AUTHENTICATE failed")

    def logout(self) -> None:
        pass


def test_oauth_refreshes_expired_token_and_persists_it():
    saved: List[Dict[str, Any]] = []
    calls: List[Dict[str, str]] = []

    def fake_post(url: str, payload: Dict[str, str]) -> Dict[str, Any]:
        calls.append(payload)
        return {"access_token": "nuevo-token", "refresh_token": "nuevo-refresh"}

    conn = OAuth2MailConnector(
        provider_key="oauth_gmail",
        email_address="farmacia@gmail.com",
        access_token="caducado",
        refresh_token="refresh-1",
        client_id="cid",
        client_secret="secret",
        imap_factory=lambda h, p: _FakeOAuthImap("nuevo-token"),
        on_tokens_refreshed=saved.append,
        token_http_post=fake_post,
    )
    client = conn._connect()
    assert isinstance(client, _FakeOAuthImap)
    assert calls and calls[0]["grant_type"] == "refresh_token"
    assert saved == [{"access_token": "nuevo-token", "refresh_token": "nuevo-refresh"}]


def test_oauth_callback_exchanges_code_and_stores_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app(db_path=tmp_path / "web.sqlite", backup_dir=tmp_path / "backups", start_background_poller=False)
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    app.state.credentials = creds
    creds.set_oauth_tokens("farmacia@gmail.com", {"client_id": "cid", "client_secret": "sec", "provider": "oauth_gmail"})

    captured: Dict[str, Any] = {}

    def fake_exchange(cls: Any, provider_key: str, code: str, client_id: str, client_secret: str = "", **_: Any):
        captured.update(provider=provider_key, code=code, client_id=client_id, client_secret=client_secret)
        return {"access_token": "at-1", "refresh_token": "rt-1"}

    monkeypatch.setattr(OAuth2MailConnector, "exchange_authorization_code", classmethod(fake_exchange))
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/api/mail/oauth/callback", params={"code": "abc", "state": "farmacia@gmail.com"})
    assert r.status_code == 200
    assert "conectada" in r.text
    assert captured == {"provider": "oauth_gmail", "code": "abc", "client_id": "cid", "client_secret": "sec"}
    tokens = creds.get_oauth_tokens("farmacia@gmail.com")
    assert tokens["access_token"] == "at-1" and tokens["refresh_token"] == "rt-1"

    r_err = client.get("/api/mail/oauth/callback", params={"error": "access_denied", "state": "x@y.es"})
    assert r_err.status_code == 400


# ---------------------------------------------------------------------
# 9. Errores por adjunto y por cuenta en el poller
# ---------------------------------------------------------------------


class _ListConnector(MailConnector):
    def __init__(self, envelopes: List[EmailEnvelope]) -> None:
        self.envelopes = envelopes

    def fetch_recent_envelopes(self, limit: int = 50, is_processed=None) -> List[EmailEnvelope]:
        return [e for e in self.envelopes if not (is_processed and is_processed(e.message_id))]


def test_attachment_error_is_recorded_and_message_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _db(tmp_path)
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    acc_id = db.upsert_mail_account(
        {"nombre": "Buzón", "email": "a@farmacia.es", "remitentes_permitidos": ["proveedor.es"]}
    )

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("PDF ilegible")

    monkeypatch.setattr(mail_poller_module, "extract_pdf_content", boom)
    env = EmailEnvelope(
        uid="10",
        message_id="<m1@proveedor.es>",
        sender_name="Prov",
        sender_email="facturas@proveedor.es",
        subject="Factura",
        date_str="",
        pdf_attachments=[PDFAttachment(filename="roto.pdf", content=b"%PDF-1.4 roto")],
    )
    poller = MailPollerService(db=db, credentials=creds, connector_factory=lambda _: _ListConnector([env]))
    account = next(a for a in db.list_mail_accounts() if a["id"] == acc_id)

    stats = poller.sync_account(account)
    assert stats["errores"] and "PDF ilegible" in stats["errores"][0]
    assert db.is_message_processed(acc_id, "<m1@proveedor.es>")
    with db.connect() as conn:
        row = conn.execute("SELECT estado, detalle FROM mensajes_procesados").fetchone()
    assert row["estado"] == "error" and "PDF ilegible" in row["detalle"]
    assert poller.sync_account(account)["mensajes_nuevos"] == 0


def test_sync_all_continues_after_account_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _db(tmp_path)
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    db.upsert_mail_account({"nombre": "Uno", "email": "uno@farmacia.es"})
    db.upsert_mail_account({"nombre": "Dos", "email": "dos@farmacia.es"})
    poller = MailPollerService(db=db, credentials=creds, connector_factory=lambda _: _ListConnector([]))
    original = poller.sync_account

    def flaky(acc: Dict[str, Any]) -> Dict[str, Any]:
        if acc["email"] == "uno@farmacia.es":
            raise RuntimeError("fallo inesperado")
        return original(acc)

    monkeypatch.setattr(poller, "sync_account", flaky)
    results = poller.sync_all_active_accounts()
    assert len(results) == 2
    assert any(r["errores"] and "fallo inesperado" in r["errores"][0] for r in results)
    assert any(r["email"] == "dos@farmacia.es" and not r["errores"] for r in results)


# ---------------------------------------------------------------------
# 10. IMAP con UIDs: solo cabeceras y luego cuerpos de los nuevos
# ---------------------------------------------------------------------


def _raw_mail(msg_id: str) -> bytes:
    m = EmailMessage()
    m["From"] = "facturas@proveedor.es"
    m["Subject"] = "Factura"
    m["Message-ID"] = msg_id
    m.set_content("adjunto")
    m.add_attachment(b"%PDF-1.4 contenido", maintype="application", subtype="pdf", filename="f.pdf")
    return m.as_bytes()


class _FakeUidImap:
    def __init__(self, messages: Dict[str, bytes]) -> None:
        self.messages = messages
        self.commands: List[tuple] = []

    def login(self, *_: Any) -> None:
        pass

    def select(self, mailbox: str, readonly: bool = False):
        return "OK", [b"2"]

    def uid(self, command: str, *args: Any):
        self.commands.append((command, args))
        if command == "SEARCH":
            assert args[0] == "SINCE"
            return "OK", [" ".join(self.messages).encode()]
        if command == "FETCH":
            uid_set, what = args
            out: List[Any] = []
            for uid in uid_set.split(","):
                raw = self.messages[uid]
                if "HEADER.FIELDS" in what:
                    mid = email_header_message_id(raw)
                    out.append((f"{uid} (UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)] {{40}}".encode(), f"Message-ID: {mid}\r\n\r\n".encode()))
                else:
                    out.append((f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        raise AssertionError(command)

    def logout(self) -> None:
        pass


def email_header_message_id(raw: bytes) -> str:
    import email as _email

    return _email.message_from_bytes(raw)["Message-ID"]


def test_imap_uses_uids_and_skips_processed_messages_before_download():
    fake = _FakeUidImap({"101": _raw_mail("<old@x>"), "205": _raw_mail("<new@x>")})
    conn = IMAPMailConnector(
        host="imap.x", port=993, email_address="a@x", password="p", imap_factory=lambda h, p: fake
    )
    envs = conn.fetch_recent_envelopes(is_processed=lambda mid: mid == "<old@x>")
    assert [e.message_id for e in envs] == ["<new@x>"]
    assert envs[0].uid == "205"
    assert len(envs[0].pdf_attachments) == 1
    body_fetches = [a for c, a in fake.commands if c == "FETCH" and "BODY.PEEK[]" in a[1]]
    assert body_fetches == [("205", "(BODY.PEEK[])")]


# ---------------------------------------------------------------------
# 11. Actualizador
# ---------------------------------------------------------------------


def test_updater_asset_selection_and_sha256sums(monkeypatch: pytest.MonkeyPatch):
    assets = [
        {"name": "Archivero-Windows.zip", "browser_download_url": "u/zip"},
        {"name": "ArchiveroUpdater.exe", "browser_download_url": "u/upd"},
        {"name": "Archivero.exe", "browser_download_url": "u/exe"},
        {"name": "Archivero-Mac.zip", "browser_download_url": "u/maczip"},
        {"name": "ArchiveroUpdater", "browser_download_url": "u/macupd"},
        {"name": "Archivero", "browser_download_url": "u/mac"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "u/sums"},
    ]
    assert UpdaterService._select_platform_asset(assets, "Windows")["name"] == "Archivero.exe"
    assert UpdaterService._select_platform_asset(assets, "Darwin")["name"] == "Archivero"
    assert UpdaterService._select_platform_asset(assets, "Linux") is None

    h_exe, h_zip = "a" * 64, "b" * 64
    sums = f"\ufeff{h_exe}  Archivero.exe\r\n{h_zip}  Archivero-Windows.zip\r\n"
    assert parse_sha256sums(sums) == {"Archivero.exe": h_exe, "Archivero-Windows.zip": h_zip}

    svc = UpdaterService()
    monkeypatch.setattr(svc, "_download_text", lambda url: sums)
    assert svc._fetch_expected_hash_from_sums(assets, "Archivero.exe") == h_exe


def test_updater_replaces_via_old_and_backs_up_database(tmp_path: Path):
    db = _db(tmp_path)
    _invoice(db, 1)
    target = tmp_path / "Archivero.exe"
    source = tmp_path / "staging" / "update_Archivero.exe"
    source.parent.mkdir()
    target.write_bytes(b"OLD")
    source.write_bytes(b"NEW")
    rc = updater.apply_update(
        source_path=source,
        target_path=target,
        expected_sha256=hashlib.sha256(b"NEW").hexdigest(),
        db_path=db.db_path,
        backup_dir=tmp_path / "db_backups",
    )
    assert rc == 0
    assert target.read_bytes() == b"NEW"
    assert not target.with_suffix(".exe.old").exists()
    backups = list((tmp_path / "db_backups").glob("*.sqlite"))
    assert len(backups) == 1
    assert Database(db_path=backups[0], backup_dir=tmp_path / "b2").get_invoice_by_id(1) is not None


def test_updater_pid_detection():
    assert updater.is_pid_running(os.getpid()) is True
    assert updater.is_pid_running(0) is False
    assert updater.wait_for_process_exit(2**22 + 12345, timeout_seconds=0.5) is True


# ---------------------------------------------------------------------
# 12. Credenciales
# ---------------------------------------------------------------------


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: Dict[tuple, str] = {}
        self.fail_writes = False

    def set_password(self, service: str, key: str, value: str) -> None:
        if self.fail_writes:
            raise RuntimeError("demasiado largo")
        self.store[(service, key)] = value

    def get_password(self, service: str, key: str):
        return self.store.get((service, key))

    def delete_password(self, service: str, key: str) -> None:
        self.store.pop((service, key), None)


def test_keyring_failure_removes_stale_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(credentials_module, "keyring", fake)
    creds = CredentialManager(service_name="test", vault_dir=tmp_path)
    assert creds.set_secret("k", "viejo") == "system_keyring"
    fake.fail_writes = True
    assert creds.set_secret("k", "nuevo-muy-largo") == "encrypted_vault"
    assert creds.get_secret("k") == "nuevo-muy-largo"


def test_corrupt_vault_is_backed_up_before_overwrite(tmp_path: Path):
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    creds.set_secret("a", "1")
    vault = tmp_path / ".credentials.enc"
    vault.write_bytes(b"basura-no-fernet")
    creds.set_secret("b", "2")
    corrupt = list(tmp_path.glob(".credentials.enc.corrupt-*"))
    assert len(corrupt) == 1 and corrupt[0].read_bytes() == b"basura-no-fernet"
    assert creds.get_secret("b") == "2"


# ---------------------------------------------------------------------
# 13. Varios
# ---------------------------------------------------------------------


def test_get_invoice_by_id_includes_reconciled_amount(tmp_path: Path):
    db = _db(tmp_path)
    inv = _invoice(db, 1)
    tx = _tx(db, 1, importe=-40.0)
    BankReconciliationService(db).reconcile_manual(
        tx, [{"factura_id": inv, "importe_asignado": 40.0}], aprender_regla=False
    )
    got = db.get_invoice_by_id(inv)
    assert got["importe_conciliado"] == 40.0
    assert got["importe_pendiente"] == 60.0


def test_update_invoice_can_clear_nullable_fields(tmp_path: Path):
    db = _db(tmp_path)
    inv = _invoice(db, 1)
    updated = db.update_invoice(inv, {"fecha_vencimiento": None, "total": None})
    assert updated["fecha_vencimiento"] is None
    assert updated["total"] == 100.0  # los campos obligatorios no se vacían

    app = create_app(db_path=tmp_path / "web.sqlite", backup_dir=tmp_path / "backups", start_background_poller=False)
    app.state.credentials = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    inv2 = _invoice(app.state.db, 2)
    r = TestClient(app, base_url="http://127.0.0.1").put(f"/api/invoices/{inv2}", json={"fecha_vencimiento": None})
    assert r.status_code == 200
    assert r.json()["fecha_vencimiento"] is None
    assert r.json()["fecha_emision"] == "2026-01-10"


def test_pdf_attachment_with_leading_bytes_is_accepted():
    m = EmailMessage()
    m["From"] = "x@y.es"
    m["Message-ID"] = "<p@y>"
    m.set_content("hola")
    m.add_attachment(b"\r\n\xef\xbb\xbf%PDF-1.4 contenido", maintype="application", subtype="pdf", filename="f.pdf")
    env = parse_raw_email_message(m.as_bytes())
    assert len(env.pdf_attachments) == 1
