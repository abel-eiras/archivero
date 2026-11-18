"""Módulo de correo: conectores IMAP y OAuth2 (Gmail/Outlook), filtrado de candidatas y poller en segundo plano."""

from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.db.database import Database
from app.services.credentials import CredentialManager
from app.services.pdf_extractor import extract_pdf_content, looks_like_invoice
from app.services.pipeline import InvoiceIngestionPipeline

logger = logging.getLogger(__name__)


@dataclass
class PDFAttachment:
    """Adjunto PDF extraído de un correo electrónico."""

    filename: str
    content: bytes


@dataclass
class EmailEnvelope:
    """Representación normalizada de un mensaje de correo con sus adjuntos PDF."""

    uid: str
    message_id: str
    sender_name: str
    sender_email: str
    subject: str
    date_str: str
    pdf_attachments: List[PDFAttachment] = field(default_factory=list)


def _decode_mime_words(raw_val: Optional[str]) -> str:
    if not raw_val:
        return ""
    parts = decode_header(raw_val)
    decoded_fragments: List[str] = []
    for content, charset in parts:
        if isinstance(content, bytes):
            enc = charset or "utf-8"
            try:
                decoded_fragments.append(content.decode(enc, errors="replace"))
            except LookupError:
                decoded_fragments.append(content.decode("utf-8", errors="replace"))
        else:
            decoded_fragments.append(str(content))
    return "".join(decoded_fragments).strip()


