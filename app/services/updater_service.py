"""Servicio de comprobación y descarga de actualizaciones desde GitHub Releases con verificación SHA-256."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

from app.config import APP_VERSION, BASE_DIR, DEFAULT_STAGING_DIR

logger = logging.getLogger(__name__)


def parse_semver(version_str: str) -> Tuple[int, int, int]:
    """Convierte una cadena de versión ('v0.2.1' o '0.2.1') en tupla comparable (0, 2, 1)."""
    cleaned = version_str.strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    nums = [int(p) for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def is_newer_version(remote_version: str, current_version: str = APP_VERSION) -> bool:
    return parse_semver(remote_version) > parse_semver(current_version)


def compute_file_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def verify_sha256_checksum(file_path: Path, expected_sha256: str) -> bool:
    """Verificación de integridad obligatoria antes de instalar una actualización (Sección 11)."""
    if not expected_sha256 or len(expected_sha256.strip()) != 64:
        return False
    actual = compute_file_sha256(file_path)
    return actual == expected_sha256.strip().lower()


def extract_sha256_from_release_body(release_body: str, asset_name: str) -> Optional[str]:
    """Extrae el hash SHA-256 publicado en las notas de la release para el ejecutable correspondiente."""
    if not release_body:
        return None
    # Formato estándar: "<sha256_64_hex>  <asset_name>" o "<asset_name>: <sha256_64_hex>"
    escaped = re.escape(asset_name)
    m1 = re.search(rf"\b([a-fA-F0-9]{{64}})\s+\*?{escaped}\b", release_body)
    if m1:
        return m1.group(1).lower()
    m2 = re.search(rf"{escaped}\s*[:=-]?\s*\b([a-fA-F0-9]{{64}})\b", release_body)
    if m2:
        return m2.group(1).lower()
    # Si solo hay un hash SHA256 en el cuerpo de la release
    all_hashes = re.findall(r"\b([a-fA-F0-9]{64})\b", release_body)
    if len(all_hashes) == 1:
        return all_hashes[0].lower()
    return None


class UpdaterService:
    """Gestiona la consulta a GitHub Releases, descarga verificada y lanzamiento del actualizador independiente."""

    def __init__(
        self,
        github_repo: str = "abel-eiras/archivero",
        github_token: Optional[str] = None,
        current_version: str = APP_VERSION,
        staging_dir: Optional[Path] = None,
    ) -> None:
        self.github_repo = github_repo.strip("/")
        self.github_token = (github_token or "").strip()
        self.current_version = current_version
        self.staging_dir = Path(staging_dir or DEFAULT_STAGING_DIR)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"FarmaFacturas/{self.current_version}",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    def check_for_updates(self) -> Dict[str, Any]:
        """Consulta la última release en GitHub y comprueba si es superior a la versión instalada."""
        url = f"https://api.github.com/repos/{self.github_repo}/releases/latest"
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, headers=self._headers())
                if resp.status_code == 404:
                    return {
                        "hay_actualizacion": False,
                        "version_actual": self.current_version,
                        "version_remota": self.current_version,
                        "mensaje": "No hay releases publicadas aún en el repositorio configurado.",
                    }
                resp.raise_for_status()
                release = resp.json()
        except Exception as exc:
            return {
                "hay_actualizacion": False,
                "version_actual": self.current_version,
                "error": f"No se pudo consultar GitHub Releases: {exc}",
            }

        tag_name = release.get("tag_name") or release.get("name") or self.current_version
        body = release.get("body") or ""
        assets = release.get("assets") or []

        asset = self._select_platform_asset(assets)
        asset_name = asset.get("name", "") if asset else ""
        download_url = asset.get("browser_download_url", "") if asset else ""
        expected_hash = extract_sha256_from_release_body(body, asset_name)

        newer = is_newer_version(tag_name, self.current_version)
        return {
            "hay_actualizacion": newer,
            "version_actual": self.current_version,
            "version_remota": tag_name.lstrip("vV"),
            "nombre_release": release.get("name") or tag_name,
            "notas": body,
            "asset_name": asset_name,
            "download_url": download_url,
            "sha256_esperado": expected_hash or "",
            "publicado_en": release.get("published_at") or "",
        }

    def download_and_verify_update(self, download_url: str, expected_sha256: str, filename: str = "update.bin") -> Path:
        """
        Descarga el paquete de actualización en la carpeta temporal y verifica obligatoriamente su hash SHA-256.
        Si el hash no coincide o no está presente, borra el archivo y lanza ValueError.
        """
        if not expected_sha256 or len(expected_sha256.strip()) != 64:
            raise ValueError("Verificación de integridad obligatoria: falta el hash SHA-256 de la release.")

        self.staging_dir.mkdir(parents=True, exist_ok=True)
        dest_file = self.staging_dir / filename

        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            resp = client.get(download_url, headers=self._headers())
            resp.raise_for_status()
            dest_file.write_bytes(resp.content)

        if not verify_sha256_checksum(dest_file, expected_sha256):
            actual = compute_file_sha256(dest_file)
            dest_file.unlink(missing_ok=True)
            raise ValueError(
                f"Fallo de integridad SHA-256: esperado {expected_sha256.lower()}, calculado {actual}."
            )

        return dest_file

    def launch_standalone_updater(
        self,
        downloaded_file: Path,
        target_executable: Path,
        expected_sha256: str,
        current_pid: Optional[int] = None,
    ) -> subprocess.Popen:
        """
        Lanza el actualizador independiente (updater.py) para que espere al cierre de este proceso,
        verifique el hash, reemplace el ejecutable y relance la aplicación (Sección 11).
        """
        updater_script = BASE_DIR / "updater.py"
        pid = current_pid if current_pid is not None else os.getpid()
        cmd = [
            sys.executable,
            str(updater_script),
            "--source",
            str(downloaded_file),
            "--target",
            str(target_executable),
            "--sha256",
            expected_sha256.strip().lower(),
            "--wait-pid",
            str(pid),
        ]
        return subprocess.Popen(cmd, close_fds=True)

    @staticmethod
    def _select_platform_asset(assets: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not assets:
            return None
        sys_name = platform.system().lower()
        for a in assets:
            name = (a.get("name") or "").lower()
            if sys_name == "windows" and (name.endswith(".exe") or "win" in name):
                return a
            if sys_name == "darwin" and ("mac" in name or "darwin" in name or name.endswith(".dmg")):
                return a
            if sys_name == "linux" and ("linux" in name or name.endswith(".appimage") or name.endswith(".tar.gz")):
                return a
        return assets[0]
