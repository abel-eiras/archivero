"""Punto de entrada principal de FarmaFacturas: servidor web local FastAPI + worker de correo."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from app.api.routes import router as api_router
from app.config import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_BACKUP_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
)
from app.db.database import Database
from app.services.credentials import CredentialManager
from app.services.mail_poller import MailPollerService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def create_app(
    db_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    start_background_poller: bool = True,
) -> FastAPI:
    """Factoría de la aplicación FastAPI (permite inyectar rutas de test aisladas)."""
    db = Database(db_path=db_path or DEFAULT_DB_PATH, backup_dir=backup_dir or DEFAULT_BACKUP_DIR)
    credentials = CredentialManager(vault_dir=(db_path.parent if db_path else None))
    mail_poller = MailPollerService(db=db, credentials=credentials)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
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
            context={"app_name": APP_NAME, "app_version": APP_VERSION},
        )

    @app.get("/presentacion", response_class=HTMLResponse)
    def presentation_page() -> HTMLResponse:
        pres_path = APP_DIR.parent / "docs" / "presentacion_proyecto.html"
        if pres_path.is_file():
            return HTMLResponse(content=pres_path.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Dossier no encontrado</h1>", status_code=404)

    @app.get("/presentacion/pdf")
    def presentation_pdf() -> FileResponse:
        pdf_path = APP_DIR.parent / "docs" / "Dossier_Presentacion_Archivero.pdf"
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


def run_server() -> None:
    """Arranca el servidor web escuchando exclusivamente en localhost (127.0.0.1)."""
    production_app = create_app(start_background_poller=True)
    logger.info("Iniciando %s v%s en http://%s:%d", APP_NAME, APP_VERSION, DEFAULT_HOST, DEFAULT_PORT)
    uvicorn.run(
        production_app,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    run_server()
