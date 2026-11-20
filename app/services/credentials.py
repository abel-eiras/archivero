"""Gestión segura de credenciales (almacén del sistema vía keyring + fallback cifrado)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

import keyring
from cryptography.fernet import Fernet

from app.config import DEFAULT_DATA_DIR, KEYRING_SERVICE_NAME

logger = logging.getLogger(__name__)


class CredentialManager:
    """
    Almacena contraseñas de correo, tokens OAuth y claves de API en el almacén seguro del sistema
    (Windows Credential Manager / macOS Keychain / SecretService) mediante `keyring`.
    Nunca guarda secretos en SQLite ni en archivos de texto plano.
    En entornos sin servicio de keyring activo (contenedores o servidores headless),
    utiliza un almacén binario cifrado con Fernet (AES-128-CBC + HMAC-SHA256).
    """

    def __init__(
        self,
        service_name: str = KEYRING_SERVICE_NAME,
        vault_dir: Optional[Path] = None,
        force_encrypted_fallback: bool = False,
    ) -> None:
        self.service_name = service_name
        self.vault_dir = Path(vault_dir or DEFAULT_DATA_DIR)
        self.force_encrypted_fallback = force_encrypted_fallback
        self._key_path = self.vault_dir / ".vault_key"
        self._vault_path = self.vault_dir / ".credentials.enc"

    def set_secret(self, key: str, secret: str) -> str:
        """Guarda un secreto y devuelve el backend utilizado ('system_keyring' o 'encrypted_vault')."""
        if not secret:
            self.delete_secret(key)
            return "deleted"

        if not self.force_encrypted_fallback:
            try:
                keyring.set_password(self.service_name, key, secret)
                # Verificar lectura
                if keyring.get_password(self.service_name, key) == secret:
                    return "system_keyring"
            except Exception as exc:
                logger.debug("Keyring del sistema no disponible (%s), usando bóveda cifrada local.", exc)
            # Si la escritura en keyring falló (p. ej. Windows Credential Manager > 2560 bytes),
            # puede quedar el valor ANTIGUO allí y get_secret lo devolvería antes que la bóveda.
            try:
                keyring.delete_password(self.service_name, key)
            except Exception:
                pass

        self._write_encrypted_vault_entry(key, secret)
        return "encrypted_vault"

    def get_secret(self, key: str) -> Optional[str]:
        """Recupera un secreto desde el almacén del sistema o la bóveda cifrada."""
        if not self.force_encrypted_fallback:
            try:
                val = keyring.get_password(self.service_name, key)
                if val is not None:
                    return val
            except Exception:
                pass
        return self._read_encrypted_vault_entry(key)

    def delete_secret(self, key: str) -> None:
        """Elimina un secreto del almacén del sistema y de la bóveda cifrada."""
        if not self.force_encrypted_fallback:
            try:
                keyring.delete_password(self.service_name, key)
            except Exception:
                pass
        data = self._load_encrypted_vault()
        if key in data:
            del data[key]
            self._save_encrypted_vault(data)

    def has_secret(self, key: str) -> bool:
        val = self.get_secret(key)
        return bool(val and val.strip())

    # Helpers semánticos para los secretos de la aplicación
    def set_llm_api_key(self, provider: str, api_key: str) -> str:
        return self.set_secret(f"llm_api_key:{provider}", api_key.strip())

    def get_llm_api_key(self, provider: str) -> Optional[str]:
        return self.get_secret(f"llm_api_key:{provider}")

    def set_mail_password(self, email: str, password: str) -> str:
        return self.set_secret(f"mail_password:{email.lower().strip()}", password)

    def get_mail_password(self, email: str) -> Optional[str]:
        return self.get_secret(f"mail_password:{email.lower().strip()}")

    def set_oauth_tokens(self, email: str, token_payload: Dict[str, str]) -> str:
        return self.set_secret(
            f"mail_oauth:{email.lower().strip()}",
            json.dumps(token_payload, ensure_ascii=False),
        )

    def get_oauth_tokens(self, email: str) -> Dict[str, str]:
        raw = self.get_secret(f"mail_oauth:{email.lower().strip()}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def set_gdrive_token(self, token: str) -> str:
        return self.set_secret("gdrive_access_token", token.strip())

    def get_gdrive_token(self) -> Optional[str]:
        return self.get_secret("gdrive_access_token")

    def set_github_token(self, token: str) -> str:
        return self.set_secret("github_release_token", token.strip())

    def get_github_token(self) -> Optional[str]:
        return self.get_secret("github_release_token")

    # =====================================================================
    # Bóveda cifrada Fernet para entornos sin demonio de keyring de escritorio
    # =====================================================================

    def _get_fernet(self) -> Fernet:
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        if not self._key_path.exists():
            key = Fernet.generate_key()
            self._key_path.write_bytes(key)
            try:
                os.chmod(self._key_path, 0o600)
            except OSError:
                pass
        return Fernet(self._key_path.read_bytes().strip())

    def _load_encrypted_vault(self, preparing_write: bool = False) -> Dict[str, str]:
        """
        Carga la bóveda cifrada. Si no se puede descifrar y se va a escribir encima,
        se conserva una copia del archivo ilegible (.corrupt-<fecha>) para no perder
        todos los secretos al guardar uno nuevo.
        """
        if not self._vault_path.exists():
            return {}
        try:
            fernet = self._get_fernet()
            decrypted = fernet.decrypt(self._vault_path.read_bytes())
            data = json.loads(decrypted.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            if not preparing_write:
                logger.error("No se pudo descifrar la bóveda local de credenciales: %s", exc)
                return {}
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self._vault_path.with_name(f"{self._vault_path.name}.corrupt-{stamp}")
            try:
                self._vault_path.replace(backup)
                logger.error(
                    "La bóveda de credenciales no se pudo descifrar (%s). Se ha guardado una copia en %s "
                    "y se creará una bóveda nueva.",
                    exc,
                    backup,
                )
            except OSError as move_exc:
                logger.error("Bóveda ilegible (%s) y no se pudo respaldar (%s).", exc, move_exc)
                raise
            return {}

    def _save_encrypted_vault(self, data: Dict[str, str]) -> None:
        fernet = self._get_fernet()
        encrypted = fernet.encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self._vault_path.write_bytes(encrypted)
        try:
            os.chmod(self._vault_path, 0o600)
        except OSError:
            pass

    def _write_encrypted_vault_entry(self, key: str, secret: str) -> None:
        data = self._load_encrypted_vault(preparing_write=True)
        data[key] = secret
        self._save_encrypted_vault(data)

    def _read_encrypted_vault_entry(self, key: str) -> Optional[str]:
        data = self._load_encrypted_vault()
        return data.get(key)
