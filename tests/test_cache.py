"""
Tests for Cache — key-value TTL cache and play-history accumulation.
Uses a temp SQLite file per test so tests are fully isolated.
"""

import time

import pytest

from cache import Cache


@pytest.fixture
def cache(tmp_path):
    return Cache(db_path=tmp_path / 'test.db')


# ── Key-value cache ───────────────────────────────────────────────────────────

class TestKVCache:
    def test_set_and_get(self, cache):
        cache.set('k', 'hello', ttl_seconds=3600)
        assert cache.get('k') == 'hello'

    def test_missing_key_returns_none(self, cache):
        assert cache.get('nonexistent') is None

    def test_expired_entry_returns_none(self, cache):
        cache.set('k', 'val', ttl_seconds=0)
        time.sleep(0.01)
        assert cache.get('k') is None

    def test_list_value_round_trips(self, cache):
        cache.set('k', [1, 2, 3], 3600)
        assert cache.get('k') == [1, 2, 3]

    def test_dict_value_round_trips(self, cache):
        cache.set('k', {'a': 1, 'b': 'two'}, 3600)
        assert cache.get('k') == {'a': 1, 'b': 'two'}

    def test_overwrite_replaces_value(self, cache):
        cache.set('k', 'first', 3600)
        cache.set('k', 'second', 3600)
        assert cache.get('k') == 'second'

    def test_delete_removes_entry(self, cache):
        cache.set('k', 'val', 3600)
        cache.delete('k')
        assert cache.get('k') is None

    def test_delete_nonexistent_is_safe(self, cache):
        cache.delete('never-existed')   # should not raise

    def test_clear_expired_removes_only_stale_entries(self, cache):
        cache.set('stale', 'old', ttl_seconds=0)
        cache.set('fresh', 'new', ttl_seconds=3600)
        time.sleep(0.01)
        cache.clear_expired()
        assert cache.get('stale') is None
        assert cache.get('fresh') == 'new'


# ── Play-history accumulation ─────────────────────────────────────────────────

class TestPlayHistory:
    def test_record_plays_returns_insert_count(self, cache):
        plays = [
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'},
            {'track_id': 't2', 'artist_id': 'a1', 'played_at': '2024-01-01T11:00:00Z'},
        ]
        assert cache.record_plays(plays) == 2

    def test_play_counts_aggregated_correctly(self, cache):
        plays = [
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'},
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-02T10:00:00Z'},
            {'track_id': 't2', 'artist_id': 'a1', 'played_at': '2024-01-01T11:00:00Z'},
        ]
        cache.record_plays(plays)
        assert cache.get_play_counts('a1') == {'t1': 2, 't2': 1}

    def test_duplicate_play_not_counted_twice(self, cache):
        play = {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'}
        first_insert = cache.record_plays([play])
        second_insert = cache.record_plays([play])
        assert first_insert == 1
        assert second_insert == 0
        assert cache.get_play_counts('a1') == {'t1': 1}

    def test_play_counts_scoped_to_artist(self, cache):
        plays = [
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'},
            {'track_id': 't2', 'artist_id': 'a2', 'played_at': '2024-01-01T11:00:00Z'},
        ]
        cache.record_plays(plays)
        assert cache.get_play_counts('a1') == {'t1': 1}
        assert cache.get_play_counts('a2') == {'t2': 1}

    def test_unknown_artist_returns_empty_dict(self, cache):
        assert cache.get_play_counts('ghost-artist') == {}

    def test_record_empty_list_returns_zero(self, cache):
        assert cache.record_plays([]) == 0

    def test_incremental_accumulation_across_calls(self, cache):
        # Simulate two weekly runs each adding new plays
        batch1 = [{'track_id': 't1', 'artist_id': 'a1', 'played_at': f'2024-01-0{i}T10:00:00Z'}
                  for i in range(1, 4)]
        batch2 = [{'track_id': 't1', 'artist_id': 'a1', 'played_at': f'2024-01-1{i}T10:00:00Z'}
                  for i in range(0, 3)]
        cache.record_plays(batch1)
        cache.record_plays(batch2)
        assert cache.get_play_counts('a1') == {'t1': 6}
