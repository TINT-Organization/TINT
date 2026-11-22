from datetime import UTC, datetime

import pytest

from audit_log.event import (
    CONSUMER_GROUP,
    STREAM_KEY,
    InvalidEvent,
    parse_timestamp,
    validate_event,
)

EVENT_ID = "0b5a3c8e-6f1d-4e57-9c1a-2f0e6a7b8c9d"


def raw(**extra):
    return {
        "event_id": EVENT_ID,
        "action": "room.delete",
        "outcome": "ok",
        "actor_id": "user-1",
        "actor_role": "host",
        "room_id": "room-1",
        "occurred_at": "2026-11-22T10:00:00.000Z",
        "source": "identity-api",
        **extra,
    }


def test_stream_and_group_names():
    assert (STREAM_KEY, CONSUMER_GROUP) == ("audit:events", "audit-workers")


def test_valid_event_keeps_only_contract_fields():
    event = validate_event(raw(email="a@b.co", ip="10.0.0.1", nick="ana", token="eyJ"))
    assert event == {
        "event_id": EVENT_ID,
        "action": "room.delete",
        "outcome": "ok",
        "actor_id": "user-1",
        "actor_role": "host",
        "room_id": "room-1",
        "occurred_at": datetime(2026, 11, 22, 10, tzinfo=UTC),
        "source": "identity-api",
    }


def test_optional_fields_may_be_missing_or_empty():
    event = validate_event(raw(action="access.denied", outcome="denied", actor_id="", room_id=None))
    assert event["actor_id"] is None
    assert event["room_id"] is None
    without_role = raw()
    del without_role["actor_role"]
    assert validate_event(without_role)["actor_role"] is None


def test_event_id_is_normalized_to_lowercase_uuid():
    assert validate_event(raw(event_id=EVENT_ID.upper()))["event_id"] == EVENT_ID


@pytest.mark.parametrize("missing", ["event_id", "action", "outcome", "occurred_at", "source"])
def test_required_fields(missing):
    with pytest.raises(InvalidEvent, match=missing):
        validate_event(raw(**{missing: ""}))


@pytest.mark.parametrize(
    "bad",
    [
        {"event_id": "not-a-uuid"},
        {"action": "DROP TABLE"},
        {"action": "room"},
        {"outcome": "maybe"},
        {"occurred_at": "yesterday"},
        {"room_id": "a\x00b"},
        {"actor_id": "x" * 201},
        {"actor_role": 3},
    ],
)
def test_invalid_values_are_rejected(bad):
    with pytest.raises(InvalidEvent):
        validate_event(raw(**bad))


def test_parse_timestamp_assumes_utc_without_offset_and_converts_offsets():
    assert parse_timestamp("2026-11-22T10:00:00") == datetime(2026, 11, 22, 10, tzinfo=UTC)
    assert parse_timestamp("2026-11-22T05:00:00-05:00") == datetime(2026, 11, 22, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_timestamp(None)
