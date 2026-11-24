"""Integración con PostgreSQL real (opcional).

Se ejecuta solo si `AUDIT_LOG_TEST_DATABASE_URL` apunta a una base; trabaja en un esquema
temporal que se borra al terminar, así que no toca las tablas reales.
"""

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from audit_log.chain import GENESIS, ChainBreak, verify_rows
from audit_log.db import AuditStore, apply_migrations, iter_chain, list_entries
from audit_log.event import InvalidEvent

DSN = os.environ.get("AUDIT_LOG_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="AUDIT_LOG_TEST_DATABASE_URL no definida")


@pytest.fixture
def connect():
    schema = f"audit_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        try:
            yield lambda: psycopg.connect(DSN, autocommit=True, options=f"-c search_path={schema}")
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def ev(**extra):
    return {
        "event_id": str(uuid.uuid4()),
        "action": "room.delete",
        "outcome": "ok",
        "actor_id": "user-1",
        "actor_role": "host",
        "room_id": "room-1",
        "source": "identity-api",
        "occurred_at": datetime(2026, 11, 22, 10, tzinfo=UTC),
        **extra,
    }


def test_migrations_are_applied_once(connect):
    with connect() as conn:
        assert apply_migrations(conn) == ["001_init"]
        assert apply_migrations(conn) == []


def test_entries_are_saved_in_order_and_idempotent(connect):
    with connect() as conn:
        apply_migrations(conn)
    store = AuditStore(connect)
    first, second = ev(), ev(action="access.denied", outcome="denied", actor_id=None, room_id=None)
    assert store.save_many([first, second, first]) == 2
    assert store.save(first) is False
    store.close()
    with connect() as conn:
        rows = conn.execute(
            "SELECT seq, event_id::text, action, actor_id FROM audit_entries ORDER BY seq"
        ).fetchall()
    assert [(r[1], r[2], r[3]) for r in rows] == [
        (first["event_id"], "room.delete", "user-1"),
        (second["event_id"], "access.denied", None),
    ]
    assert rows[0][0] < rows[1][0]


def test_an_outcome_outside_the_contract_is_rejected_by_the_database(connect):
    with connect() as conn:
        apply_migrations(conn)
    store = AuditStore(connect)
    with pytest.raises(InvalidEvent):
        store.save(ev(outcome="maybe"))
    assert store.save(ev()) is True, "la conexión sigue sirviendo tras el rechazo"
    store.close()


def saved(connect, n=3):
    with connect() as conn:
        apply_migrations(conn)
    store = AuditStore(connect)
    store.save_many([ev(room_id=f"room-{i}") for i in range(n)])
    store.close()


def test_rows_read_back_from_postgres_verify(connect):
    saved(connect)
    with connect() as conn:
        count, last_seq, anchor = verify_rows(iter_chain(conn, page_size=2))
    assert count == 3
    assert len(anchor) == 64


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_entries SET outcome = 'denied' WHERE seq = 1",
        "DELETE FROM audit_entries WHERE seq = 1",
        "TRUNCATE audit_entries",
    ],
)
def test_update_delete_and_truncate_are_rejected(connect, statement):
    saved(connect)
    with connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="solo admite INSERT"):
            conn.execute(statement)
        (count,) = conn.execute("SELECT count(*) FROM audit_entries").fetchone()
    assert count == 3


@pytest.mark.parametrize(
    "tamper, broken_seq",
    [
        ("UPDATE audit_entries SET actor_id = 'otro' WHERE seq = 2", 2),
        ("DELETE FROM audit_entries WHERE seq = 2", 3),
    ],
)
def test_a_superuser_edit_with_the_trigger_disabled_breaks_the_chain(connect, tamper, broken_seq):
    saved(connect)
    with connect() as conn:
        conn.execute("ALTER TABLE audit_entries DISABLE TRIGGER USER")
        conn.execute(tamper)
        conn.execute("ALTER TABLE audit_entries ENABLE TRIGGER USER")
        with pytest.raises(ChainBreak) as exc:
            verify_rows(iter_chain(conn))
    assert exc.value.seq == broken_seq


def test_list_entries_reads_back_from_postgres(connect):
    saved(connect)
    with connect() as conn:
        rows = list_entries(conn, room_id="room-1")
    assert [r["room_id"] for r in rows] == ["room-1"]
    assert rows[0]["prev_hash"] != GENESIS
