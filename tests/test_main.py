"""Tests for main._write_env_value.

Each test points main.ENV_FILE at a temp path via monkeypatch so the real
.env file is never touched.
"""

import main


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
