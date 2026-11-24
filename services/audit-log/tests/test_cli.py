import io
import json
from datetime import UTC, datetime

import psycopg
import pytest
from fakes import FakeConn, build_chain

from audit_log import list as list_cli
from audit_log import verify
from audit_log.db import ROW_COLUMNS

ENV = {"DATABASE_URL": "postgresql://u:p@db/tint"}


def as_tuples(rows):
    return [tuple(r[c] for c in ROW_COLUMNS) for r in rows]


def connect_to(rows):
    def connect(dsn, **kwargs):
        assert dsn == ENV["DATABASE_URL"]
        assert kwargs == {"autocommit": True}
        return FakeConn(lambda sql, params: rows if params[0] == 0 else [])

    return connect


def broken(dsn, **kwargs):
    raise psycopg.OperationalError("postgres caído")


def test_verify_ok_prints_count_and_anchor():
    rows = build_chain(2)
    out = io.StringIO()
    assert verify.main(ENV, connect=connect_to(as_tuples(rows)), out=out) == 0
    assert out.getvalue().startswith("OK: 2 filas")
    assert rows[-1]["hash"] in out.getvalue()


def test_verify_broken_chain_exits_1():
    rows = build_chain(2)
    rows[0] = {**rows[0], "room_id": "otra"}
    out = io.StringIO()
    assert verify.main(ENV, connect=connect_to(as_tuples(rows)), out=out) == 1
    assert "ROTA: cadena rota en seq=1" in out.getvalue()


def test_verify_without_database_url_or_connection_exits_2(capsys):
    assert verify.main({}) == 2
    assert verify.main(ENV, connect=broken) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_list_prints_one_json_line_per_row_with_filters():
    rows = build_chain(2)
    seen = []

    def connect(dsn, **kwargs):
        def responder(sql, params):
            seen.append(params)
            return list(reversed(as_tuples(rows)))

        return FakeConn(responder)

    out = io.StringIO()
    code = list_cli.main(["--room", "room-1", "--limit", "5"], ENV, connect=connect, out=out)
    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    assert lines[0]["occurred_at"] == datetime(2026, 11, 22, 10, tzinfo=UTC).isoformat()
    assert seen == [("room-1", 5)]


def test_list_errors(capsys):
    assert list_cli.main([], {}) == 2
    assert list_cli.main([], ENV, connect=broken) == 2
    with pytest.raises(SystemExit):
        list_cli.main(["--limit", "0"], ENV)
