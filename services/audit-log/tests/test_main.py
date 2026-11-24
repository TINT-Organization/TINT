import threading

import psycopg
from fakes import FakeConn, FakeRedis

from audit_log import __main__ as entry
from audit_log.event import CONSUMER_GROUP, STREAM_KEY

ENV = {
    "DATABASE_URL": "postgresql://u:p@db/tint",
    "REDIS_URL": "redis://redis:6379",
    "AUDIT_LOG_PORT": "8089",
    "AUDIT_LOG_CONSUMER": "audit-log-1",
}


class FakeServer:
    def __init__(self, host, port):
        self.address = (host, port)
        self.stopped = threading.Event()
        self.closed = False

    def serve_forever(self):
        self.stopped.wait(5)

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.closed = True


class Recorder:
    def __init__(self, fail_connect=False):
        self.conns = []
        self.redis_calls = []
        self.redis = FakeRedis()
        self.servers = []
        self.fail_connect = fail_connect

    def connect(self, dsn, **kwargs):
        assert dsn == ENV["DATABASE_URL"]
        assert kwargs == {"autocommit": True}
        if self.fail_connect:
            raise psycopg.OperationalError("postgres caído")
        self.conns.append(FakeConn())
        return self.conns[-1]

    def redis_factory(self, url, **kwargs):
        self.redis_calls.append((url, kwargs))
        return self.redis

    def server_factory(self, host, port):
        self.servers.append(FakeServer(host, port))
        return self.servers[-1]


def run_main(env=ENV, rec=None):
    rec = rec or Recorder()
    stop = threading.Event()
    stop.set()  # arranca todo y se apaga enseguida
    code = entry.main(
        env,
        stop=stop,
        connect=rec.connect,
        redis_factory=rec.redis_factory,
        server_factory=rec.server_factory,
    )
    return code, rec


def test_main_migrates_starts_consumer_and_health_then_shuts_down():
    code, rec = run_main()
    assert code == 0
    migrations = rec.conns[0].statements("INSERT INTO audit_schema_migrations")
    assert [params for _sql, params in migrations] == [("001_init",)]
    assert rec.conns[0].closed
    assert rec.redis_calls == [(ENV["REDIS_URL"], {"decode_responses": True})]
    assert rec.redis.groups == {(STREAM_KEY, CONSUMER_GROUP)}
    (server,) = rec.servers
    assert server.address == ("0.0.0.0", 8089)
    assert server.closed


def test_missing_config_exits_with_code_2(capsys):
    code, _ = run_main(env={})
    assert code == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_invalid_log_level_exits_with_code_2(capsys):
    code, _ = run_main(env={**ENV, "LOG_LEVEL": "verbose"})
    assert code == 2
    assert "LOG_LEVEL" in capsys.readouterr().err


def test_database_down_at_start_exits_with_code_1():
    code, rec = run_main(rec=Recorder(fail_connect=True))
    assert code == 1
    assert rec.servers == []


def test_signal_handlers_set_the_stop_event(monkeypatch):
    handlers = {}
    monkeypatch.setattr(entry.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))
    stop = threading.Event()
    entry.install_signal_handlers(stop)
    handlers[entry.signal.SIGTERM]()
    assert stop.is_set()
