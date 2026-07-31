"""
Tests for spotify_client/client.py — pure-logic methods only, no API calls.
"""

from unittest.mock import MagicMock

import pytest

from spotify_client.client import SpotifyClient, deduplicate_tracks
from sources.models import Track


@pytest.fixture
def client():
    return SpotifyClient(sp=MagicMock())


def make_track(
    id='id1',
    name='Song',
    release_date='2020-01-01',
    release_date_precision='day',
    duration_ms=200000,
    album_id='alb1',
    album_name='Studio Album',
) -> Track:
    return Track(
        id=id,
        name=name,
        release_date=release_date,
        release_date_precision=release_date_precision,
        duration_ms=duration_ms,
        album_id=album_id,
        album_name=album_name,
    )


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


class TestDeduplicateTracks:
    # ── Grouping basics ───────────────────────────────────────────────────────

    def test_single_track_passthrough(self):
        t = make_track(id='a', name='Song')
        result = deduplicate_tracks([t])
        assert result == [t]

    def test_distinct_songs_all_kept(self):
        a = make_track(id='a', name='Alpha')
        b = make_track(id='b', name='Beta')
        result = deduplicate_tracks([a, b])
        assert len(result) == 2
        assert {t.id for t in result} == {'a', 'b'}

    def test_non_stripped_suffix_keeps_songs_distinct(self):
        # "(Remastered)" is not a variant keyword, so it isn't stripped by
        # normalize_track_name — these two land in separate groups entirely.
        a = make_track(id='a', name='Song')
        b = make_track(id='b', name='Song (Remastered)')
        result = deduplicate_tracks([a, b])
        assert len(result) == 2
        assert {t.id for t in result} == {'a', 'b'}

    # ── Single non-variant wins ───────────────────────────────────────────────

    def test_studio_preferred_over_live_variant(self):
        studio = make_track(id='studio', name='Song', album_name='Studio Album')
        live = make_track(id='live', name='Song (Live)', album_name='Studio Album')
        result = deduplicate_tracks([studio, live])
        assert result == [studio]

    # ── All-variant group ─────────────────────────────────────────────────────

    def test_all_variant_group_falls_back_to_shortest_title(self):
        long_live = make_track(
            id='long', name='Song (Live at the Greek Theatre)', album_name='Live Album'
        )
        short_acoustic = make_track(
            id='short', name='Song (Acoustic)', album_name='Studio Album'
        )
        result = deduplicate_tracks([long_live, short_acoustic])
        assert result == [short_acoustic]

    def test_all_variant_group_lastfm_score_overrides_shortest_title(self):
        # Without lastfm data, the shorter "Song (Live)" would win on length.
        # A positive lastfm score for the literal (lowercased/stripped) name of
        # the longer track should override that fallback.
        shorter = make_track(id='shorter', name='Song (Live)', album_name='Live Album')
        longer = make_track(
            id='longer', name='Song (Acoustic Extended Mix)', album_name='Studio Album'
        )
        lastfm_scores = {'song (acoustic extended mix)': 3.5}
        result = deduplicate_tracks([shorter, longer], lastfm_scores=lastfm_scores)
        assert result == [longer]

    def test_all_variant_group_lastfm_scores_all_zero_falls_back_to_shortest(self):
        shorter = make_track(id='shorter', name='Song (Live)', album_name='Live Album')
        longer = make_track(
            id='longer', name='Song (Acoustic Extended Mix)', album_name='Studio Album'
        )
        # Neither literal name has a positive score, so the lastfm branch
        # doesn't fire and we fall back to shortest-title.
        lastfm_scores = {'some other track': 9.0}
        result = deduplicate_tracks([shorter, longer], lastfm_scores=lastfm_scores)
        assert result == [shorter]

    # ── Multiple non-variants ─────────────────────────────────────────────────

    def test_two_identical_non_variant_tracks_returns_exactly_one(self):
        first = make_track(id='a', name='Song')
        second = make_track(id='b', name='Song')
        result = deduplicate_tracks([first, second])
        assert len(result) == 1
        # Equal-length titles tie, so the first in the list wins.
        assert result[0].id == 'a'


