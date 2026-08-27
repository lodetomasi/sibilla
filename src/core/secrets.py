"""Gestione secret: cifratura at-rest e secrets manager pluggabile (sez. 52).

- API key cifrate con Fernet usando ATS_SECRET_KEY;
- gli agenti LLM non hanno alcun tool per leggere secret (sez. 80);
- in produzione si puo sostituire il provider con AWS Secrets Manager/Vault.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from core.errors import ConfigError


class SecretProvider(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...


def _derive_key(material: str) -> bytes:
    """Accetta sia una Fernet key valida sia una passphrase arbitraria."""
    raw = material.encode()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except Exception:  # noqa: BLE001 - passphrase non base64
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


class SecretBox:
    """Cifra/decifra valori con Fernet."""

    def __init__(self, key_material: str | None = None):
        material = key_material or os.getenv("ATS_SECRET_KEY") or ""
        if not material:
            raise ConfigError(
                "ATS_SECRET_KEY mancante: generarla con `make gen-key` prima di cifrare secret"
            )
        self._fernet = Fernet(_derive_key(material))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ConfigError("secret non decifrabile: chiave errata o dato corrotto") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()


class EnvSecretProvider:
    """Provider di default: variabili d'ambiente, opzionalmente cifrate.

    Un valore che inizia con `enc:` viene decifrato con SecretBox.
    """

    prefix = "ATS_"
    enc_marker = "enc:"

    def __init__(self, box: SecretBox | None = None):
        self._box = box

    def get(self, name: str) -> str | None:
        key = name if name.startswith(self.prefix) else f"{self.prefix}{name.upper()}"
        value = os.getenv(key)
        if value is None:
            return None
        if value.startswith(self.enc_marker):
            box = self._box or SecretBox()
            return box.decrypt(value[len(self.enc_marker):])
        return value

    def set(self, name: str, value: str) -> None:
        key = name if name.startswith(self.prefix) else f"{self.prefix}{name.upper()}"
        os.environ[key] = value


_provider: SecretProvider = EnvSecretProvider()


def get_secret_provider() -> SecretProvider:
    return _provider


def set_secret_provider(provider: SecretProvider) -> None:
    global _provider
    _provider = provider


def get_secret(name: str) -> str | None:
    return _provider.get(name)
