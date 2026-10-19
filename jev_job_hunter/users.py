"""Invite-only users for `jjh serve`: one secret link per person, a signed session cookie, no passwords.

data/users.json holds each user's id, name and a SHA-256 of their invite token (never the token).
Each user's files live in data/users/<id>/ laid out like the repo root (config/, state/, results/),
so load_profile, WatchState and run_cycle work on it unchanged. The CLI writes users.json and the
server only reads it, re-reading on every request so an invite or revoke takes effect at once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SESSION_DAYS = 90
_ID = re.compile(r"^[0-9a-f]{12}$")


@dataclass
class User:
    id: str
    name: str
    token_hash: str
    created: str
    revoked: bool = False


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Users:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "users.json"

    def _load(self) -> list[User]:
        if not self.path.is_file():
            return []
        return [User(**u) for u in json.loads(self.path.read_text(encoding="utf-8"))]

    def _save(self, users: list[User]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(u) for u in users], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> list[User]:
        return self._load()

    def active(self) -> list[User]:
        return [u for u in self._load() if not u.revoked]

    def get(self, uid: str) -> User | None:
        return next((u for u in self._load() if u.id == uid), None)

    def find(self, name: str) -> User | None:
        key = name.strip().lower()
        return next((u for u in self._load() if u.name.lower() == key), None)

    def invite(self, name: str) -> tuple[User, str]:
        """New user, or a fresh token for an existing one (the old link and its sessions stop working)."""
        name = name.strip()
        if not name:
            raise ValueError("name is empty")
        token = secrets.token_urlsafe(24)
        users = self._load()
        user = next((u for u in users if u.name.lower() == name.lower()), None)
        if user is None:
            user = User(id=secrets.token_hex(6), name=name, token_hash=_hash(token),
                        created=time.strftime("%Y-%m-%d %H:%M"))
            users.append(user)
        else:
            user.token_hash, user.revoked = _hash(token), False
        self._save(users)
        return user, token

    def revoke(self, name: str, delete: bool = False) -> User:
        users = self._load()
        user = next((u for u in users if u.name.lower() == name.strip().lower()), None)
        if user is None:
            raise KeyError(name)
        if delete:
            users.remove(user)
            shutil.rmtree(self.dir(user.id), ignore_errors=True)
        else:
            user.revoked = True
        self._save(users)
        return user

    def by_token(self, token: str) -> User | None:
        h = _hash(token or "")
        return next((u for u in self._load() if not u.revoked and hmac.compare_digest(u.token_hash, h)), None)

    def dir(self, uid: str) -> Path:
        if not _ID.match(uid):  # ids are only ever our own hex; never let one walk the filesystem
            raise ValueError(f"bad user id {uid!r}")
        return self.data_dir / "users" / uid


def secret_key(data_dir: Path) -> bytes:
    """SECRET_KEY from the environment, else one generated once into data/.secret (local runs)."""
    env = (os.environ.get("SECRET_KEY") or "").strip()
    if env:
        return env.encode()
    path = Path(data_dir) / ".secret"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip().encode()


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session(key: bytes, user: User, now: float | None = None) -> str:
    """uid.issued.sig, bound to the user's current token so re-inviting logs out old sessions."""
    payload = f"{user.id}.{int(now or time.time())}.{user.token_hash[:16]}"
    return f"{payload}.{_sign(key, payload)}"


def read_session(key: bytes, users: Users, cookie: str, now: float | None = None) -> User | None:
    try:
        uid, issued, bind, sig = (cookie or "").split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(key, f"{uid}.{issued}.{bind}")):
        return None
    if not issued.isdigit() or (now or time.time()) - int(issued) > SESSION_DAYS * 86400:
        return None
    user = users.get(uid)
    if user is None or user.revoked or user.token_hash[:16] != bind:
        return None
    return user
