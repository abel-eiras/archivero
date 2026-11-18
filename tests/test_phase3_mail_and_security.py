"""Tests de Fase 3, 8 y 10: Credenciales seguras (sin SQLite) y sincronización de correo IMAP/OAuth2."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from typing import List

from app.db.database import Database
from app.services.credentials import CredentialManager
from app.services.mail_poller import (
    EmailEnvelope,
    MailConnector,
    MailPollerService,
    OAuth2MailConnector,
    parse_raw_email_message,
)
from scripts.generate_sample_data import build_invoice_pdf_bytes, get_sample_invoices_specs


class FakeMailConnector(MailConnector):
    def __init__(self, envelopes: List[EmailEnvelope]) -> None:
        self.envelopes = envelopes

    def fetch_recent_envelopes(self, limit: int = 50) -> List[EmailEnvelope]:
        return self.envelopes[:limit]


def _build_rfc822_email_with_pdf(
    message_id: str,
    sender: str,
    subject: str,
    pdf_bytes: bytes,
    pdf_filename: str = "factura.pdf",
) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = sender
    msg["To"] = "farmacia@example.com"
    msg["Subject"] = subject
    msg.set_content("Adjuntamos su factura mensual en formato PDF.")
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=pdf_filename,
    )
    return msg.as_bytes()


def test_credentials_stored_outside_sqlite(tmp_path: Path):
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)
    creds.set_llm_api_key("google_ai_studio", "AIzaSyTestSecretKey12345")
    creds.set_mail_password("admin@farmacia.es", "AppPasswordSecret99")
    creds.set_oauth_tokens("farmacia@gmail.com", {"access_token": "ya29.tok", "refresh_token": "1//ref"})

    assert creds.get_llm_api_key("google_ai_studio") == "AIzaSyTestSecretKey12345"
    assert creds.get_mail_password("admin@farmacia.es") == "AppPasswordSecret99"
    assert creds.get_oauth_tokens("farmacia@gmail.com")["refresh_token"] == "1//ref"

    # Verificar que el archivo en disco está cifrado y NO contiene las claves en texto plano
    vault_raw = (tmp_path / ".credentials.enc").read_bytes()
    assert b"AIzaSyTestSecretKey12345" not in vault_raw
    assert b"AppPasswordSecret99" not in vault_raw


def test_oauth_authorization_url_generation():
    gmail_url = OAuth2MailConnector.build_authorization_url(
        provider_key="oauth_gmail",
        client_id="my-google-client-id",
        state="farmacia@gmail.com",
    )
    assert "accounts.google.com" in gmail_url
    assert "my-google-client-id" in gmail_url

    outlook_url = OAuth2MailConnector.build_authorization_url(
        provider_key="oauth_outlook",
        client_id="my-ms-client-id",
        state="farmacia@outlook.es",
    )
    assert "login.microsoftonline.com" in outlook_url
    assert "my-ms-client-id" in outlook_url


def test_mail_poller_processes_new_messages_once_and_applies_sender_rules(tmp_path: Path):
    db = Database(db_path=tmp_path / "mail_test.sqlite", backup_dir=tmp_path / "backups")
    db.set_settings({"carpeta_salida": str(tmp_path / "archivo")})
    creds = CredentialManager(vault_dir=tmp_path, force_encrypted_fallback=True)

    acc_id = db.upsert_mail_account(
        {
            "nombre": "Administración",
            "email": "admin@farmacia.es",
            "tipo_conexion": "imap",
            "servidor_imap": "imap.farmacia.es",
            "remitentes_permitidos": ["distribuidoranorte.es"],
        }
    )

    # Regla por remitente: todo lo que venga de energiapeninsular.es es categoría 'suministros'
    db.create_rule(
        tipo_regla="remitente_categoria",
        patron="energiapeninsular.es",
        proveedor_nombre="Comercializadora Electrica Peninsular S.A.",
        categoria="suministros",
        tipo_documento="factura_servicios",
    )

    spec_dist = get_sample_invoices_specs()[0]
    raw_msg_1 = _build_rfc822_email_with_pdf(
        message_id="<msg-001@distribuidoranorte.es>",
        sender="Facturación Distribuidora <facturacion@distribuidoranorte.es>",
        subject="Factura mensual Distribuidora Norte",
        pdf_bytes=build_invoice_pdf_bytes(spec_dist["lines"]),
        pdf_filename="dist_norte_nov.pdf",
    )
    env_1 = parse_raw_email_message(raw_msg_1, uid="101")

    envelopes = [env_1]
    poller = MailPollerService(
        db=db,
        credentials=creds,
        connector_factory=lambda _: FakeMailConnector(envelopes),
    )

    # Primera sincronización: procesa el mensaje e inserta 1 factura
    res1 = poller.sync_all_active_accounts()
    assert len(res1) == 1
    assert res1[0]["mensajes_nuevos"] == 1
    assert res1[0]["facturas_insertadas"] == 1

    # Segunda sincronización con el mismo buzón: NO reprocesa el mensaje (<msg-001@distribuidoranorte.es>)
    res2 = poller.sync_all_active_accounts()
    assert res2[0]["mensajes_nuevos"] == 0
    assert res2[0]["facturas_insertadas"] == 0
    assert db.is_message_processed(acc_id, "<msg-001@distribuidoranorte.es>") is True
