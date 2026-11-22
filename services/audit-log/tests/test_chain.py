from datetime import timedelta, timezone

import pytest
from fakes import T0, build_chain

from audit_log.chain import GENESIS, ChainBreak, canonical, entry_hash, verify_rows


def test_canonical_is_stable_across_timezones_and_ignores_chain_columns():
    entry = build_chain(1)[0]
    bogota = timezone(timedelta(hours=-5))
    same_instant = {**entry, "occurred_at": T0.astimezone(bogota), "seq": 99, "hash": "x"}
    assert canonical(entry) == canonical(same_instant)
    assert '"occurred_at":"2026-11-22T10:00:00.000000+00:00"' in canonical(entry)


def test_hash_depends_on_previous_hash_and_on_every_field():
    entry = build_chain(1)[0]
    base = entry_hash(GENESIS, entry)
    assert len(base) == 64
    assert entry_hash("1" * 64, entry) != base
    assert entry_hash(GENESIS, {**entry, "outcome": "denied"}) != base
    assert entry_hash(GENESIS, {**entry, "actor_id": None}) != base


def test_an_intact_chain_verifies_and_returns_the_anchor():
    rows = build_chain(3)
    assert verify_rows(rows) == (3, 3, rows[-1]["hash"])
    assert verify_rows([]) == (0, None, GENESIS)


def test_a_modified_row_is_detected():
    rows = build_chain(3)
    rows[1] = {**rows[1], "outcome": "denied"}
    with pytest.raises(ChainBreak, match="modificada") as exc:
        verify_rows(rows)
    assert exc.value.seq == 2


def test_a_deleted_row_is_detected_at_the_next_one():
    rows = build_chain(3)
    del rows[1]
    with pytest.raises(ChainBreak, match="borrada") as exc:
        verify_rows(rows)
    assert exc.value.seq == 3


def test_a_rewritten_row_with_a_recomputed_hash_still_breaks_the_next_link():
    rows = build_chain(3)
    forged = {**rows[1], "actor_id": "someone-else"}
    forged["hash"] = entry_hash(forged["prev_hash"], forged)
    rows[1] = forged
    with pytest.raises(ChainBreak) as exc:
        verify_rows(rows)
    assert exc.value.seq == 3
