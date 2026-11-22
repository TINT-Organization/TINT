from datetime import UTC, datetime

import psycopg
import pytest
from fakes import FakeConn

from audit_log.chain import GENESIS, entry_hash, verify_rows
from audit_log.db import (
    CHAIN_LOCK_ID,
    CHAIN_LOCK_SQL,
    ENTRY_COLUMNS,
    EXISTS_SQL,
    INSERT_ENTRY_SQL,
    LAST_HASH_SQL,
    MIGRATIONS_LOCK_ID,
    ROW_COLUMNS,
    SELECT_CHAIN_PAGE_SQL,
    AuditStore,
    Migration,
    apply_migrations,
    iter_chain,
    list_entries,
    load_migrations,
)
from audit_log.event import InvalidEvent

NOW = datetime(2026, 11, 22, 10, 0, 1, 123456, tzinfo=UTC)


def event(n=1, **extra):
    return {
        "event_id": f"00000000-0000-4000-8000-{n:012d}",
        "action": "room.delete",
        "outcome": "ok",
        "actor_id": "user-1",
        "actor_role": "host",
        "room_id": "room-1",
        "source": "identity-api",
        "occurred_at": datetime(2026, 11, 22, 10, tzinfo=UTC),
        **extra,
    }


class FakeTable:
    """Responde como `audit_entries` en memoria a las consultas de `AuditStore`."""

    def __init__(self):
        self.rows = []

    def __call__(self, sql, params):
        if sql == LAST_HASH_SQL:
            return [(self.rows[-1]["hash"],)] if self.rows else []
        if sql == EXISTS_SQL:
            return [(1,)] if any(r["event_id"] == params[0] for r in self.rows) else []
        if sql == INSERT_ENTRY_SQL:
            row = dict(zip(ENTRY_COLUMNS, params, strict=True))
            self.rows.append({"seq": len(self.rows) + 1, **row})
        return []


def test_load_migrations_reads_the_sql_files_in_order():
    migrations = load_migrations()
    assert [m.version for m in migrations][0] == "001_init"
    assert "CREATE TABLE IF NOT EXISTS audit_entries" in migrations[0].sql
    assert "BEFORE UPDATE OR DELETE ON audit_entries" in migrations[0].sql
    assert "BEFORE TRUNCATE ON audit_entries" in migrations[0].sql


def test_apply_migrations_runs_only_pending_ones_under_a_lock():
    conn = FakeConn(lambda sql, params: [("001_a",)] if sql.startswith("SELECT version") else [])
    applied = apply_migrations(conn, [Migration("001_a", "A"), Migration("002_b", "B")])
    assert applied == ["002_b"]
    assert conn.executed[0] == ("SELECT pg_advisory_xact_lock(%s)", (MIGRATIONS_LOCK_ID,))
    assert ("B", None) in conn.executed
    assert ("A", None) not in conn.executed
    assert conn.statements("INSERT INTO audit_schema_migrations") == [
        ("INSERT INTO audit_schema_migrations (version) VALUES (%s)", ("002_b",))
    ]
    assert conn.committed == 1


def test_save_many_chains_new_rows_in_one_locked_transaction():
    table = FakeTable()
    conn = FakeConn(table)
    store = AuditStore(lambda: conn, clock=lambda: NOW)
    assert store.save_many([event(1), event(2)]) == 2
    assert conn.transactions == conn.committed == 1
    assert conn.executed[0] == (CHAIN_LOCK_SQL, (CHAIN_LOCK_ID,))
    first, second = table.rows
    assert first["prev_hash"] == GENESIS
    assert second["prev_hash"] == first["hash"]
    assert first["recorded_at"] == NOW
    assert first["hash"] == entry_hash(GENESIS, first)
    assert verify_rows(table.rows)[0] == 2


def test_save_many_continues_the_chain_from_the_last_stored_hash():
    table = FakeTable()
    conn = FakeConn(table)
    store = AuditStore(lambda: conn, clock=lambda: NOW)
    store.save(event(1))
    store.save(event(2))
    assert table.rows[1]["prev_hash"] == table.rows[0]["hash"]
    assert verify_rows(table.rows)[0] == 2


def test_duplicates_in_the_table_or_in_the_batch_are_skipped():
    table = FakeTable()
    conn = FakeConn(table)
    store = AuditStore(lambda: conn, clock=lambda: NOW)
    assert store.save(event(1)) is True
    assert store.save_many([event(1), event(2), event(2)]) == 1
    assert [r["event_id"][-1] for r in table.rows] == ["1", "2"]
    assert verify_rows(table.rows)[0] == 2


@pytest.mark.parametrize("error", [psycopg.DataError, psycopg.IntegrityError])
def test_rejected_data_becomes_invalid_event_and_keeps_the_connection(error):
    def boom(sql, params):
        if sql == INSERT_ENTRY_SQL:
            raise error("valor rechazado")
        return []

    conn = FakeConn(boom)
    store = AuditStore(lambda: conn)
    with pytest.raises(InvalidEvent):
        store.save(event())
    assert not conn.closed


def test_infrastructure_error_drops_the_connection_and_reconnects():
    conns = []

    def fail(sql, params):
        raise psycopg.OperationalError("postgres caído")

    def connect():
        conns.append(FakeConn(fail if not conns else FakeTable()))
        return conns[-1]

    store = AuditStore(connect)
    with pytest.raises(psycopg.OperationalError):
        store.save(event())
    assert conns[0].closed
    assert store.save(event()) is True
    assert len(conns) == 2


def test_close_without_connection_is_a_no_op():
    AuditStore(lambda: FakeConn()).close()


def row(seq):
    return (seq, *(f"{c}-{seq}" for c in ROW_COLUMNS[1:]))


def test_iter_chain_reads_page_by_page_from_the_last_seq():
    pages = {0: [row(1), row(2)], 2: [row(3)]}
    conn = FakeConn(lambda sql, params: pages[params[0]])
    rows = list(iter_chain(conn, page_size=2))
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert rows[0]["hash"] == "hash-1"
    assert [p for s, p in conn.executed if s == SELECT_CHAIN_PAGE_SQL] == [(0, 2), (2, 2)]


def test_list_entries_filters_and_returns_oldest_first():
    conn = FakeConn(lambda sql, params: [row(3), row(2)])
    rows = list_entries(conn, room_id="room-1", actor_id="user-1", limit=2)
    assert [r["seq"] for r in rows] == [2, 3]
    sql, params = conn.executed[0]
    assert "WHERE room_id = %s AND actor_id = %s ORDER BY seq DESC LIMIT %s" in sql
    assert params == ("room-1", "user-1", 2)


def test_list_entries_without_filters():
    conn = FakeConn(lambda sql, params: [])
    assert list_entries(conn) == []
    sql, params = conn.executed[0]
    assert "WHERE" not in sql
    assert params == (100,)
