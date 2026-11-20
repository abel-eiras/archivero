#!/usr/bin/env python3
"""
Actualizador independiente de Archivero (Sección 3 y 11).

En Windows un ejecutable no puede sobrescribirse mientras está en ejecución.
Este programa independiente:
1. Verifica obligatoriamente el hash SHA-256 del nuevo ejecutable descargado.
2. Espera a que el proceso principal (PID) termine.
3. Si se indica --db-path, crea una copia de seguridad de la base de datos SQLite
   (en --backup-dir o junto a la base de datos) antes de tocar nada.
4. Guarda una copia del ejecutable anterior (.bak), renombra el ejecutable en uso a .old
   (Windows permite renombrar, pero no sobrescribir, un .exe bloqueado) y copia el nuevo
   con reintentos; si algo falla restaura el anterior.
5. Relanza la aplicación actualizada si se solicita (--relaunch).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

# En Windows evita que se abra una ventana de consola al lanzar procesos auxiliares
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _log(msg: str) -> None:
    # Con console=False sys.stderr puede ser None: print() lo ignora sin fallar
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _is_pid_running_windows(pid: int) -> bool:
    """Comprueba un PID en Windows con OpenProcess/GetExitCodeProcess (sin psutil)."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Acceso denegado implica que el proceso existe
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass

    # Alternativa: tasklist en CSV comparando el PID exacto (no por subcadena)
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            creationflags=_NO_WINDOW_FLAGS,
        )
    except Exception:
        return False
    for line in out.splitlines():
        cols = [c.strip().strip('"') for c in line.split('","')]
        if len(cols) >= 2 and cols[1].strip('"') == str(pid):
            return True
    return False


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_pid_running_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def wait_for_process_exit(pid: int, timeout_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_pid_running(pid):
            return True
        time.sleep(0.25)
    return not is_pid_running(pid)


def _retry(action: Callable[[], None], attempts: int = 20, delay: float = 0.5) -> None:
    """Reintenta operaciones de archivo que fallan mientras Windows libera el bloqueo del .exe."""
    last_exc: Optional[BaseException] = None
    for _ in range(max(1, attempts)):
        try:
            action()
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay)
        except OSError as exc:
            # WinError 32: el archivo está siendo usado por otro proceso
            if getattr(exc, "winerror", None) in (5, 32):
                last_exc = exc
                time.sleep(delay)
                continue
            raise
    if last_exc is not None:
        raise last_exc


def backup_database(db_path: Path, backup_dir: Optional[Path] = None) -> Optional[Path]:
    """Copia de seguridad consistente de la base SQLite usando la API de backup de sqlite3."""
    if not db_path.is_file():
        return None
    dest_dir = backup_dir or (db_path.parent / "backups")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{db_path.stem}_pre_actualizacion_{stamp}{db_path.suffix or '.sqlite'}"
    src_conn = sqlite3.connect(str(db_path))
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dest


def _child_env() -> dict:
    env = dict(os.environ)
    # Evita que el nuevo ejecutable PyInstaller herede el entorno de extracción de este proceso
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def apply_update(
    source_path: Path,
    target_path: Path,
    expected_sha256: str,
    wait_pid: int = 0,
    timeout_seconds: float = 30.0,
    relaunch: bool = False,
    db_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> int:
    if not source_path.is_file():
        _log(f"[ERROR] No existe el archivo de origen: {source_path}")
        return 2

    # 1. Verificación obligatoria de integridad SHA-256
    actual_hash = compute_sha256(source_path)
    if actual_hash != expected_sha256.strip().lower():
        _log(f"[ERROR] Hash SHA-256 inválido. Esperado: {expected_sha256.lower()}, Actual: {actual_hash}")
        return 3

    # 2. Esperar al cierre del ejecutable principal
    if wait_pid > 0:
        if not wait_for_process_exit(wait_pid, timeout_seconds=timeout_seconds):
            _log(f"[ERROR] El proceso {wait_pid} no se cerró a tiempo.")
            return 4

    # 3. Copia de seguridad de la base de datos
    if db_path is not None:
        try:
            dest = backup_database(db_path, backup_dir)
            if dest:
                _log(f"[OK] Copia de seguridad de la base de datos: {dest}")
        except Exception as exc:
            _log(f"[ERROR] No se pudo copiar la base de datos ({exc}). Se cancela la actualización.")
            return 6

    # 4. Copia .bak, renombrado a .old y reemplazo con reintentos
    backup_path = target_path.with_suffix(target_path.suffix + ".bak")
    old_path = target_path.with_suffix(target_path.suffix + ".old")
    moved_to_old = False
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if old_path.exists():
            try:
                old_path.unlink()
            except OSError:
                pass
        if target_path.exists():
            shutil.copy2(str(target_path), str(backup_path))
            _retry(lambda: os.replace(str(target_path), str(old_path)))
            moved_to_old = True

        _retry(lambda: shutil.copy2(str(source_path), str(target_path)))
        try:
            os.chmod(target_path, 0o755)
        except OSError:
            pass
    except Exception as exc:
        _log(f"[ERROR] Fallo reemplazando ejecutable ({exc}). Restaurando copia previa.")
        try:
            if moved_to_old and old_path.exists():
                os.replace(str(old_path), str(target_path))
            elif backup_path.exists() and not target_path.exists():
                shutil.copy2(str(backup_path), str(target_path))
        except Exception as restore_exc:
            _log(f"[ERROR] No se pudo restaurar el ejecutable anterior: {restore_exc}")
        return 5

    # El .old puede seguir bloqueado unos segundos; si no se puede borrar se limpiará en la próxima
    if moved_to_old:
        try:
            old_path.unlink()
        except OSError:
            pass

    # 5. Relanzar la aplicación si se solicitó
    if relaunch:
        flags = _NO_WINDOW_FLAGS
        if sys.platform == "win32":
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [str(target_path)],
            close_fds=True,
            env=_child_env(),
            cwd=str(target_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Actualizador independiente de Archivero")
    parser.add_argument("--source", required=True, help="Ruta del nuevo ejecutable descargado")
    parser.add_argument("--target", required=True, help="Ruta del ejecutable actual a reemplazar")
    parser.add_argument("--sha256", required=True, help="Hash SHA-256 esperado (64 caracteres hex)")
    parser.add_argument("--wait-pid", type=int, default=0, help="PID del proceso principal a esperar")
    parser.add_argument("--timeout", type=float, default=60.0, help="Segundos máximos de espera")
    parser.add_argument("--relaunch", action="store_true", help="Relanzar el ejecutable tras actualizar")
    parser.add_argument("--db-path", default="", help="Base de datos SQLite a respaldar antes de actualizar")
    parser.add_argument("--backup-dir", default="", help="Carpeta donde guardar la copia de la base de datos")
    args = parser.parse_args(argv)

    return apply_update(
        source_path=Path(args.source),
        target_path=Path(args.target),
        expected_sha256=args.sha256,
        wait_pid=args.wait_pid,
        timeout_seconds=args.timeout,
        relaunch=args.relaunch,
        db_path=Path(args.db_path) if args.db_path else None,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
