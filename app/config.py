"""Configuración base y constantes del sistema."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

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

BOOTSTRAP_FILENAME = "ubicacion.json"
LEGACY_DATA_DIR = BASE_DIR / "data"


def user_config_dir() -> Path:
    """Carpeta de configuración por usuario (fuera de la carpeta de datos) para el archivo de arranque."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Archivero"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Archivero"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "archivero"


def bootstrap_file_path() -> Path:
    """Archivo que recuerda la carpeta de datos elegida por el usuario (ARCHIVERO_BOOTSTRAP_FILE para tests)."""
    override = os.environ.get("ARCHIVERO_BOOTSTRAP_FILE")
    return Path(override) if override else user_config_dir() / BOOTSTRAP_FILENAME


def default_user_data_dir() -> Path:
    """Carpeta de datos por defecto por usuario (fuera de Descargas/ZIP/Archivos de programa)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Archivero" / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Archivero" / "data"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "archivero" / "data"


def read_bootstrap_data_dir() -> Optional[Path]:
    try:
        raw = json.loads(bootstrap_file_path().read_text(encoding="utf-8"))
        value = (raw or {}).get("data_dir") if isinstance(raw, dict) else None
        if value and Path(value).is_absolute():
            return Path(value)
    except (OSError, ValueError):
        pass
    return None


def write_bootstrap_data_dir(data_dir: Path) -> Path:
    path = bootstrap_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"data_dir": str(data_dir)}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def resolve_data_dir() -> Path:
    """
    Orden de resolución de la carpeta de datos (base de datos, copias, claves cifradas y log):
    1. Variable de entorno ARCHIVERO_DATA_DIR.
    2. Archivo de arranque por usuario (ubicacion.json) elegido desde Configuración.
    3. Carpeta antigua junto al programa (BASE_DIR/data) si ya contiene facturas.sqlite.
    4. Carpeta por usuario del sistema operativo.
    """
    env_dir = os.environ.get("ARCHIVERO_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    chosen = read_bootstrap_data_dir()
    if chosen is not None:
        return chosen
    if (LEGACY_DATA_DIR / "facturas.sqlite").is_file():
        return LEGACY_DATA_DIR
    return default_user_data_dir()


DEFAULT_DATA_DIR = resolve_data_dir()
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

# =====================================================================
# Catálogo de proveedores y modelos de IA
# =====================================================================
# Se usan alias "-latest" o nombres sin fecha siempre que el fabricante los ofrece,
# para que un cambio de versión del modelo no deje el programa sin funcionar.

GOOGLE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_ENDPOINT = "https://api.openai.com/v1"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1"

LLM_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "google_ai_studio": {
        "nombre": "Google Gemini (Google AI Studio)",
        "endpoint": GOOGLE_ENDPOINT,
        "modelo_defecto": "gemini-flash-lite-latest",
        "modelos": [
            {
                "id": "gemini-flash-lite-latest",
                "nombre": "Gemini Flash-Lite (última versión) — Recomendado",
                "descripcion": "El que más usos gratuitos diarios ofrece. Suficiente para la gran mayoría de facturas.",
            },
            {
                "id": "gemini-flash-latest",
                "nombre": "Gemini Flash (última versión)",
                "descripcion": "Algo más preciso con facturas escaneadas o difíciles, pero con menos usos gratuitos al día.",
            },
            {
                "id": "gemini-pro-latest",
                "nombre": "Gemini Pro (última versión)",
                "descripcion": "El más potente. En el plan gratuito tiene muy pocos usos diarios; pensado para el plan de pago.",
            },
        ],
    },
    "openai_compatible": {
        "nombre": "OpenAI (ChatGPT API) o IA local compatible (Ollama, LM Studio...)",
        "endpoint": OPENAI_ENDPOINT,
        "modelo_defecto": "gpt-5-mini",
        "modelos": [
            {
                "id": "gpt-5-mini",
                "nombre": "GPT-5 mini — Recomendado",
                "descripcion": "Buen equilibrio entre precisión y coste (OpenAI).",
            },
            {
                "id": "gpt-5-nano",
                "nombre": "GPT-5 nano",
                "descripcion": "El más barato de OpenAI; puede fallar más en facturas complejas.",
            },
            {
                "id": "gpt-4o-mini",
                "nombre": "GPT-4o mini",
                "descripcion": "Modelo anterior de OpenAI, económico y rápido.",
            },
        ],
    },
    "anthropic": {
        "nombre": "Anthropic (Claude API)",
        "endpoint": ANTHROPIC_ENDPOINT,
        "modelo_defecto": "claude-haiku-5-5",
        "modelos": [
            {
                "id": "claude-haiku-5-5",
                "nombre": "Claude Haiku 5.5 — Recomendado",
                "descripcion": "Rápido y muy económico. Lee PDFs escaneados directamente.",
            },
            {
                "id": "claude-sonnet-5-5",
                "nombre": "Claude Sonnet 5.5",
                "descripcion": "Más preciso con documentos difíciles, con un coste mayor por factura.",
            },
        ],
    },
    "heuristico_local": {
        "nombre": "Modo básico sin Internet",
        "endpoint": "",
        "modelo_defecto": "",
        "modelos": [],
    },
}

# Modelos retirados por los fabricantes que pudieran estar guardados de versiones anteriores
# del programa. Si se detectan, se sustituyen automáticamente por el modelo recomendado.
RETIRED_MODEL_PREFIXES: Dict[str, tuple] = {
    "google_ai_studio": ("gemini-1.", "gemini-2.0", "gemini-pro-vision", "gemini-pro"),
    "openai_compatible": (),
    "anthropic": ("claude-2", "claude-3", "claude-instant"),
}


def resolve_llm_model(provider: str, model: Optional[str]) -> str:
    """Devuelve el modelo a usar: el elegido, o el recomendado si está vacío o retirado."""
    info = LLM_PROVIDERS.get(provider) or LLM_PROVIDERS["google_ai_studio"]
    default_model = info["modelo_defecto"]
    clean = (model or "").strip()
    if not clean:
        return default_model
    if clean == "gemini-pro-latest":
        return clean
    # Un modelo de otro proveedor (p. ej. quedó guardado al cambiar de proveedor) no sirve.
    for other_key, other in LLM_PROVIDERS.items():
        if other_key != provider and any(m["id"] == clean for m in other["modelos"]):
            return default_model
    if clean.startswith(RETIRED_MODEL_PREFIXES.get(provider, ())):
        return default_model
    return clean


def resolve_llm_endpoint(provider: str, endpoint: Optional[str]) -> str:
    """Devuelve la dirección del servicio: la personalizada o la oficial del proveedor elegido."""
    info = LLM_PROVIDERS.get(provider) or LLM_PROVIDERS["google_ai_studio"]
    clean = (endpoint or "").strip().rstrip("/")
    official = {p["endpoint"] for p in LLM_PROVIDERS.values() if p["endpoint"]}
    # Si está vacía o es la oficial de OTRO proveedor (valor por defecto antiguo), usar la propia.
    if not clean or (clean in official and clean != info["endpoint"]):
        return info["endpoint"]
    return clean


# Configuración inicial por defecto en SQLite
DEFAULT_SETTINGS = {
    "carpeta_salida": str(DEFAULT_ARCHIVE_DIR),
    "capa_almacenamiento": "local",  # "local" | "gdrive"
    "gdrive_folder_id": "",
    "proveedor_modelo": "google_ai_studio",  # "google_ai_studio" | "openai_compatible" | "heuristico_local"
    "modelo_llm": "gemini-flash-lite-latest",
    "llm_endpoint_url": "",  # vacío = dirección oficial del proveedor elegido
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
