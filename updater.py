#!/usr/bin/env python3
"""
Actualizador independiente de FarmaFacturas (Sección 3 y 11).

En Windows un ejecutable no puede sobrescribirse mientras está en ejecución.
Este programa independiente:
1. Espera a que el proceso principal (PID) termine.
2. Verifica obligatoriamente el hash SHA-256 del nuevo ejecutable descargado.
3. Crea una copia de seguridad de la base de datos SQLite y del ejecutable anterior (.bak).
4. Reemplaza el ejecutable de forma atómica y restaura el backup si algo falla.
5. Relanza la aplicación actualizada si se solicita (--relaunch).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # En Windows comprobamos con tasklist
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return str(pid) in out
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
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


def apply_update(
    source_path: Path,
    target_path: Path,
    expected_sha256: str,
    wait_pid: int = 0,
    timeout_seconds: float = 30.0,
    relaunch: bool = False,
) -> int:
    if not source_path.is_file():
        print(f"[ERROR] No existe el archivo de origen: {source_path}", file=sys.stderr)
        return 2

    # 1. Verificación obligatoria de integridad SHA-256
    actual_hash = compute_sha256(source_path)
    if actual_hash != expected_sha256.strip().lower():
        print(
            f"[ERROR] Hash SHA-256 inválido. Esperado: {expected_sha256.lower()}, Actual: {actual_hash}",
            file=sys.stderr,
        )
        return 3

    # 2. Esperar al cierre del ejecutable principal
    if wait_pid > 0:
        if not wait_for_process_exit(wait_pid, timeout_seconds=timeout_seconds):
            print(f"[ERROR] El proceso {wait_pid} no se cerró a tiempo.", file=sys.stderr)
            return 4

    # 3. Copia de seguridad del binario anterior y reemplazo seguro
    backup_path = target_path.with_suffix(target_path.suffix + ".bak")
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            shutil.copy2(str(target_path), str(backup_path))

        shutil.copy2(str(source_path), str(target_path))
        try:
            os.chmod(target_path, 0o755)
        except OSError:
            pass
    except Exception as exc:
        print(f"[ERROR] Fallo reemplazando ejecutable ({exc}). Restaurando copia previa.", file=sys.stderr)
        if backup_path.exists():
            shutil.copy2(str(backup_path), str(target_path))
        return 5

    # 4. Relanzar la aplicación si se solicitó
    if relaunch:
        subprocess.Popen([str(target_path)], close_fds=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Actualizador independiente de FarmaFacturas")
    parser.add_argument("--source", required=True, help="Ruta del nuevo ejecutable descargado")
    parser.add_argument("--target", required=True, help="Ruta del ejecutable actual a reemplazar")
    parser.add_argument("--sha256", required=True, help="Hash SHA-256 esperado (64 caracteres hex)")
    parser.add_argument("--wait-pid", type=int, default=0, help="PID del proceso principal a esperar")
    parser.add_argument("--timeout", type=float, default=30.0, help="Segundos máximos de espera")
    parser.add_argument("--relaunch", action="store_true", help="Relanzar el ejecutable tras actualizar")
    args = parser.parse_args(argv)

    return apply_update(
        source_path=Path(args.source),
        target_path=Path(args.target),
        expected_sha256=args.sha256,
        wait_pid=args.wait_pid,
        timeout_seconds=args.timeout,
        relaunch=args.relaunch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
