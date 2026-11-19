"""Encrypted SQLite storage for what AFAS OAuth 2.0 login has to remember: MCP clients, pending logins and tokens."""

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

IN_MEMORY = ':memory:'
OWNER_ONLY = 0o600
SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value BLOB NOT NULL,
    expires_at REAL NOT NULL,
    grant_id TEXT,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS records_expiry ON records (expires_at);
CREATE INDEX IF NOT EXISTS records_grant ON records (grant_id);
"""


class Kind(StrEnum):
    """The kinds of record the store holds."""

    CLIENT = 'client'
    CONSENT = 'consent'
    LOGIN = 'login'
    CODE = 'code'
    ACCESS = 'access'
    REFRESH = 'refresh'
    GRANT = 'grant'


TOKEN_KINDS = (Kind.ACCESS, Kind.REFRESH)


def fingerprint(key: str) -> str:
    """Return the SHA-256 of a lookup key, so the database never holds a usable code or token.

    Args:
        key: The secret or identifier a record is looked up by.

    Returns:
        The hexadecimal digest.
    """
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


class Store:
    """Expiring records in one SQLite file, with keys hashed and values encrypted.

    Every record is addressed by a kind and a key and disappears at its expiry time. Records that belong to one
    AFAS login share a grant id, so the whole login can be removed at once. The calls are synchronous: they are
    single-row statements against a local file and return in well under a millisecond.
    """

    def __init__(self, path: str, encryption_key: str, clock: Callable[[], float] = time.time) -> None:
        """Open the database, creating the file (readable by its owner only) and the table when they are missing.

        Args:
            path: The SQLite file, or ``:memory:`` for a store that lives as long as this object.
            encryption_key: The Fernet key that encrypts every stored value.
            clock: Returns the current time in seconds; replaceable in tests.
        """
        self._logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self._clock = clock
        self._fernet = Fernet(encryption_key.encode('ascii'))
        if path != IN_MEMORY:
            Path(path).touch(mode=OWNER_ONLY, exist_ok=True)
        self._database = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._database.executescript(SCHEMA)

    def close(self) -> None:
        """Close the database."""
        self._database.close()

    def put(
        self,
        kind: Kind,
        key: str,
        value: dict[str, Any],
        *,
        expires_at: float,
        grant_id: str | None = None,
    ) -> None:
        """Store a record, replacing any record with the same kind and key, and drop the records that expired.

        Args:
            kind: The kind of record.
            key: The secret or identifier to find it by.
            value: The JSON-serialisable content.
            expires_at: When the record stops existing, in seconds since the epoch.
            grant_id: The AFAS login the record belongs to, if any.
        """
        self._database.execute('DELETE FROM records WHERE expires_at <= ?', (self._clock(),))
        encrypted = self._fernet.encrypt(json.dumps(value).encode('utf-8'))
        self._database.execute(
            'INSERT OR REPLACE INTO records (kind, key, value, expires_at, grant_id) VALUES (?, ?, ?, ?, ?)',
            (kind.value, fingerprint(key), encrypted, expires_at, grant_id),
        )

    def get(self, kind: Kind, key: str) -> dict[str, Any] | None:
        """Return a record that has not expired.

        Args:
            kind: The kind of record.
            key: The secret or identifier it was stored under.

        Returns:
            The stored content, or None when there is no such record.
        """
        row = self._database.execute(
            'SELECT value FROM records WHERE kind = ? AND key = ? AND expires_at > ?',
            (kind.value, fingerprint(key), self._clock()),
        ).fetchone()
        return self.decrypted(row)

    def pop(self, kind: Kind, key: str) -> dict[str, Any] | None:
        """Remove a record and return it, so a code or one-time token can be used exactly once.

        Args:
            kind: The kind of record.
            key: The secret or identifier it was stored under.

        Returns:
            The stored content, or None when there was no such record or it had expired.
        """
        row = self._database.execute(
            'DELETE FROM records WHERE kind = ? AND key = ? AND expires_at > ? RETURNING value',
            (kind.value, fingerprint(key), self._clock()),
        ).fetchone()
        return self.decrypted(row)

    def delete_tokens(self, grant_id: str) -> None:
        """Remove the MCP access and refresh tokens of a login, keeping the login itself.

        Args:
            grant_id: The AFAS login whose tokens are replaced.
        """
        self._database.execute(
            'DELETE FROM records WHERE grant_id = ? AND kind IN (?, ?)',
            (grant_id, *(kind.value for kind in TOKEN_KINDS)),
        )

    def delete_grant(self, grant_id: str) -> None:
        """Remove an AFAS login and every code and token issued for it.

        Args:
            grant_id: The AFAS login to forget.
        """
        self._database.execute('DELETE FROM records WHERE grant_id = ?', (grant_id,))

    def count(self, kind: Kind) -> int:
        """Return how many records of a kind have not expired.

        Args:
            kind: The kind of record to count.

        Returns:
            The number of live records.
        """
        row = self._database.execute(
            'SELECT COUNT(*) FROM records WHERE kind = ? AND expires_at > ?', (kind.value, self._clock())
        ).fetchone()
        return int(row[0])

    def decrypted(self, row: tuple[bytes] | None) -> dict[str, Any] | None:
        """Decrypt the value column of a row.

        Args:
            row: A single-column row holding the encrypted value, or None when nothing matched.

        Returns:
            The stored content, or None when there is no row or it was written with another encryption key.
        """
        if row is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(row[0]))
        except InvalidToken:
            self._logger.warning('ignoring a record that was encrypted with another key')
            return None
