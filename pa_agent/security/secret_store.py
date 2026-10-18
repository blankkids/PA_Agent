"""Secret storage: DPAPI on Windows, Fernet elsewhere.

Public API
----------
SecretStore.encrypt(plaintext: str) -> str
SecretStore.decrypt(ciphertext: str) -> str
mask_secret(s: str) -> str
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path


# ── mask_secret ───────────────────────────────────────────────────────────────

def mask_secret(s: str) -> str:
    """Return s with all but the last 4 characters replaced by '*'.

    If len(s) < 4, return s unchanged (including empty string).
    """
    if len(s) < 4:
        return s
    return "*" * (len(s) - 4) + s[-4:]


# ── Platform-specific backends ────────────────────────────────────────────────

def _encrypt_dpapi(plaintext: str) -> str:
    import win32crypt  # type: ignore[import]
    data = plaintext.encode("utf-8")
    encrypted = win32crypt.CryptProtectData(
        data,
        None,   # description
        None,   # entropy
        None,   # reserved
        None,   # prompt struct
        win32crypt.CRYPTPROTECT_UI_FORBIDDEN,
    )
    return base64.b64encode(encrypted).decode("ascii")


def _decrypt_dpapi(ciphertext: str) -> str:
    import win32crypt  # type: ignore[import]
    try:
        data = base64.b64decode(ciphertext)
        _, decrypted = win32crypt.CryptUnprotectData(
            data,
            None,   # entropy
            None,   # reserved
            None,   # prompt struct
            win32crypt.CRYPTPROTECT_UI_FORBIDDEN,
        )
        return decrypted.decode("utf-8")
    except Exception as exc:
        raise ValueError("invalid ciphertext") from exc


_FERNET_KEY_PATH = Path.home() / ".pa_agent" / "secret.key"


def _get_fernet():
    from cryptography.fernet import Fernet  # type: ignore[import]
    if _FERNET_KEY_PATH.exists():
        key = _FERNET_KEY_PATH.read_bytes()
    else:
        key = Fernet.generate_key()
        _FERNET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FERNET_KEY_PATH.write_bytes(key)
    return Fernet(key)


def _encrypt_fernet(plaintext: str) -> str:
    f = _get_fernet()
    token = f.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(token).decode("ascii")


def _decrypt_fernet(ciphertext: str) -> str:
    from cryptography.fernet import InvalidToken  # type: ignore[import]
    try:
        f = _get_fernet()
        token = base64.b64decode(ciphertext)
        return f.decrypt(token).decode("utf-8")
    except (InvalidToken, Exception) as exc:
        raise ValueError("invalid ciphertext") from exc


# ── Public SecretStore class ──────────────────────────────────────────────────

class SecretStore:
    """Thin wrapper that dispatches to the platform-appropriate backend."""

    @staticmethod
    def encrypt(plaintext: str) -> str:
        """Encrypt *plaintext* and return a base64 ciphertext string."""
        if not plaintext:
            return ""
        if sys.platform == "win32":
            try:
                return _encrypt_dpapi(plaintext)
            except Exception:  # noqa: BLE001
                # DPAPI unavailable (e.g. pywin32 not installed) — fall back to Fernet
                return _encrypt_fernet(plaintext)
        return _encrypt_fernet(plaintext)

    @staticmethod
    def decrypt(ciphertext: str) -> str:
        """Decrypt *ciphertext* and return the original plaintext string.

        Raises ValueError on any decryption failure.
        """
        if not ciphertext:
            return ""
        if sys.platform == "win32":
            try:
                return _decrypt_dpapi(ciphertext)
            except Exception:  # noqa: BLE001
                # May be Fernet-encrypted (fallback from a previous session)
                try:
                    return _decrypt_fernet(ciphertext)
                except Exception as exc2:
                    raise ValueError("invalid ciphertext") from exc2
        return _decrypt_fernet(ciphertext)
