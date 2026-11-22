"""SQLite storage for local accounts and browser login sessions."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


LOGIN_COOKIE = "edis_login"
SESSION_SECONDS = 8 * 60 * 60


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**15, r=8, p=3,
        maxmem=64 * 1024 * 1024, dklen=32,
    )


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class AccountStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY NOT NULL,
                    username TEXT NOT NULL UNIQUE,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_sessions (
                    token_hash BLOB PRIMARY KEY NOT NULL,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    expires_at INTEGER NOT NULL
                );
            """)

    def signup(self, username: str, password: str) -> str | None:
        owner = f"account:{uuid.uuid4().hex}"
        salt = secrets.token_bytes(16)
        password_hash = _password_hash(password, salt)
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute(
                """INSERT INTO accounts (id, username, salt, password_hash)
                   VALUES (?, ?, ?, ?) ON CONFLICT(username) DO NOTHING""",
                (owner, username, salt, password_hash),
            )
            return owner if result.rowcount == 1 else None

    def verify(self, username: str, password: str) -> str | None:
        with closing(sqlite3.connect(self.path)) as db:
            account = db.execute(
                "SELECT id, salt, password_hash FROM accounts WHERE username = ?", (username,),
            ).fetchone()
        owner, salt, expected = account if account else (None, bytes(16), bytes(32))
        return owner if secrets.compare_digest(_password_hash(password, salt), expected) else None

    def start_session(self, owner: str, previous_token: str | None) -> str:
        token = secrets.token_hex(32)
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("DELETE FROM login_sessions WHERE expires_at <= ?", (now,))
            if previous_token:
                db.execute("DELETE FROM login_sessions WHERE token_hash = ?", (_token_hash(previous_token),))
            db.execute(
                "INSERT INTO login_sessions (token_hash, account_id, expires_at) VALUES (?, ?, ?)",
                (_token_hash(token), owner, now + SESSION_SECONDS),
            )
        return token

    def session_owner(self, token: str | None) -> str | None:
        if not token:
            return None
        with closing(sqlite3.connect(self.path)) as db:
            session = db.execute(
                "SELECT account_id FROM login_sessions WHERE token_hash = ? AND expires_at > ?",
                (_token_hash(token), int(time.time())),
            ).fetchone()
        return session[0] if session else None

    def end_session(self, token: str | None) -> None:
        if token:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("DELETE FROM login_sessions WHERE token_hash = ?", (_token_hash(token),))