def parse_raw_email_message(raw_bytes: bytes, uid: str = "") -> EmailEnvelope:
    """Parsea un mensaje RFC822 bruto extrayendo cabeceras y adjuntos PDF."""
    msg: Message = email.message_from_bytes(raw_bytes)
    raw_msg_id = (msg.get("Message-ID") or "").strip()
    if not raw_msg_id:
        raw_msg_id = f"<sha256-{hashlib.sha256(raw_bytes).hexdigest()[:24]}@local>"

    from_header = _decode_mime_words(msg.get("From"))
    sender_name, sender_addr = parseaddr(from_header)
    sender_email = (sender_addr or from_header).strip().lower()
    subject = _decode_mime_words(msg.get("Subject"))
    date_str = (msg.get("Date") or "").strip()

    attachments: List[PDFAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        raw_filename = part.get_filename()
        filename = _decode_mime_words(raw_filename) if raw_filename else ""

        is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
        if is_pdf:
            payload = part.get_payload(decode=True)
            if payload and payload.startswith(b"%PDF"):
                attachments.append(
                    PDFAttachment(
                        filename=filename or f"adjunto_{len(attachments) + 1}.pdf",
                        content=payload,
                    )
                )

    return EmailEnvelope(
        uid=uid,
        message_id=raw_msg_id,
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        date_str=date_str,
        pdf_attachments=attachments,
    )


class MailConnector(ABC):
    """Interfaz interna única para modos de conexión a correo (IMAP genérico y OAuth2)."""

    @abstractmethod
    def fetch_recent_envelopes(self, limit: int = 50) -> List[EmailEnvelope]:
        """Recupera los mensajes del buzón con adjuntos PDF."""


class IMAPMailConnector(MailConnector):
    """Conector IMAP genérico con usuario y contraseña de aplicación."""

    def __init__(
        self,
        host: str,
        port: int,
        email_address: str,
        password: str,
        use_ssl: bool = True,
        mailbox: str = "INBOX",
        imap_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.email_address = email_address
        self.password = password
        self.use_ssl = use_ssl
        self.mailbox = mailbox
        self.imap_factory = imap_factory

    def _connect(self) -> Any:
        if self.imap_factory is not None:
            client = self.imap_factory(self.host, self.port)
        elif self.use_ssl:
            client = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            client = imaplib.IMAP4(self.host, self.port)
        client.login(self.email_address, self.password)
        return client

    def fetch_recent_envelopes(self, limit: int = 50) -> List[EmailEnvelope]:
        client = self._connect()
        envelopes: List[EmailEnvelope] = []
        try:
            status, _ = client.select(self.mailbox, readonly=True)
            if status != "OK":
                return envelopes
            status, data = client.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return envelopes

            msg_nums = data[0].split()
            for num in msg_nums[-limit:]:
                uid_str = num.decode("ascii", errors="ignore") if isinstance(num, bytes) else str(num)
                st, msg_data = client.fetch(num, "(RFC822)")
                if st != "OK" or not msg_data:
                    continue
                for item in msg_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                        envelopes.append(parse_raw_email_message(item[1], uid=uid_str))
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return envelopes


class OAuth2MailConnector(MailConnector):
    """
    Conector OAuth2 para Gmail y Outlook mediante IMAP + XOAUTH2.
    Soporta refresco automático del access_token usando el refresh_token almacenado en keyring.
    """

    PROVIDERS = {
        "oauth_gmail": {
            "imap_host": "imap.gmail.com",
            "imap_port": 993,
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scope": "https://mail.google.com/",
        },
        "oauth_outlook": {
            "imap_host": "outlook.office365.com",
            "imap_port": 993,
            "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        },
    }

    def __init__(
        self,
        provider_key: str,
        email_address: str,
        access_token: str = "",
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        mailbox: str = "INBOX",
        imap_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.provider_key = provider_key if provider_key in self.PROVIDERS else "oauth_gmail"
        self.provider_cfg = self.PROVIDERS[self.provider_key]
        self.email_address = email_address
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.mailbox = mailbox
        self.imap_factory = imap_factory

    @classmethod
    def build_authorization_url(
        cls,
        provider_key: str,
        client_id: str,
        redirect_uri: str = "http://127.0.0.1:8000/api/mail/oauth/callback",
        state: str = "",
    ) -> str:
        cfg = cls.PROVIDERS.get(provider_key, cls.PROVIDERS["oauth_gmail"])
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": cfg["scope"],
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"{cfg['auth_url']}?{urlencode(params)}"

    def refresh_access_token_if_needed(self) -> str:
        """Renueva el access_token usando el refresh_token contra el servidor OAuth2."""
        if not self.refresh_token or not self.client_id:
            return self.access_token
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(self.provider_cfg["token_url"], data=payload)
            resp.raise_for_status()
            token_data = resp.json()
            self.access_token = token_data.get("access_token", self.access_token)
            return self.access_token

    def _build_xoauth2_bytes(self, token: str) -> bytes:
        auth_string = f"user={self.email_address}\x01auth=Bearer {token}\x01\x01"
        return auth_string.encode("utf-8")

    def _connect(self) -> Any:
        token = self.access_token or self.refresh_access_token_if_needed()
        if not token:
            raise ValueError(f"No hay token OAuth2 disponible para {self.email_address}")

        host = self.provider_cfg["imap_host"]
        port = int(self.provider_cfg["imap_port"])
        client = (
            self.imap_factory(host, port)
            if self.imap_factory is not None
            else imaplib.IMAP4_SSL(host, port)
        )
        client.authenticate("XOAUTH2", lambda _: self._build_xoauth2_bytes(token))
        return client

    def fetch_recent_envelopes(self, limit: int = 50) -> List[EmailEnvelope]:
        client = self._connect()
        envelopes: List[EmailEnvelope] = []
        try:
            status, _ = client.select(self.mailbox, readonly=True)
            if status != "OK":
                return envelopes
            status, data = client.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return envelopes
            msg_nums = data[0].split()
            for num in msg_nums[-limit:]:
                uid_str = num.decode("ascii", errors="ignore") if isinstance(num, bytes) else str(num)
                st, msg_data = client.fetch(num, "(RFC822)")
                if st != "OK" or not msg_data:
                    continue
                for item in msg_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                        envelopes.append(parse_raw_email_message(item[1], uid=uid_str))
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return envelopes


class MailPollerService:
    """
    Servicio de consulta periódica de buzones de correo, filtrado de candidatas
    e ingesta de facturas PDF sin reprocesar nunca el mismo mensaje.
    """

    def __init__(
        self,
        db: Database,
        credentials: CredentialManager,
        connector_factory: Optional[Callable[[Dict[str, Any]], MailConnector]] = None,
    ) -> None:
        self.db = db
        self.credentials = credentials
        self.connector_factory = connector_factory
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def build_connector_for_account(self, account: Dict[str, Any]) -> MailConnector:
        if self.connector_factory is not None:
            return self.connector_factory(account)

        email_addr = account["email"]
        conn_type = account.get("tipo_conexion") or "imap"

        if conn_type in ("oauth_gmail", "oauth_outlook"):
            tokens = self.credentials.get_oauth_tokens(email_addr)
            return OAuth2MailConnector(
                provider_key=conn_type,
                email_address=email_addr,
                access_token=tokens.get("access_token", ""),
                refresh_token=tokens.get("refresh_token", ""),
                client_id=tokens.get("client_id", ""),
                client_secret=tokens.get("client_secret", ""),
            )

        password = self.credentials.get_mail_password(email_addr) or ""
        host = account.get("servidor_imap") or ""
        port = int(account.get("puerto_imap") or 993)
        use_ssl = bool(account.get("usar_ssl", True))
        return IMAPMailConnector(
            host=host,
            port=port,
            email_address=email_addr,
            password=password,
            use_ssl=use_ssl,
        )

    def is_sender_in_whitelist(self, sender_email: str, account: Dict[str, Any]) -> bool:
        """Comprueba si el remitente está en la lista configurada o en las reglas de remitente."""
        s_low = (sender_email or "").lower().strip()
        if not s_low:
            return False

        account_senders = [x.lower().strip() for x in (account.get("remitentes_permitidos") or []) if x]
        global_senders = [
            x.lower().strip()
            for x in (self.db.get_setting("remitentes_globales") or [])
            if x
        ]
        all_allowed = account_senders + global_senders

        for pattern in all_allowed:
            if pattern in s_low:
                return True

        # También si existe una regla activa para ese remitente
        rule_match = self.db.match_rules_for_invoice(sender_email=s_low)
        return bool(rule_match)

    def sync_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sincroniza una cuenta de correo:
        - Descarga sobres recientes.
        - Ignora los ya registrados en `mensajes_procesados`.
        - Evalúa si el mensaje es candidato (remitente en lista o PDF que parece factura).
        - Procesa cada PDF adjunto con el pipeline e inserta el registro en SQLite.
        """
        account_id = int(account["id"])
        stats = {
            "cuenta_id": account_id,
            "email": account["email"],
            "mensajes_nuevos": 0,
            "facturas_insertadas": 0,
            "facturas_duplicadas": 0,
            "mensajes_descartados": 0,
            "errores": [],
        }

        try:
            connector = self.build_connector_for_account(account)
            envelopes = connector.fetch_recent_envelopes()
        except Exception as exc:
            err_msg = f"Error conectando a {account['email']}: {exc}"
            logger.warning(err_msg)
            self.db.update_mail_account_sync_status(account_id, error=str(exc))
            stats["errores"].append(err_msg)
            return stats

        settings = self.db.get_all_settings()
        provider = settings.get("proveedor_modelo") or "google_ai_studio"
        llm_key = self.credentials.get_llm_api_key(provider)
        gdrive_token = self.credentials.get_gdrive_token()

        pipeline = InvoiceIngestionPipeline(
            db=self.db,
            llm_api_key=llm_key,
            gdrive_access_token=gdrive_token,
        )

        for env in envelopes:
            if self.db.is_message_processed(account_id, env.message_id):
                continue

            stats["mensajes_nuevos"] += 1

            if not env.pdf_attachments:
                self.db.mark_message_processed(
                    cuenta_id=account_id,
                    message_id=env.message_id,
                    uid_imap=env.uid,
                    remitente=env.sender_email,
                    asunto=env.subject,
                    fecha_mensaje=env.date_str,
                    estado="ignorado_sin_pdf",
                    detalle="Correo sin adjuntos PDF",
                )
                stats["mensajes_descartados"] += 1
                continue

            sender_whitelisted = self.is_sender_in_whitelist(env.sender_email, account)
            processed_any = False
            details: List[str] = []

            for att in env.pdf_attachments:
                pdf_content = extract_pdf_content(
                    att.content,
                    filename=att.filename,
                    ocr_strategy=settings.get("estrategia_ocr") or "vision_llm",
                )
                # Sección 4: candidato si viene de dirección configurada O si trae un PDF que parece factura
                if not sender_whitelisted and not looks_like_invoice(
                    pdf_content, filename=att.filename, subject=env.subject
                ):
                    details.append(f"{att.filename}: descartado (no parece factura)")
                    continue

                res = pipeline.ingest_pdf(
                    pdf_source=att.content,
                    filename=att.filename,
                    sender_email=env.sender_email,
                    message_id=env.message_id,
                )
                processed_any = True
                details.append(f"{att.filename}: {res.status}")
                if res.status == "insertada":
                    stats["facturas_insertadas"] += 1
                elif res.status == "duplicada":
                    stats["facturas_duplicadas"] += 1

            self.db.mark_message_processed(
                cuenta_id=account_id,
                message_id=env.message_id,
                uid_imap=env.uid,
                remitente=env.sender_email,
                asunto=env.subject,
                fecha_mensaje=env.date_str,
                estado="procesado" if processed_any else "descartado",
                detalle="; ".join(details),
            )
            if not processed_any:
                stats["mensajes_descartados"] += 1

        self.db.update_mail_account_sync_status(account_id, error=None)
        return stats

    def sync_all_active_accounts(self) -> List[Dict[str, Any]]:
        """Sincroniza todas las cuentas de correo activas de forma segura."""
        with self._lock:
            accounts = self.db.list_mail_accounts(active_only=True)
            return [self.sync_account(acc) for acc in accounts]

    def start_background_worker(self) -> None:
        """Arranca el hilo en segundo plano que vigila los buzones periódicamente."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="Archivero-MailPoller",
            daemon=True,
        )
        self._thread.start()
        logger.info("Proceso de vigilancia de correo en segundo plano iniciado.")

    def stop_background_worker(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.db.get_setting("poller_activo", True):
                    self.sync_all_active_accounts()
            except Exception as exc:
                logger.error("Error en ciclo del worker de correo: %s", exc)

            interval_mins = int(self.db.get_setting("intervalo_correo_minutos", 15) or 15)
            wait_seconds = max(30, interval_mins * 60)
            if self._stop_event.wait(timeout=wait_seconds):
                break
