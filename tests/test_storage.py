"""Tests for the encrypted token store."""

import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from afas_mcp_server.storage import Kind, Store, fingerprint
from tests.conftest import ENCRYPTION_KEY, OTHER_ENCRYPTION_KEY


class Clock:
    """A clock tests move by hand."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        """Start at a fixed moment."""
        self.now = now

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now


@pytest.fixture
def clock() -> Clock:
    """A clock standing still at an arbitrary moment."""
    return Clock()


@pytest.fixture
def store(clock: Clock) -> Iterator[Store]:
    """An in-memory store on the fake clock."""
    opened = Store(':memory:', ENCRYPTION_KEY, clock)
    yield opened
    opened.close()


def test_get_returns_what_was_put(store: Store, clock: Clock) -> None:
    """A record is found by its kind and key, and only by both."""
    store.put(Kind.CLIENT, 'client-1', {'name': 'Claude'}, expires_at=clock.now + 60)
    assert store.get(Kind.CLIENT, 'client-1') == {'name': 'Claude'}
    assert store.get(Kind.CODE, 'client-1') is None
    assert store.get(Kind.CLIENT, 'client-2') is None


def test_put_replaces_a_record_with_the_same_key(store: Store, clock: Clock) -> None:
    """Storing again under the same key keeps one record with the new content."""
    store.put(Kind.GRANT, 'grant-1', {'access_token': 'old'}, expires_at=clock.now + 60)
    store.put(Kind.GRANT, 'grant-1', {'access_token': 'new'}, expires_at=clock.now + 60)
    assert store.get(Kind.GRANT, 'grant-1') == {'access_token': 'new'}
    assert store.count(Kind.GRANT) == 1


def test_records_expire(store: Store, clock: Clock) -> None:
    """At its expiry time a record is gone for get, pop and count."""
    store.put(Kind.CODE, 'code-1', {'client_id': 'c'}, expires_at=clock.now + 60)
    clock.now += 60
    assert store.get(Kind.CODE, 'code-1') is None
    assert store.pop(Kind.CODE, 'code-1') is None
    assert store.count(Kind.CODE) == 0


def test_pop_returns_a_record_once(store: Store, clock: Clock) -> None:
    """Codes and refresh tokens are single-use: the second pop finds nothing."""
    store.put(Kind.CODE, 'code-1', {'client_id': 'c'}, expires_at=clock.now + 60)
    assert store.pop(Kind.CODE, 'code-1') == {'client_id': 'c'}
    assert store.pop(Kind.CODE, 'code-1') is None


def test_delete_tokens_keeps_the_login(store: Store, clock: Clock) -> None:
    """Rotating tokens removes the access and refresh token of one login and nothing else."""
    for kind in (Kind.GRANT, Kind.ACCESS, Kind.REFRESH):
        store.put(kind, 'mine', {}, expires_at=clock.now + 60, grant_id='grant-1')
    store.put(Kind.ACCESS, 'theirs', {}, expires_at=clock.now + 60, grant_id='grant-2')
    store.delete_tokens('grant-1')
    assert store.get(Kind.GRANT, 'mine') == {}
    assert store.get(Kind.ACCESS, 'mine') is None
    assert store.get(Kind.REFRESH, 'mine') is None
    assert store.get(Kind.ACCESS, 'theirs') == {}


def test_delete_grant_removes_everything_of_one_login(store: Store, clock: Clock) -> None:
    """Logging out removes the login with its codes and tokens, and leaves other logins alone."""
    for kind in (Kind.GRANT, Kind.CODE, Kind.ACCESS, Kind.REFRESH):
        store.put(kind, 'mine', {}, expires_at=clock.now + 60, grant_id='grant-1')
    store.put(Kind.GRANT, 'theirs', {}, expires_at=clock.now + 60, grant_id='grant-2')
    store.delete_grant('grant-1')
    assert [store.count(kind) for kind in (Kind.CODE, Kind.ACCESS, Kind.REFRESH)] == [0, 0, 0]
    assert store.get(Kind.GRANT, 'mine') is None
    assert store.get(Kind.GRANT, 'theirs') == {}


def test_database_holds_no_readable_keys_or_values(tmp_path: Path, clock: Clock) -> None:
    """A copy of the file reveals neither the tokens it is keyed by nor the tokens inside it."""
    path = tmp_path / 'store.sqlite3'
    store = Store(str(path), ENCRYPTION_KEY, clock)
    store.put(Kind.ACCESS, 'mcp-access-token', {'afas': 'afas-access-token'}, expires_at=clock.now + 60)
    store.close()
    database = sqlite3.connect(path)
    key, value = database.execute('SELECT key, value FROM records').fetchone()
    database.close()
    assert key == fingerprint('mcp-access-token')
    assert b'afas-access-token' not in value
    assert b'mcp-access-token' not in path.read_bytes()


def test_database_file_is_private_and_survives_a_restart(tmp_path: Path, clock: Clock) -> None:
    """The file is created for its owner only, and a new process finds the logins of the previous one."""
    path = tmp_path / 'store.sqlite3'
    first = Store(str(path), ENCRYPTION_KEY, clock)
    first.put(Kind.GRANT, 'grant-1', {'subject': 'jan'}, expires_at=clock.now + 60)
    first.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    second = Store(str(path), ENCRYPTION_KEY, clock)
    assert second.get(Kind.GRANT, 'grant-1') == {'subject': 'jan'}
    second.close()


def test_records_of_another_key_are_ignored(tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture) -> None:
    """After the encryption key changes the old logins are unreadable, which reads as not being logged in."""
    path = tmp_path / 'store.sqlite3'
    first = Store(str(path), ENCRYPTION_KEY, clock)
    first.put(Kind.GRANT, 'grant-1', {'subject': 'jan'}, expires_at=clock.now + 60)
    first.close()
    second = Store(str(path), OTHER_ENCRYPTION_KEY, clock)
    assert second.get(Kind.GRANT, 'grant-1') is None
    second.close()
    assert 'encrypted with another key' in caplog.text
