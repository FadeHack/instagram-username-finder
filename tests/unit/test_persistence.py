"""State save/load, atomicity, resume and corruption handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from instagram_username_finder.models import (
    STATE_VERSION,
    CheckResult,
    CheckStatus,
    ScanState,
)
from instagram_username_finder.persistence import CorruptStateError, StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "nested" / "state.json")


def sample_state() -> ScanState:
    return ScanState(
        search_length=3,
        current_index=8420,
        checked=8420,
        taken=8411,
        errors=0,
        found=[
            CheckResult(
                username="qzx",
                status=CheckStatus.POSSIBLY_AVAILABLE,
                http_status=404,
                latency_ms=184.0,
            )
        ],
        fingerprint="abc|3-4",
    )


class TestSaveAndLoad:
    def test_round_trips_every_field(self, store: StateStore) -> None:
        store.save(sample_state())
        loaded = store.load()
        assert loaded.search_length == 3
        assert loaded.current_index == 8420
        assert loaded.checked == 8420
        assert loaded.taken == 8411
        assert loaded.fingerprint == "abc|3-4"
        assert [result.username for result in loaded.found] == ["qzx"]
        assert loaded.found[0].status is CheckStatus.POSSIBLY_AVAILABLE

    def test_creates_missing_directories(self, store: StateStore) -> None:
        assert not store.exists
        store.save(ScanState())
        assert store.path.is_file()

    def test_writes_valid_versioned_json(self, store: StateStore) -> None:
        store.save(sample_state())
        payload = json.loads(store.path.read_text(encoding="utf-8"))
        assert payload["version"] == STATE_VERSION
        assert payload["updated_at"]

    def test_save_refreshes_the_timestamp(self, store: StateStore) -> None:
        state = sample_state()
        state.updated_at = "1999-01-01T00:00:00+00:00"
        store.save(state)
        assert store.load().updated_at != "1999-01-01T00:00:00+00:00"


class TestResume:
    def test_resumed_state_continues_from_the_saved_index(self, store: StateStore) -> None:
        store.save(sample_state())
        resumed = store.load()
        resumed.current_index += 100
        store.save(resumed)
        assert store.load().current_index == 8520

    def test_completed_lengths_survive_a_round_trip(self, store: StateStore) -> None:
        state = ScanState(completed_lengths=[4, 3])
        store.save(state)
        assert store.load().completed_lengths == [3, 4]


class TestAtomicity:
    def test_no_temporary_files_are_left_behind(self, store: StateStore) -> None:
        for _ in range(3):
            store.save(sample_state())
        assert [p.name for p in store.path.parent.iterdir()] == ["state.json"]

    def test_a_failed_write_leaves_the_previous_state_intact(
        self, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.save(sample_state())
        original = store.path.read_text(encoding="utf-8")

        def explode(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("instagram_username_finder.persistence.os.replace", explode)
        with pytest.raises(OSError):
            store.save(ScanState(current_index=999))

        assert store.path.read_text(encoding="utf-8") == original
        assert [p.name for p in store.path.parent.iterdir()] == ["state.json"]


class TestCorruption:
    def test_missing_file_raises(self, store: StateStore) -> None:
        with pytest.raises(CorruptStateError):
            store.load()

    def test_truncated_json_raises(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text('{"version": 1, "current_ind', encoding="utf-8")
        with pytest.raises(CorruptStateError):
            store.load()

    def test_non_object_json_raises(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(CorruptStateError):
            store.load()

    def test_unsupported_version_raises(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text(json.dumps({"version": 99}), encoding="utf-8")
        with pytest.raises(CorruptStateError, match="version"):
            store.load()

    def test_malformed_fields_raise(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text(
            json.dumps({"version": STATE_VERSION, "current_index": "eight"}),
            encoding="utf-8",
        )
        with pytest.raises(CorruptStateError):
            store.load()

    def test_load_or_none_swallows_corruption(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text("not json", encoding="utf-8")
        assert store.load_or_none() is None

    def test_quarantine_moves_the_file_aside(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text("not json", encoding="utf-8")
        moved = store.quarantine()
        assert moved is not None and moved.is_file()
        assert not store.exists

    def test_clear_removes_state(self, store: StateStore) -> None:
        store.save(sample_state())
        store.clear()
        assert not store.exists
        store.clear()  # idempotent
