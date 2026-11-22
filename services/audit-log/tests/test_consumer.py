import logging
import threading
import uuid
from unittest.mock import Mock

import pytest
from fakes import FakeAuditStore, FakeRedis
from redis.exceptions import ResponseError

from audit_log.consumer import RETRY_DELAY_S, StreamConsumer
from audit_log.event import CONSUMER_GROUP, STREAM_KEY


def eid(name):
    """UUID estable a partir de un nombre corto, para leer las pruebas más fácil."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, name))


def fields(name="e1", **extra):
    return {
        "event_id": eid(name),
        "action": "ban.create",
        "outcome": "ok",
        "actor_id": "user-1",
        "actor_role": "host",
        "room_id": "r1",
        "occurred_at": "2026-11-22T10:00:00.000Z",
        "source": "identity-api",
        **extra,
    }


def make_consumer(redis, store=None, **kwargs):
    return StreamConsumer(redis, store or FakeAuditStore(), consumer="a1", **kwargs)


def test_ensure_group_creates_group_and_stream_once():
    redis = FakeRedis()
    consumer = make_consumer(redis)
    consumer.ensure_group()
    consumer.ensure_group()
    assert redis.groups == {(STREAM_KEY, CONSUMER_GROUP)}


def test_ensure_group_propagates_other_errors():
    redis = Mock()
    redis.xgroup_create.side_effect = ResponseError("NOPERM")
    with pytest.raises(ResponseError):
        make_consumer(redis).ensure_group()


def test_poll_validates_saves_and_acks_each_message():
    redis = FakeRedis()
    ids = [redis.xadd(STREAM_KEY, fields(e, email="a@b.co")) for e in ("e1", "e2")]
    store = FakeAuditStore()
    assert make_consumer(redis, store).poll() == 2
    assert list(store.entries) == [eid("e1"), eid("e2")]
    assert "email" not in store.entries[eid("e1")]
    assert redis.acks == ids
    assert redis.pending == {}


def test_poll_saves_the_whole_batch_in_one_call_and_acks_it_at_once():
    redis = Mock()
    redis.xreadgroup.return_value = [[STREAM_KEY, [("1-0", fields("e1")), ("2-0", fields("e2"))]]]
    store = FakeAuditStore()
    assert make_consumer(redis, store).poll() == 2
    assert store.batches == [[eid("e1"), eid("e2")]]
    redis.xack.assert_called_once_with(STREAM_KEY, CONSUMER_GROUP, "1-0", "2-0")


def test_poll_without_messages_does_not_touch_the_store_or_ack():
    redis = Mock()
    redis.xreadgroup.return_value = []
    store = FakeAuditStore()
    assert make_consumer(redis, store).poll() == 0
    assert store.saves == 0
    redis.xack.assert_not_called()


def test_poll_passes_block_and_count_to_redis():
    redis = Mock()
    redis.xreadgroup.return_value = None
    assert make_consumer(redis, block_ms=1234, count=7).poll() == 0
    redis.xreadgroup.assert_called_once_with(
        CONSUMER_GROUP, "a1", {STREAM_KEY: ">"}, count=7, block=1234
    )


def test_duplicate_events_are_acked_without_saving_twice():
    redis = FakeRedis()
    first = redis.xadd(STREAM_KEY, fields())
    second = redis.xadd(STREAM_KEY, fields())
    store = FakeAuditStore()
    make_consumer(redis, store).poll()
    assert len(store.entries) == 1
    assert redis.acks == [first, second]


@pytest.mark.parametrize("bad", [fields(event_id=""), None, fields(outcome="maybe")])
def test_invalid_event_is_logged_and_acked_without_saving(caplog, bad):
    redis = FakeRedis()
    message_id = redis.xadd(STREAM_KEY, bad)
    store = FakeAuditStore()
    with caplog.at_level(logging.WARNING, logger="audit_log.consumer"):
        make_consumer(redis, store).poll()
    assert store.saves == 0
    assert redis.acks == [message_id]
    assert message_id in caplog.text


def test_a_rejected_event_only_discards_itself_and_the_rest_of_the_batch_is_saved(caplog):
    redis = FakeRedis()
    ids = [redis.xadd(STREAM_KEY, fields(e)) for e in ("e1", "bad", "e3")]
    store = FakeAuditStore(invalid_ids={eid("bad")})
    with caplog.at_level(logging.WARNING, logger="audit_log.consumer"):
        make_consumer(redis, store).poll()
    assert list(store.entries) == [eid("e1"), eid("e3")]
    # El lote entero falla una vez y luego se reintenta evento por evento.
    assert store.batches == [
        [eid("e1"), eid("bad"), eid("e3")],
        [eid("e1")],
        [eid("bad")],
        [eid("e3")],
    ]
    assert redis.acks == ids
    assert f"evento {ids[1]} inválido" in caplog.text


def test_store_error_leaves_the_whole_batch_pending():
    redis = FakeRedis()
    ids = [redis.xadd(STREAM_KEY, fields(e)) for e in ("e1", "e2")]
    store = FakeAuditStore(fail_times=1)
    with pytest.raises(RuntimeError):
        make_consumer(redis, store).poll()
    assert redis.acks == []
    assert set(redis.pending) == set(ids)
    assert store.entries == {}


def test_recover_pending_retries_unacked_messages_after_restart():
    redis = FakeRedis()
    redis.xadd(STREAM_KEY, fields())
    with pytest.raises(RuntimeError):
        make_consumer(redis, FakeAuditStore(fail_times=1)).poll()
    store = FakeAuditStore()
    assert make_consumer(redis, store).recover_pending() == 1
    assert list(store.entries) == [eid("e1")]
    assert redis.pending == {}


class StoppingStore(FakeAuditStore):
    def __init__(self, stop, stop_on, **kwargs):
        super().__init__(**kwargs)
        self.stop = stop
        self.stop_on = eid(stop_on)

    def stored(self, event):
        if event["event_id"] == self.stop_on:
            self.stop.set()
        return super().stored(event)


def test_run_survives_store_errors_and_retries_pending(caplog):
    redis = FakeRedis()
    stop = threading.Event()
    redis.xadd(STREAM_KEY, fields())
    store = StoppingStore(stop, "e1", fail_times=2)
    sleeps = []
    with caplog.at_level(logging.ERROR, logger="audit_log.consumer"):
        make_consumer(redis, store, sleep=sleeps.append).run(stop)
    assert store.saves == 3
    assert sleeps == [RETRY_DELAY_S, RETRY_DELAY_S]
    assert caplog.text.count("se reintenta") == 2
    assert redis.pending == {}


def test_run_recovers_pending_then_consumes_until_stopped(caplog):
    redis = FakeRedis()
    stop = threading.Event()
    redis.xadd(STREAM_KEY, fields("old"))
    redis.xreadgroup(CONSUMER_GROUP, "a1", {STREAM_KEY: ">"}, count=1)  # entregado y sin ACK
    redis.xadd(STREAM_KEY, fields("new"))
    store = StoppingStore(stop, "new")
    with caplog.at_level(logging.INFO, logger="audit_log.consumer"):
        make_consumer(redis, store).run(stop)
    assert list(store.entries) == [eid("old"), eid("new")]
    assert redis.pending == {}
    assert "reprocesados 1 eventos pendientes" in caplog.text


def test_run_returns_immediately_when_already_stopped():
    redis = FakeRedis()
    stop = threading.Event()
    stop.set()
    redis.xadd(STREAM_KEY, fields())
    store = FakeAuditStore()
    make_consumer(redis, store).run(stop)
    assert store.saves == 0
    assert redis.groups == {(STREAM_KEY, CONSUMER_GROUP)}
