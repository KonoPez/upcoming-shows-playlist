"""Tests for main's helpers.

TestWriteEnvValue points main.ENV_FILE at a temp path via monkeypatch so the
real .env file is never touched.
"""

import datetime

import pytest
import requests
import spotipy

import main
from cache import Cache


class TestWriteEnvValue:
    """Tests for main._write_env_value."""

    def test_creates_file_when_missing(self, tmp_path, monkeypatch):
        env_path = tmp_path / '.env'
        monkeypatch.setattr(main, 'ENV_FILE', env_path)

        main._write_env_value('KEY', 'value')

        assert env_path.read_text() == 'KEY=value\n'

    def test_appends_when_key_absent(self, tmp_path, monkeypatch):
        env_path = tmp_path / '.env'
        env_path.write_text('OTHER=1\nSPOTIFY_CLIENT_ID=abc\n')
        monkeypatch.setattr(main, 'ENV_FILE', env_path)

        main._write_env_value('KEY', 'value')

        content = env_path.read_text()
        lines = content.splitlines()
        assert 'OTHER=1' in lines
        assert 'SPOTIFY_CLIENT_ID=abc' in lines
        assert 'KEY=value' in lines
        assert lines[-1] == 'KEY=value'
        assert content.endswith('\n')

    def test_updates_existing_key_in_place(self, tmp_path, monkeypatch):
        env_path = tmp_path / '.env'
        env_path.write_text('OTHER=1\nKEY=oldvalue\nANOTHER=2\n')
        monkeypatch.setattr(main, 'ENV_FILE', env_path)

        main._write_env_value('KEY', 'newvalue')

        content = env_path.read_text()
        lines = content.splitlines()
        assert 'oldvalue' not in content
        assert 'KEY=newvalue' in lines
        # Line count should not grow, and other keys are untouched.
        assert len(lines) == 3
        assert 'OTHER=1' in lines
        assert 'ANOTHER=2' in lines

    def test_matches_and_updates_key_with_space_before_equals(self, tmp_path, monkeypatch):
        # The implementation matches both 'KEY=' and 'KEY =' prefixes via
        # startswith, so a pre-existing "KEY =oldval" style line is found
        # and rewritten -- note the output is always normalized to
        # 'KEY=newvalue' with no space, regardless of the original spacing.
        env_path = tmp_path / '.env'
        env_path.write_text('KEY =oldval\n')
        monkeypatch.setattr(main, 'ENV_FILE', env_path)

        main._write_env_value('KEY', 'newvalue')

        content = env_path.read_text()
        lines = content.splitlines()
        assert len(lines) == 1
        assert lines[0] == 'KEY=newvalue'
        assert 'oldval' not in content


class TestIsTransient:
    @pytest.mark.parametrize('exc', [
        requests.exceptions.ReadTimeout('timed out'),
        requests.exceptions.ConnectionError('Idle timeout'),
        TimeoutError('timed out'),
        spotipy.SpotifyException(429, -1, 'slow down'),
        spotipy.SpotifyException(503, -1, 'unavailable'),
    ])
    def test_transient(self, exc):
        assert main._is_transient(exc)

    @pytest.mark.parametrize('exc', [
        spotipy.SpotifyException(404, -1, 'missing'),
        KeyError('items'),
        ValueError('bad config'),
    ])
    def test_not_transient(self, exc):
        assert not main._is_transient(exc)


class TestRunWithRetries:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        self.slept = []
        monkeypatch.setattr(main.time, 'sleep', self.slept.append)

    def _flaky(self, failures):
        calls = []
        def run():
            calls.append(1)
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
        return run, calls

    def test_success_needs_no_retry(self):
        run, calls = self._flaky([])
        main._run_with_retries(run, (60, 120))
        assert len(calls) == 1 and self.slept == []

    def test_transient_failures_are_retried_on_schedule(self):
        run, calls = self._flaky([requests.exceptions.ReadTimeout(), requests.exceptions.ReadTimeout()])
        main._run_with_retries(run, (60, 120, 240))
        assert len(calls) == 3 and self.slept == [60, 120]

    def test_gives_up_after_last_delay(self):
        run, calls = self._flaky([requests.exceptions.ReadTimeout()] * 5)
        with pytest.raises(requests.exceptions.ReadTimeout):
            main._run_with_retries(run, (60, 120))
        assert len(calls) == 3

    def test_bugs_are_not_retried(self):
        run, calls = self._flaky([KeyError('items')])
        with pytest.raises(KeyError):
            main._run_with_retries(run, (60,))
        assert len(calls) == 1 and self.slept == []

    def test_no_delays_means_no_retries(self):
        # Manual runs pass no delays: someone is watching, so fail fast.
        run, calls = self._flaky([requests.exceptions.ReadTimeout()])
        with pytest.raises(requests.exceptions.ReadTimeout):
            main._run_with_retries(run, ())
        assert len(calls) == 1


class TestRanWithin:
    @pytest.fixture
    def cache(self, tmp_path):
        return Cache(db_path=tmp_path / 'test.db')

    def _log(self, cache, command, hours_ago):
        run_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
        with cache._conn() as conn:
            conn.execute(
                "INSERT INTO run_log (run_at, trigger, command) VALUES (?, 'cron', ?)",
                (run_at.isoformat(), command),
            )

    def test_never_ran(self, cache):
        assert not main._ran_within(cache, 'discover', 60)

    def test_recent_run_skips(self, cache):
        self._log(cache, 'discover', 30)
        assert main._ran_within(cache, 'discover', 60)

    def test_old_run_does_not_skip(self, cache):
        self._log(cache, 'discover', 61)
        assert not main._ran_within(cache, 'discover', 60)

    def test_tick_jitter_is_absorbed(self, cache):
        # Logged two minutes after the tick it started on; the tick 60h later
        # must still count as due.
        self._log(cache, 'discover', 60 - 2 / 60)
        assert not main._ran_within(cache, 'discover', 60)

    def test_other_commands_do_not_count(self, cache):
        self._log(cache, 'update', 1)
        assert not main._ran_within(cache, 'discover', 60)
