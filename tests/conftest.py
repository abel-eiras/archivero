"""Aislamiento de los tests: importar app.main crea una base de datos en la carpeta de datos por defecto,
así que antes de importar nada del programa se apuntan todas las carpetas a un directorio temporal."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="archivero_tests_"))
os.environ.setdefault("ARCHIVERO_DATA_DIR", str(_TEST_ROOT / "data"))
os.environ.setdefault("ARCHIVERO_ARCHIVE_DIR", str(_TEST_ROOT / "archivo"))
os.environ.setdefault("ARCHIVERO_BOOTSTRAP_FILE", str(_TEST_ROOT / "ubicacion.json"))
os.environ.setdefault("ARCHIVERO_NO_BROWSER", "1")
