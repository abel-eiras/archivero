"""Punto de entrada principal de Archivero: servidor web local FastAPI + worker de correo."""

from __future__ import annotations

from contextlib import asynccontextmanager
import io
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import threading
from typing import AsyncIterator, Iterable, Optional
from urllib.parse import urlsplit
import webbrowser

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from app.api.routes import router as api_router
from app.config import (
    APP_NAME,
    APP_VERSION,
    BUNDLE_DIR,
    DAILY_BACKUPS_TO_KEEP,
    DEFAULT_BACKUP_DIR,
    DEFAULT_DATA_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
)
from app.db.database import Database
from app.services.activity import generate_due_date_notice
from app.services.credentials import CredentialManager
from app.services.mail_poller import MailPollerService


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _build_log_file_handler() -> Optional[logging.Handler]:
    """Registro en archivero.log que rota solo (5 MB x 3 copias) para que no crezca sin límite."""
    try:
        DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            DEFAULT_DATA_DIR / "archivero.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        return handler
    except Exception:
        return None


class _StreamToLog(io.TextIOBase):
    """Sustituto de sys.stdout/sys.stderr que escribe en el log rotativo (sin consola en Windows)."""

    def __init__(self, handler: logging.Handler, name: str) -> None:
        self._handler = handler
        self._name = name

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        if text and text.strip():
            record = logging.LogRecord(self._name, logging.INFO, "", 0, text.rstrip("\n"), None, None)
            try:
                self._handler.handle(record)
            except Exception:
                pass
        return len(text or "")

    def flush(self) -> None:
        pass


def _ensure_standard_streams(handler: Optional[logging.Handler] = None) -> bool:
    """
    En ejecutables de Windows compilados con PyInstaller (console=False),
    sys.stdout y sys.stderr son None, lo que provoca un AttributeError ('isatty')
    en el formateador por defecto de Uvicorn. Se redirigen al log rotativo local.
    Devuelve True si se ha redirigido algo.
    """
    redirected = False
    if handler is None:
        handler = globals().get("_LOG_FILE_HANDLER")
    for attr in ("stdout", "stderr"):
        if getattr(sys, attr) is None:
            redirected = True
            if handler is not None:
                setattr(sys, attr, _StreamToLog(handler, attr))
            else:
                setattr(sys, attr, open(os.devnull, "w", encoding="utf-8"))
    return redirected


_LOG_FILE_HANDLER = _build_log_file_handler()
_STREAMS_REDIRECTED = _ensure_standard_streams(_LOG_FILE_HANDLER)

_handlers: list = []
if _LOG_FILE_HANDLER is not None:
    _handlers.append(_LOG_FILE_HANDLER)
if not _STREAMS_REDIRECTED:
    # Con consola: también se ve en pantalla. Sin consola, la pantalla ya es el propio log.
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=_handlers)
logger = logging.getLogger(__name__)

APP_DIR = BUNDLE_DIR / "app"
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _host_header_hostname(host_header: str) -> str:
    """'127.0.0.1:8000' -> '127.0.0.1'; '[::1]:8000' -> '::1'."""
    try:
        return (urlsplit(f"//{host_header.strip()}").hostname or "").lower()
    except ValueError:
        return ""


def _is_local_origin(value: str, allowed: Iterable[str]) -> bool:
    try:
        parts = urlsplit(value.strip())
        return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in allowed
    except ValueError:
        return False


def _asset_version() -> str:
    """Huella de los ficheros estáticos para que el navegador no use versiones antiguas tras actualizar."""
    stamps = []
    for name in ("styles.css", "app.js"):
        try:
            stamps.append(str(int((STATIC_DIR / name).stat().st_mtime)))
        except OSError:
            stamps.append("0")
    return f"{APP_VERSION}-{'-'.join(stamps)}"


