"""Configuración base y constantes del sistema."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Dict, List

APP_NAME = "Archivero"
APP_VERSION = "0.1.0"
KEYRING_SERVICE_NAME = "Archivero_Local"

# Cuando se ejecuta como .exe empaquetado (PyInstaller), los datos persistentes
# se guardan junto al ejecutable y los recursos estáticos se leen de _MEIPASS.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = BASE_DIR

DEFAULT_DATA_DIR = Path(os.environ.get("ARCHIVERO_DATA_DIR", BASE_DIR / "data"))
DEFAULT_DB_PATH = Path(os.environ.get("ARCHIVERO_DB_PATH", DEFAULT_DATA_DIR / "facturas.sqlite"))
DEFAULT_ARCHIVE_DIR = Path(os.environ.get("ARCHIVERO_ARCHIVE_DIR", BASE_DIR / "archivo_facturas"))
DEFAULT_BACKUP_DIR = Path(os.environ.get("ARCHIVERO_BACKUP_DIR", DEFAULT_DATA_DIR / "backups"))
DEFAULT_STAGING_DIR = Path(os.environ.get("ARCHIVERO_STAGING_DIR", DEFAULT_DATA_DIR / ".tmp_staging"))

# Servidor local: estrictamente localhost por seguridad (Sección 3 y 10)
DEFAULT_HOST = os.environ.get("ARCHIVERO_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ARCHIVERO_PORT", "8000"))

# Tipos de documento iniciales (Sección 5)
TIPOS_DOCUMENTO: Dict[str, str] = {
    "factura_mercancia": "Factura de compra de mercancía",
    "factura_gasto": "Factura de gasto o material",
    "factura_servicios": "Factura de servicios",
    "alquiler": "Alquiler",
    "abono": "Abono o factura rectificativa",
    "otros": "Otros",
}

# Categorías genéricas y abiertas de partida (Sección 5)
CATEGORIAS_INICIALES: List[Dict[str, str]] = [
    {"codigo": "compras_mercancia", "nombre": "Compras de mercancía para vender"},
    {"codigo": "gastos_material", "nombre": "Gastos de material para uso propio"},
    {"codigo": "suministros", "nombre": "Suministros (agua, luz, gas, internet)"},
    {"codigo": "servicios_profesionales", "nombre": "Servicios profesionales"},
    {"codigo": "alquiler", "nombre": "Alquiler"},
    {"codigo": "seguros", "nombre": "Seguros"},
    {"codigo": "impuestos_tasas", "nombre": "Impuestos y tasas"},
    {"codigo": "otros", "nombre": "Otros"},
]

MESES_NOMBRES: Dict[int, str] = {
    1: "01-Enero",
    2: "02-Febrero",
    3: "03-Marzo",
    4: "04-Abril",
    5: "05-Mayo",
    6: "06-Junio",
    7: "07-Julio",
    8: "08-Agosto",
    9: "09-Septiembre",
    10: "10-Octubre",
    11: "11-Noviembre",
    12: "12-Diciembre",
}

# Configuración inicial por defecto en SQLite
DEFAULT_SETTINGS = {
    "carpeta_salida": str(DEFAULT_ARCHIVE_DIR),
    "capa_almacenamiento": "local",  # "local" | "gdrive"
    "gdrive_folder_id": "",
    "proveedor_modelo": "google_ai_studio",  # "google_ai_studio" | "openai_compatible" | "heuristico_local"
    "modelo_llm": "gemini-2.0-flash",
    "llm_endpoint_url": "https://generativelanguage.googleapis.com/v1beta",
    "estrategia_ocr": "vision_llm",  # "vision_llm" | "ocr_local"
    "umbral_confianza": 0.85,
    "intervalo_correo_minutos": 15,
    "poller_activo": True,
    "github_repo": "abel-eiras/archivero",
    "github_repo_privado": False,
    "categorias": CATEGORIAS_INICIALES,
    "remitentes_globales": [],
    "aviso_privacidad_aceptado": False,
}
