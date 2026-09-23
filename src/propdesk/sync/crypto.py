"""Symmetric encryption for the one secret this system now stores: an MT5
investor (read-only) password.

The key lives only in the server's environment (`PROPDESK_SECRET_KEY`), never
in the database, never in git. Losing the key means losing the ability to
decrypt stored credentials — that's the correct failure mode; it must not be
recoverable from the database alone.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = os.environ.get("PROPDESK_SECRET_KEY")
    if not key:
        raise SecretKeyMissing(
            "PROPDESK_SECRET_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and set it in the environment before storing any credential."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Could not decrypt credential — wrong key or corrupted data") from exc