class TestGetArtistAlbums:
    def test_non_variant_beats_newer_variant(self, client):
        client.sp._get.return_value = {
            'items': [
                {'id': 'live', 'name': 'Rumours (Live)', 'release_date': '2023',
                 'release_date_precision': 'year'},
                {'id': 'studio', 'name': 'Rumours', 'release_date': '2020',
                 'release_date_precision': 'year'},
            ],
            'next': None,
        }
        result = client._get_artist_albums('artist1')
        ids = [a['id'] for a in result]
        assert ids == ['studio']

    def test_same_kind_collision_newest_wins(self, client):
        client.sp._get.return_value = {
            'items': [
                {'id': 'old', 'name': 'Album', 'release_date': '2018',
                 'release_date_precision': 'year'},
                {'id': 'new', 'name': 'Album', 'release_date': '2022',
                 'release_date_precision': 'year'},
            ],
            'next': None,
        }
        result = client._get_artist_albums('artist1')
        ids = [a['id'] for a in result]
        assert ids == ['new']

    def test_deluxe_not_treated_as_variant_collapses_by_recency(self, client):
        client.sp._get.return_value = {
            'items': [
                {'id': 'plain', 'name': 'X', 'release_date': '2020',
                 'release_date_precision': 'year'},
                {'id': 'deluxe', 'name': 'X (Deluxe)', 'release_date': '2022',
                 'release_date_precision': 'year'},
            ],
            'next': None,
        }
        result = client._get_artist_albums('artist1')
        # Both collapse into a single entry — "(Deluxe)" isn't a variant
        # keyword, so this is a same-kind collision decided by recency alone.
        assert len(result) == 1
        assert result[0]['id'] == 'deluxe'


class TestFetchArtistTracks:
    def test_oldest_studio_version_wins_on_name_collision(self, client):
        client._get_artist_albums = MagicMock(return_value=[
            {'id': 'alb_new', 'name': 'Album New', 'release_date': '2023-01-01',
             'release_date_precision': 'day'},
            {'id': 'alb_old', 'name': 'Album Old', 'release_date': '2020-01-01',
             'release_date_precision': 'day'},
        ])

        def fake_get_album_tracks(album_id):
            return {
                'alb_new': [{'id': 't_new', 'name': 'Song', 'duration_ms': 1000}],
                'alb_old': [{'id': 't_old', 'name': 'Song', 'duration_ms': 2000}],
            }.get(album_id, [])

        client._get_album_tracks = MagicMock(side_effect=fake_get_album_tracks)

        result = client._fetch_artist_tracks('artist1')
        assert len(result) == 1
        assert result[0].id == 't_old'
        assert result[0].album_name == 'Album Old'

    def test_studio_and_live_variant_both_survive(self, client):
        client._get_artist_albums = MagicMock(return_value=[
            {'id': 'studio_id', 'name': 'Studio Album', 'release_date': '2022-01-01',
             'release_date_precision': 'day'},
            {'id': 'live_id', 'name': 'Live at Wembley', 'release_date': '2019-01-01',
             'release_date_precision': 'day'},
        ])

        def fake_get_album_tracks(album_id):
            return {
                'studio_id': [{'id': 't_studio', 'name': 'Song', 'duration_ms': 1000}],
                'live_id': [{'id': 't_live', 'name': 'Song', 'duration_ms': 2000}],
            }.get(album_id, [])

        client._get_album_tracks = MagicMock(side_effect=fake_get_album_tracks)

        result = client._fetch_artist_tracks('artist1')
        assert len(result) == 2
        by_id = {t.id: t for t in result}
        assert by_id['t_studio'].album_name == 'Studio Album'
        assert by_id['t_live'].album_name == 'Live at Wembley'

