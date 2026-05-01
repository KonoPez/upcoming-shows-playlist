"""
Tests for spotify_client/client.py — pure-logic methods only, no API calls.
"""

from unittest.mock import MagicMock

import pytest

from spotify_client.client import SpotifyClient


@pytest.fixture
def client():
    return SpotifyClient(sp=MagicMock())


class TestIsVariantRecording:
    # ── Live — album level ────────────────────────────────────────────────────

    def test_live_at_venue(self, client):
        assert client._is_variant_recording("Song", "Live at the Apollo")

    def test_live_from_venue(self, client):
        assert client._is_variant_recording("Song", "Live from Austin TX")

    def test_live_in_city(self, client):
        assert client._is_variant_recording("Song", "Live in London")

    def test_unplugged_album(self, client):
        assert client._is_variant_recording("Song", "MTV Unplugged")

    def test_acoustic_sessions_album(self, client):
        assert client._is_variant_recording("Song", "Acoustic Sessions")

    def test_acoustic_session_singular(self, client):
        assert client._is_variant_recording("Song", "Acoustic Session")

    def test_live_parenthetical_album(self, client):
        assert client._is_variant_recording("Song", "Greatest Hits (Live)")

    # ── Track-level filtering — parenthetical ────────────────────────────────

    def test_track_live_suffix(self, client):
        assert client._is_variant_recording("Song (Live)", "Studio Album")

    def test_track_live_at_suffix(self, client):
        assert client._is_variant_recording("Song (Live at Glastonbury)", "Studio Album")

    def test_track_acoustic_version(self, client):
        assert client._is_variant_recording("Song (Acoustic Version)", "Studio Album")

    def test_track_acoustic_no_version(self, client):
        assert client._is_variant_recording("Song (Acoustic)", "Studio Album")

    def test_track_unplugged_suffix(self, client):
        assert client._is_variant_recording("Song (Unplugged)", "Studio Album")

    # ── Track-level filtering — dash suffix ──────────────────────────────────

    def test_track_dash_acoustic(self, client):
        # Regression: "DWTK - Acoustic" by Good Kid was slipping through
        assert client._is_variant_recording("DWTK - Acoustic", "Studio Album")

    def test_track_dash_demo(self, client):
        # Regression: "In Twos - Demo" by Horsegirl was slipping through
        assert client._is_variant_recording("In Twos - Demo", "Studio Album")

    def test_track_dash_live(self, client):
        assert client._is_variant_recording("Song - Live", "Studio Album")

    def test_track_dash_live_version(self, client):
        assert client._is_variant_recording("Song - Live Version", "Studio Album")

    def test_track_dash_acoustic_version(self, client):
        assert client._is_variant_recording("Song - Acoustic Version", "Studio Album")

    def test_track_em_dash_acoustic(self, client):
        assert client._is_variant_recording("Song – Acoustic", "Studio Album")

    def test_track_dash_instrumental(self, client):
        assert client._is_variant_recording("Song - Instrumental", "Studio Album")

    # ── Remix ─────────────────────────────────────────────────────────────────

    def test_track_remix(self, client):
        assert client._is_variant_recording("Song (X Remix)", "Studio Album")

    def test_track_remix_bare(self, client):
        assert client._is_variant_recording("Song (Remix)", "Studio Album")

    def test_album_remixed(self, client):
        assert client._is_variant_recording("Song", "Album Remixed")

    def test_album_the_remixes(self, client):
        assert client._is_variant_recording("Song", "The Remixes")

    def test_album_remixes(self, client):
        assert client._is_variant_recording("Song", "Remixes")

    # ── Instrumental ──────────────────────────────────────────────────────────

    def test_track_instrumental(self, client):
        assert client._is_variant_recording("Song (Instrumental)", "Studio Album")

    def test_track_instrumental_version(self, client):
        assert client._is_variant_recording("Song (Instrumental Version)", "Studio Album")

    # ── Demo ─────────────────────────────────────────────────────────────────

    def test_track_demo(self, client):
        assert client._is_variant_recording("Song (Demo)", "Studio Album")

    def test_track_demo_version(self, client):
        assert client._is_variant_recording("Song (Demo Version)", "Studio Album")

    def test_album_demos(self, client):
        assert client._is_variant_recording("Song", "Demos")

    def test_album_demo(self, client):
        assert client._is_variant_recording("Song", "Demo")

    # ── A cappella ────────────────────────────────────────────────────────────

    def test_track_a_cappella(self, client):
        assert client._is_variant_recording("Song (A Cappella)", "Studio Album")

    def test_track_acapella(self, client):
        assert client._is_variant_recording("Song (Acapella)", "Studio Album")

    # ── Should NOT be filtered ────────────────────────────────────────────────

    def test_normal_studio_track(self, client):
        assert not client._is_variant_recording("From the Start", "NIIIGAATA")

    def test_live_in_track_title_without_parens(self, client):
        # "Live Wire" — the word live is not in parens
        assert not client._is_variant_recording("Live Wire", "Studio Album")

    def test_acoustic_as_standalone_title(self, client):
        # A track artistically titled "Acoustic" (no parens)
        assert not client._is_variant_recording("Acoustic", "Studio Album")

    def test_remix_as_standalone_title(self, client):
        # A track titled just "Remix" without parens
        assert not client._is_variant_recording("Remix", "Studio Album")

    def test_live_in_artist_or_album_name_word_boundary(self, client):
        # "Olive" contains "live" but not as a standalone word
        assert not client._is_variant_recording("Song", "Olive Branch Sessions")

    def test_album_named_live_without_qualifier(self, client):
        # An album simply called "Live" (no "at/from/in") is ambiguous —
        # intentionally not matched to avoid false positives on artistic titles
        assert not client._is_variant_recording("Song", "Live")

    def test_radio_edit_not_filtered(self, client):
        assert not client._is_variant_recording("Song (Radio Edit)", "Studio Album")

    def test_remastered_not_filtered(self, client):
        assert not client._is_variant_recording("Song (Remastered)", "Studio Album")

    def test_deluxe_album_not_filtered(self, client):
        assert not client._is_variant_recording("Song", "Album (Deluxe Edition)")


