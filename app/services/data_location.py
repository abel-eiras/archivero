"""Cambio de la carpeta de datos del programa (base de datos, copias de seguridad y claves cifradas)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict

from app.config import write_bootstrap_data_dir

DB_FILENAME = "facturas.sqlite"
VAULT_FILES = (".credentials.enc", ".vault_key")


def validate_target_dir(raw_path: str) -> Path:
    """Comprueba que la ruta es absoluta, se puede crear y se puede escribir en ella."""
    ruta = (raw_path or "").strip().strip('"')
    if not ruta:
        raise ValueError("Indica la carpeta donde quieres guardar los datos.")
    target = Path(os.path.expanduser(ruta))
    if not target.is_absolute():
        raise ValueError("La ruta debe ser completa (por ejemplo C:\\Users\\Farmacia\\Documents\\Archivero).")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"No se puede crear la carpeta {target}: {exc}") from exc
    if not target.is_dir():
        raise ValueError(f"{target} no es una carpeta.")
    probe = target / f".prueba_escritura_{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"No se puede escribir en {target}: {exc}") from exc
    return target.resolve()


def change_data_dir(raw_path: str, current_db_path: Path, current_vault_dir: Path) -> Dict[str, Any]:
    """
    Prepara la nueva carpeta de datos y la deja recordada para el próximo arranque.
    - Si la carpeta ya contiene una base de datos, se usa tal cual (no se copia nada).
    - Si no, se copia la base de datos con la API de backup de SQLite y la bóveda de claves.
    La carpeta anterior nunca se borra.
    """
    target = validate_target_dir(raw_path)
    current_dir = current_db_path.parent.resolve()
    if target == current_dir:
        raise ValueError("Esa ya es la carpeta de datos actual.")

    target_db = target / DB_FILENAME
    existing_db = target_db.is_file()
    copiados: list[str] = []

    if not existing_db:
        if current_db_path.is_file():
            tmp_db = target / (DB_FILENAME + ".copiando")
            src = sqlite3.connect(str(current_db_path))
            try:
                dst = sqlite3.connect(str(tmp_db))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            os.replace(tmp_db, target_db)
            copiados.append(DB_FILENAME)
        for name in VAULT_FILES:
            src_file = Path(current_vault_dir) / name
            if src_file.is_file() and not (target / name).exists():
                shutil.copy2(src_file, target / name)
                copiados.append(name)

    bootstrap = write_bootstrap_data_dir(target)

    if existing_db:
        mensaje = (
            f"Se ha encontrado una base de datos existente en {target} y se usará esa. "
            "Cierra y vuelve a abrir Archivero para empezar a usarla."
        )
    else:
        mensaje = (
            f"Datos copiados a {target}. Cierra y vuelve a abrir Archivero para empezar a usar la nueva carpeta. "
            f"La carpeta anterior ({current_dir}) no se ha borrado."
        )
    if os.environ.get("ARCHIVERO_DATA_DIR"):
        mensaje += " Atención: la variable de entorno ARCHIVERO_DATA_DIR tiene prioridad sobre esta elección."

    return {
        "carpeta_datos_nueva": str(target),
        "carpeta_datos_actual": str(current_dir),
        "base_datos_existente": existing_db,
        "archivos_copiados": copiados,
        "archivo_ubicacion": str(bootstrap),
        "requiere_reinicio": True,
        "mensaje": mensaje,
    }
