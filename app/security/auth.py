import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

from app.models import AuthSession


def private_write(path: Path, value: str, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC), 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
    os.chmod(path, 0o600)


def ensure_key(settings):
    if not settings.key_path.exists():
        if settings.production:
            raise ValueError("Encryption key missing")
        try:
            private_write(settings.key_path, Fernet.generate_key().decode(), exclusive=True)
        except FileExistsError:
            pass
    key = settings.key_path.read_text().strip()
    Fernet(key.encode())  # Fail startup on an empty or malformed key.
    return key


def password_hash(password: str, salt: str | None = None) -> dict:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {"salt": salt, "hash": digest}


def set_owner_password(settings, password):
    private_write(settings.owner_path, json.dumps(password_hash(password)))


def verify_password(settings, password: str) -> bool:
    try:
        data = json.loads(settings.owner_path.read_text())
        actual = password_hash(password, data["salt"])
        return hmac.compare_digest(actual["hash"], data["hash"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def new_session(db):
    token = secrets.token_urlsafe(40)
    csrf = secrets.token_urlsafe(32)
    with db.write() as session:
        session.add(AuthSession(id=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=(datetime.now(UTC) + timedelta(hours=8)).isoformat(), data={"csrf": csrf}))
    return token, csrf


def get_session(db, token):
    if not token or len(token) > 200:
        return None
    with db.read() as session:
        record = session.get(AuthSession, hashlib.sha256(token.encode()).hexdigest())
        if record and record.expires_at > datetime.now(UTC).isoformat():
            return record
    return None
