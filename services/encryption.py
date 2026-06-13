"""Fernet encryption for Pager credentials stored in DB."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class Secrets:
    def __init__(self, key: str) -> None:
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Cannot decrypt stored secret") from exc
