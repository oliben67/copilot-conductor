"""
users.py — User store backed by $CONDUCTOR_HOME/users.json.

Passwords are stored as PBKDF2-HMAC-SHA256 salted hashes (stdlib only,
no extra dependency required).

Schema of users.json:
    {
        "<username>": {
            "password_hash": "<hex>",
            "password_salt": "<hex>",
            "active": true
        },
        ...
    }
"""

import hashlib
import json
import logging
import os
import secrets
import threading
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

_PBKDF2_ITERS = 260_000  # NIST recommendation for SHA-256
_LOCK = threading.Lock()


def _users_file() -> str:
    conductor_home = os.environ.get("CONDUCTOR_HOME", "")
    return os.path.join(conductor_home, "users.json") if conductor_home else "users.json"


@dataclass
class UserRecord:
    username: str
    password_hash: str
    password_salt: str
    active: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("username")  # key in the outer mapping, not stored inside
        return d


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return (hex_hash, hex_salt). Generates a fresh salt when *salt* is None."""
    if salt is None:
        salt = secrets.token_bytes(32)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return h.hex(), salt.hex()


def _load(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def create_user(username: str, password: str, active: bool = True, **extra) -> UserRecord:
    """
    Create and persist a new user.

    Raises
    ------
    ValueError
        If a user with the same *username* already exists.
    """
    path = _users_file()
    with _LOCK:
        data = _load(path)
        if username in data:
            raise ValueError(f"User {username!r} already exists")
        pw_hash, pw_salt = _hash_password(password)
        record: dict = {"password_hash": pw_hash, "password_salt": pw_salt, "active": active}
        record.update(extra)
        data[username] = record
        _save(path, data)
        log.info("Created user %r (active=%s)", username, active)
    return UserRecord(username=username, password_hash=pw_hash, password_salt=pw_salt, active=active)


def user_exists(username: str) -> bool:
    data = _load(_users_file())
    return username in data
