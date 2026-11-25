"""DPAPI-backed credential storage and secret protection (BLUEPRINT 5c, 5h).

Stores API keys for Economy engines (DeepSeek, Kimi, GLM, MiniMax, etc.) securely using
Windows Data Protection API (DPAPI) for the current user. Provides a safe interface
with an in-memory fake for CI environments. Automatically registers stored secrets for redaction.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class CredentialStore(Protocol):
    """Protocol for secure credential storage."""

    def set_secret(self, key_name: str, secret: str) -> None:
        """Store a secret securely."""
        ...

    def get_secret(self, key_name: str) -> str | None:
        """Retrieve a stored secret, or None if not found."""
        ...

    def delete_secret(self, key_name: str) -> None:
        """Remove a stored secret."""
        ...

    def list_keys(self) -> list[str]:
        """List all stored secret keys (not the values)."""
        ...


class MemoryCredentialStore:
    """In-memory credential store for tests and CI environments."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def set_secret(self, key_name: str, secret: str) -> None:
        if secret:
            self._secrets[key_name] = secret
            _register_secret_for_redaction(secret)
        else:
            self.delete_secret(key_name)

    def get_secret(self, key_name: str) -> str | None:
        return self._secrets.get(key_name)

    def delete_secret(self, key_name: str) -> None:
        self._secrets.pop(key_name, None)

    def list_keys(self) -> list[str]:
        return sorted(list(self._secrets.keys()))


class WindowsDPAPICredentialStore:
    """Stores secrets encrypted with Windows DPAPI (CryptProtectData)."""

    def __init__(self, storage_path: Path | None = None) -> None:
        if storage_path is not None:
            self._path = storage_path
        else:
            local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            base_dir = Path(local_app_data) / "NightShift"
            base_dir.mkdir(parents=True, exist_ok=True)
            self._path = base_dir / "credentials.dpapi"

    def _encrypt(self, plaintext: str) -> str:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        data_bytes = plaintext.encode("utf-8")
        c_bytes = (ctypes.c_char * len(data_bytes))(*data_bytes)
        inp = DATA_BLOB(len(data_bytes), c_bytes)
        out = DATA_BLOB()

        crypt32 = getattr(ctypes.windll, "crypt32", None)
        if crypt32 is None or not crypt32.CryptProtectData(
            ctypes.byref(inp), "NightShift Secret", None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptProtectData failed to protect secret.")

        encrypted = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return base64.b64encode(encrypted).decode("ascii")

    def _decrypt(self, ciphertext: str) -> str:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        enc_bytes = base64.b64decode(ciphertext.encode("ascii"))
        c_bytes = (ctypes.c_char * len(enc_bytes))(*enc_bytes)
        inp = DATA_BLOB(len(enc_bytes), c_bytes)
        out = DATA_BLOB()

        crypt32 = getattr(ctypes.windll, "crypt32", None)
        if crypt32 is None or not crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptUnprotectData failed to unprotect secret.")

        decrypted = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return decrypted.decode("utf-8")

    def _load_dict(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_dict(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def set_secret(self, key_name: str, secret: str) -> None:
        if not secret:
            self.delete_secret(key_name)
            return
        data = self._load_dict()
        encrypted = self._encrypt(secret)
        data[key_name] = encrypted
        self._save_dict(data)
        _register_secret_for_redaction(secret)

    def get_secret(self, key_name: str) -> str | None:
        data = self._load_dict()
        encrypted = data.get(key_name)
        if not encrypted:
            return None
        try:
            decrypted = self._decrypt(encrypted)
            _register_secret_for_redaction(decrypted)
            return decrypted
        except Exception:
            return None

    def delete_secret(self, key_name: str) -> None:
        data = self._load_dict()
        if key_name in data:
            data.pop(key_name)
            self._save_dict(data)

    def list_keys(self) -> list[str]:
        return sorted(list(self._load_dict().keys()))


_ACTIVE_STORE: CredentialStore | None = None
_KNOWN_SECRETS: set[str] = set()


def _register_secret_for_redaction(secret: str) -> None:
    if secret and len(secret) >= 4:
        _KNOWN_SECRETS.add(secret)


def get_known_secrets() -> list[str]:
    return sorted(list(_KNOWN_SECRETS), key=len, reverse=True)


def redact_known_secrets(text: str) -> str:
    """Redact all stored API keys from text."""
    redacted = text
    for sec in get_known_secrets():
        redacted = redacted.replace(sec, "[REDACTED_SECRET]")
    return redacted


def get_credential_store(*, fake_on_ci: bool = True) -> CredentialStore:
    """Return active credential store, defaulting to WindowsDPAPICredentialStore on Windows."""
    global _ACTIVE_STORE
    if _ACTIVE_STORE is not None:
        return _ACTIVE_STORE

    force_fake = (
        fake_on_ci
        and (
            os.environ.get("CI") == "true"
            or os.environ.get("NIGHTSHIFT_FAKE_DPAPI") == "1"
            or sys.platform != "win32"
        )
    )

    if force_fake or sys.platform != "win32":
        _ACTIVE_STORE = MemoryCredentialStore()
    else:
        try:
            import ctypes

            if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "crypt32"):
                _ACTIVE_STORE = WindowsDPAPICredentialStore()
            else:
                _ACTIVE_STORE = MemoryCredentialStore()
        except Exception:
            _ACTIVE_STORE = MemoryCredentialStore()

    return _ACTIVE_STORE


def set_credential_store(store: CredentialStore | None) -> None:
    global _ACTIVE_STORE
    _ACTIVE_STORE = store