# ── _get_popularity_batch ─────────────────────────────────────────────────────

class TestGetPopularityBatch:
    def _client_with_get(self, tracks_response):
        sp = MagicMock()
        sp._get.return_value = {'tracks': tracks_response}
        return SpotifyClient(sp=sp)

    def test_uses_get_not_tracks_wrapper(self):
        # Regression: sp.tracks() passes market=None which Spotify rejects with 403.
        # Must use sp._get() with IDs embedded in the URL instead.
        client = self._client_with_get([])
        client._get_popularity_batch(['abc123'])
        client.sp._get.assert_called_once()
        client.sp.tracks.assert_not_called()

    def test_ids_embedded_in_url(self):
        client = self._client_with_get([])
        client._get_popularity_batch(['id1', 'id2', 'id3'])
        url = client.sp._get.call_args[0][0]
        assert 'id1,id2,id3' in url
        assert 'market' not in url

    def test_returns_popularity_by_track_id(self):
        client = self._client_with_get([
            {'id': 'abc', 'popularity': 72},
            {'id': 'def', 'popularity': 45},
        ])
        result = client._get_popularity_batch(['abc', 'def'])
        assert result == {'abc': 72, 'def': 45}

    def test_batches_at_50(self):
        sp = MagicMock()
        sp._get.return_value = {'tracks': []}
        client = SpotifyClient(sp=sp)
        ids = [f'id{i}' for i in range(60)]
        client._get_popularity_batch(ids)
        assert sp._get.call_count == 2
        first_url = sp._get.call_args_list[0][0][0]
        assert first_url.count(',') == 49   # 50 IDs = 49 commas

    def test_returns_empty_on_api_error(self):
        sp = MagicMock()
        sp._get.side_effect = Exception('403 Forbidden')
        client = SpotifyClient(sp=sp)
        result = client._get_popularity_batch(['abc'])
        assert result == {}

    def test_skips_null_tracks(self):
        # Spotify occasionally returns null entries in the tracks array
        client = self._client_with_get([None, {'id': 'abc', 'popularity': 60}])
        result = client._get_popularity_batch(['abc'])
        assert result == {'abc': 60}
