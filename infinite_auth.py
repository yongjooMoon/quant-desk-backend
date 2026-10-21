"""infinite_auth.py - scrypt password hashing for the infinite-buying cycles (stdlib only, so local scripts can use it without the web stack)."""
import base64
import hashlib
import hmac
import os
from typing import Optional

_SCRYPT = dict(n=2 ** 14, r=8, p=1)


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT, dklen=32)
    return "scrypt${n}${r}${p}${s}${h}".format(**_SCRYPT, s=base64.b64encode(salt).decode(), h=base64.b64encode(h).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, s, h = stored.split("$")
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(s), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False