def create_app(
    db_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    start_background_poller: bool = True,
    allowed_hosts: Optional[Iterable[str]] = None,
) -> FastAPI:
    """Factoría de la aplicación FastAPI (permite inyectar rutas de test aisladas)."""
    # ARCHIVERO_ALLOWED_HOSTS (separados por comas) permite añadir nombres extra, p. ej. en Docker en red local.
    extra_hosts = [h.strip() for h in os.environ.get("ARCHIVERO_ALLOWED_HOSTS", "").split(",") if h.strip()]
    allowed = frozenset(
        h.lower().strip("[]") for h in list(allowed_hosts or LOCAL_HOSTNAMES) + extra_hosts
    )
    db = Database(db_path=db_path or DEFAULT_DB_PATH, backup_dir=backup_dir or DEFAULT_BACKUP_DIR)
    credentials = CredentialManager(vault_dir=(db_path.parent if db_path else None))
    mail_poller = MailPollerService(db=db, credentials=credentials)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        try:
            purged = db.purge_old_events(days=180)
            db.log_event(
                tipo="inicio_programa",
                mensaje=f"{APP_NAME} {APP_VERSION} se ha puesto en marcha.",
                detalle={"avisos_antiguos_borrados": purged},
                leido=True,
            )
            generate_due_date_notice(db)
            db.create_daily_backup_if_needed(keep=DAILY_BACKUPS_TO_KEEP)
        except Exception as exc:  # El arranque nunca debe fallar por el registro de actividad
            logger.warning("No se pudo preparar el registro de actividad: %s", exc)
        if start_background_poller:
            mail_poller.start_background_worker()
        try:
            yield
        finally:
            if start_background_poller:
                mail_poller.stop_background_worker()

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        description="Gestor local de facturas por correo y conciliación bancaria para farmacias",
        lifespan=lifespan,
    )

    app.state.db = db
    app.state.credentials = credentials
    app.state.mail_poller = mail_poller

    @app.middleware("http")
    async def local_only_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        """
        Protección del servidor local frente a DNS rebinding y CSRF:
        - La cabecera Host debe ser 127.0.0.1, localhost o [::1] (cualquier puerto).
        - En peticiones que modifican datos, Origin (o en su defecto Referer) debe ser local.
        """
        host_name = _host_header_hostname(request.headers.get("host", ""))
        if host_name not in allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": "Acceso denegado: Archivero solo acepta conexiones desde este mismo ordenador."},
            )
        if request.method.upper() not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin if origin is not None else referer
            if source is not None and not _is_local_origin(source, allowed):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Acceso denegado: la petición no procede de la propia aplicación Archivero."},
                )
        return await call_next(request)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.include_router(api_router)

    @app.get("/", response_class=HTMLResponse)
    def index_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "app_name": APP_NAME,
                "app_version": APP_VERSION,
                "asset_version": _asset_version(),
            },
        )

    @app.get("/presentacion", response_class=HTMLResponse)
    def presentation_page() -> HTMLResponse:
        pres_path = BUNDLE_DIR / "docs" / "presentacion_proyecto.html"
        if pres_path.is_file():
            return HTMLResponse(content=pres_path.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Dossier no encontrado</h1>", status_code=404)

    @app.get("/presentacion/pdf")
    def presentation_pdf() -> FileResponse:
        pdf_path = BUNDLE_DIR / "docs" / "Dossier_Presentacion_Archivero.pdf"
        return FileResponse(
            path=str(pdf_path),
            media_type="application/pdf",
            filename="Dossier_Presentacion_Archivero.pdf",
        )

    @app.get("/health")
    def healthcheck() -> dict[str, str]:
        return {"status": "ok", "version": APP_VERSION}

    return app


app = create_app(start_background_poller=False)


def _is_port_already_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def run_server() -> None:
    """Arranca el servidor web escuchando exclusivamente en localhost (127.0.0.1) y abre el navegador."""
    _ensure_standard_streams()
    url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

    # Si el usuario hace doble clic en Archivero.exe cuando ya estaba abierto en segundo plano,
    # simplemente abrimos el navegador en http://127.0.0.1:8000 sin fallar por puerto ocupado.
    if _is_port_already_in_use(DEFAULT_HOST, DEFAULT_PORT):
        logger.info("%s ya está en ejecución en %s. Abriendo navegador...", APP_NAME, url)
        if os.environ.get("ARCHIVERO_NO_BROWSER", "0") != "1":
            webbrowser.open(url)
        return

    production_app = create_app(start_background_poller=True)
    logger.info("Iniciando %s v%s en %s", APP_NAME, APP_VERSION, url)

    if os.environ.get("ARCHIVERO_NO_BROWSER", "0") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        production_app,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        log_level="info",
        log_config=None,
    )


if __name__ == "__main__":
    run_server()
