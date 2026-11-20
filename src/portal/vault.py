"""Cifra os textos completos antes de publicar (o site é público; o texto não pode ser).

AES-256-GCM com chave derivada da senha READER_PASSWORD por PBKDF2-SHA256. O navegador refaz a
mesma derivação com WebCrypto depois que a senha é digitada uma vez. Sem a senha, o arquivo
publicado é ilegível.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 310_000
CHECK = "portal-ok"  # cifrado junto, para a página saber se a senha digitada está certa


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def article_id(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]


def seal(password: str, articles: dict[str, dict]) -> dict:
    """articles: {id: {...}} -> objeto JSON publicável com tudo cifrado."""
    salt = os.urandom(16)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(
        password.encode("utf-8"))
    aes = AESGCM(key)

    def enc(obj) -> dict:
        nonce = os.urandom(12)
        ct = aes.encrypt(nonce, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
        return {"n": _b64(nonce), "c": _b64(ct)}

    return {"v": 1, "salt": _b64(salt), "iter": ITERATIONS, "check": enc(CHECK),
            "a": {aid: enc(a) for aid, a in articles.items()}}


def open_sealed(password: str, sealed: dict) -> dict[str, dict]:
    """Inverso de seal (usado nos testes)."""
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(sealed["salt"]),
                     iterations=sealed["iter"]).derive(password.encode("utf-8"))
    aes = AESGCM(key)

    def dec(x):
        return json.loads(aes.decrypt(base64.b64decode(x["n"]), base64.b64decode(x["c"]), None))

    assert dec(sealed["check"]) == CHECK
    return {aid: dec(x) for aid, x in sealed["a"].items()}
