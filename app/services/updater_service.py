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

from app.config import APP_VERSION, BASE_DIR, DEFAULT_BACKUP_DIR, DEFAULT_DB_PATH, DEFAULT_STAGING_DIR

# Nombres exactos de los ejecutables publicados en cada release
WINDOWS_ASSET_NAME = "Archivero.exe"
MAC_ASSET_NAME = "Archivero"
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

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


def parse_sha256sums(text: str) -> Dict[str, str]:
    """Parsea un SHA256SUMS.txt ('<hash>  <nombre>' o '<hash> *<nombre>') a {nombre: hash}."""
    result: Dict[str, str] = {}
    for line in (text or "").lstrip("\ufeff").splitlines():
        m = re.match(r"^\s*([a-fA-F0-9]{64})\s+\*?(.+?)\s*$", line)
        if m:
            result[m.group(2)] = m.group(1).lower()
    return result


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
            "User-Agent": f"Archivero/{self.current_version}",
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
        expected_hash = ""
        if asset_name:
            expected_hash = self._fetch_expected_hash_from_sums(assets, asset_name) or ""
            if not expected_hash:
                expected_hash = extract_sha256_from_release_body(body, asset_name) or ""

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

    def _fetch_expected_hash_from_sums(self, assets: list[Dict[str, Any]], asset_name: str) -> Optional[str]:
        """Descarga los SHA256SUMS*.txt publicados en la release y devuelve el hash del asset."""
        sums_assets = [
            a
            for a in assets
            if re.fullmatch(r"sha256sums.*\.txt", (a.get("name") or "").lower())
        ]
        # SHA256SUMS.txt primero
        sums_assets.sort(key=lambda a: 0 if (a.get("name") or "").lower() == "sha256sums.txt" else 1)
        for a in sums_assets:
            url = a.get("browser_download_url") or ""
            if not url:
                continue
            try:
                text = self._download_text(url)
            except Exception as exc:
                logger.warning("No se pudo descargar %s: %s", a.get("name"), exc)
                continue
            found = parse_sha256sums(text).get(asset_name)
            if found:
                return found
        return None

    def _download_text(self, url: str) -> str:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.content.decode("utf-8-sig", errors="replace")

    def download_and_verify_update(self, download_url: str, expected_sha256: str, filename: str = "update.bin") -> Path:
        """
        Descarga el paquete de actualización en la carpeta temporal y verifica obligatoriamente su hash SHA-256.
        Si el hash no coincide o no está presente, borra el archivo y lanza ValueError.
        """
        if not expected_sha256 or len(expected_sha256.strip()) != 64:
            raise ValueError("Verificación de integridad obligatoria: falta el hash SHA-256 de la release.")

        self.staging_dir.mkdir(parents=True, exist_ok=True)
        dest_file = self.staging_dir / filename

        headers = self._headers()
        headers["Accept"] = "application/octet-stream"
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            with client.stream("GET", download_url, headers=headers) as resp:
                resp.raise_for_status()
                with dest_file.open("wb") as fh:
                    for chunk in resp.iter_bytes(65536):
                        fh.write(chunk)

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
        Lanza el actualizador independiente para que espere al cierre de este proceso,
        verifique el hash, haga copia de la base de datos, reemplace el ejecutable y
        relance la aplicación (Sección 11).
        - Empaquetado (PyInstaller): usa ArchiveroUpdater(.exe) situado junto al ejecutable principal.
        - Desde código fuente: usa `python updater.py`.
        """
        pid = current_pid if current_pid is not None else os.getpid()
        common_args = [
            "--source",
            str(downloaded_file),
            "--target",
            str(target_executable),
            "--sha256",
            expected_sha256.strip().lower(),
            "--wait-pid",
            str(pid),
            "--db-path",
            str(DEFAULT_DB_PATH),
            "--backup-dir",
            str(DEFAULT_BACKUP_DIR),
        ]
        env = dict(os.environ)
        if getattr(sys, "frozen", False):
            updater_name = "ArchiveroUpdater.exe" if sys.platform == "win32" else "ArchiveroUpdater"
            updater_exe = Path(sys.executable).resolve().parent / updater_name
            if not updater_exe.is_file():
                raise FileNotFoundError(
                    f"No se encuentra {updater_name} junto a {Path(sys.executable).name}. "
                    "Descarga de nuevo el paquete completo desde GitHub Releases."
                )
            cmd = [str(updater_exe), *common_args, "--relaunch"]
            # El actualizador es otro ejecutable PyInstaller: que no herede nuestro entorno de extracción
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        else:
            cmd = [sys.executable, str(BASE_DIR / "updater.py"), *common_args]

        flags = _NO_WINDOW_FLAGS
        if sys.platform == "win32":
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            cmd,
            close_fds=True,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    @staticmethod
    def _select_platform_asset(
        assets: list[Dict[str, Any]], system_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Elige el ejecutable exacto de la plataforma: 'Archivero.exe' en Windows y 'Archivero'
        en macOS (nunca el .zip ni el actualizador). En otras plataformas no hay ejecutable.
        """
        if not assets:
            return None
        sys_name = (system_name or platform.system()).lower()
        wanted = {"windows": WINDOWS_ASSET_NAME, "darwin": MAC_ASSET_NAME}.get(sys_name)
        if not wanted:
            return None
        for a in assets:
            if (a.get("name") or "") == wanted:
                return a
        return None
