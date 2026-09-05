"""
Tests for spotify_client/client.py — pure-logic methods only, no API calls.
"""

from unittest.mock import MagicMock

import pytest

from spotify_client.client import (
    TOP_ARTIST_RANK_SPAN,
    SpotifyClient,
    album_type_rank,
    deduplicate_tracks,
    pad_release_date,
)
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
    album_type='album',
) -> Track:
    return Track(
        id=id,
        name=name,
        release_date=release_date,
        release_date_precision=release_date_precision,
        duration_ms=duration_ms,
        album_id=album_id,
        album_name=album_name,
        album_type=album_type,
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

    # ── Album version beats single version ────────────────────────────────────

    def test_album_version_beats_single_version(self):
        # Combat's "Stay Golden" shipped on the single "Epic Season Finale" a
        # month before the album of the same name. The album cut is canonical.
        single = make_track(
            id='single', name='Stay Golden',
            album_name='Epic Season Finale', album_type='single',
            release_date='2024-07-16',
        )
        album = make_track(
            id='album', name='Stay Golden',
            album_name='Stay Golden', album_type='album',
            release_date='2024-08-16',
        )
        result = deduplicate_tracks([single, album])
        assert [t.id for t in result] == ['album']

    def test_album_version_beats_compilation_version(self):
        comp = make_track(id='comp', name='Song', album_type='compilation')
        album = make_track(id='album', name='Song', album_type='album')
        result = deduplicate_tracks([comp, album])
        assert [t.id for t in result] == ['album']

    def test_album_type_outranks_lastfm_tiebreak(self):
        # No studio cut exists, so both candidates are variants. The Last.fm
        # score would pick the single's, but release type narrows the field
        # before Last.fm is consulted.
        single = make_track(
            id='single', name='Song (Live)',
            album_name='Live at the Roxy', album_type='single',
        )
        album = make_track(
            id='album', name='Song (Live at Wembley)',
            album_name='Live at Wembley', album_type='album',
        )
        lastfm_scores = {'song (live)': 9.0}
        result = deduplicate_tracks([single, album], lastfm_scores=lastfm_scores)
        assert [t.id for t in result] == ['album']

    def test_all_singles_group_still_falls_back_to_existing_tiebreaks(self):
        # No album pressing exists — ranks tie, so shortest title decides.
        a = make_track(
            id='a', name='Song (Live at the Greek Theatre)',
            album_name='Live at the Greek Theatre', album_type='single',
        )
        b = make_track(
            id='b', name='Song (Live)', album_name='Live at X', album_type='single'
        )
        result = deduplicate_tracks([a, b])
        assert [t.id for t in result] == ['b']

    def test_studio_single_still_beats_live_album_cut(self):
        # Release type is only consulted among candidates that survive the
        # variant filter, so an album's live cut can't outrank a studio single.
        live = make_track(
            id='live', name='Song (Live)', album_name='Album', album_type='album'
        )
        studio_single = make_track(id='single', name='Song', album_type='single')
        result = deduplicate_tracks([live, studio_single])
        assert [t.id for t in result] == ['single']


class TestAlbumTypeRank:
    def test_ordering(self):
        assert (
            album_type_rank('album')
            > album_type_rank('compilation')
            > album_type_rank('single')
            > album_type_rank('')
        )

    def test_case_and_whitespace_insensitive(self):
        assert album_type_rank(' Album ') == album_type_rank('album')

    def test_unknown_type_ranks_last(self):
        assert album_type_rank('mixtape') == 0


class TestPadReleaseDate:
    def test_year_precision_padded(self):
        assert pad_release_date('2020') == '2020-01-01'

    def test_month_precision_padded(self):
        assert pad_release_date('2020-06') == '2020-06-01'

    def test_day_precision_unchanged(self):
        assert pad_release_date('2020-06-15') == '2020-06-15'

    def test_year_precision_compares_as_same_day_not_earlier(self):
        # Raw string compare would rank "2020" before "2020-01-01"
        assert pad_release_date('2020') == pad_release_date('2020-01-01')

    def test_empty_sorts_first(self):
        assert pad_release_date('') < pad_release_date('1900')


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

    def test_album_beats_newer_same_named_single(self, client):
        # Title tracks are routinely released as a single ahead of the album.
        client.sp._get.return_value = {
            'items': [
                {'id': 'sgl', 'name': 'Stay Golden', 'release_date': '2024-09-01',
                 'release_date_precision': 'day', 'album_type': 'single'},
                {'id': 'alb', 'name': 'Stay Golden', 'release_date': '2024-08-16',
                 'release_date_precision': 'day', 'album_type': 'album'},
            ],
            'next': None,
        }
        result = client._get_artist_albums('artist1')
        assert [a['id'] for a in result] == ['alb']

    def test_differently_named_single_is_kept_alongside_the_album(self, client):
        # Only same-name collisions collapse; a separate single stays in the
        # pool so its non-album tracks are still reachable.
        client.sp._get.return_value = {
            'items': [
                {'id': 'alb', 'name': 'Stay Golden', 'release_date': '2024-08-16',
                 'release_date_precision': 'day', 'album_type': 'album'},
                {'id': 'sgl', 'name': 'Epic Season Finale', 'release_date': '2024-07-16',
                 'release_date_precision': 'day', 'album_type': 'single'},
            ],
            'next': None,
        }
        result = client._get_artist_albums('artist1')
        assert {a['id'] for a in result} == {'alb', 'sgl'}


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

    def test_album_cut_beats_older_single_cut(self, client):
        # Regression: oldest-wins alone handed the song to the pre-release
        # single. Release type is checked first, date only breaks ties.
        client._get_artist_albums = MagicMock(return_value=[
            {'id': 'alb', 'name': 'Stay Golden', 'release_date': '2024-08-16',
             'release_date_precision': 'day', 'album_type': 'album'},
            {'id': 'sgl', 'name': 'Epic Season Finale', 'release_date': '2024-07-16',
             'release_date_precision': 'day', 'album_type': 'single'},
        ])

        def fake_get_album_tracks(album_id):
            return {
                'alb': [{'id': 't_album', 'name': 'Stay Golden', 'duration_ms': 118595}],
                'sgl': [{'id': 't_single', 'name': 'Stay Golden', 'duration_ms': 118595}],
            }.get(album_id, [])

        client._get_album_tracks = MagicMock(side_effect=fake_get_album_tracks)

        result = client._fetch_artist_tracks('artist1')
        assert len(result) == 1
        assert result[0].id == 't_album'
        assert result[0].album_name == 'Stay Golden'
        assert result[0].album_type == 'album'

    def test_oldest_wins_within_the_same_release_type(self, client):
        # The anti-re-recording rule survives: same type → older pressing wins.
        client._get_artist_albums = MagicMock(return_value=[
            {'id': 'alb_new', 'name': 'Rerecorded', 'release_date': '2023-01-01',
             'release_date_precision': 'day', 'album_type': 'album'},
            {'id': 'alb_old', 'name': 'Original', 'release_date': '2008-01-01',
             'release_date_precision': 'day', 'album_type': 'album'},
        ])

        def fake_get_album_tracks(album_id):
            return {
                'alb_new': [{'id': 't_new', 'name': 'Song', 'duration_ms': 1000}],
                'alb_old': [{'id': 't_old', 'name': 'Song', 'duration_ms': 1000}],
            }.get(album_id, [])

        client._get_album_tracks = MagicMock(side_effect=fake_get_album_tracks)

        result = client._fetch_artist_tracks('artist1')
        assert [t.id for t in result] == ['t_old']

    def test_year_precision_album_not_treated_as_older_than_same_year_album(self, client):
        # "2020" vs "2020-01-01" denote the same release date; raw string
        # compare would call the year-precision one older and let it win.
        client._get_artist_albums = MagicMock(return_value=[
            {'id': 'day_prec', 'name': 'A', 'release_date': '2020-01-01',
             'release_date_precision': 'day', 'album_type': 'album'},
            {'id': 'year_prec', 'name': 'B', 'release_date': '2020',
             'release_date_precision': 'year', 'album_type': 'album'},
        ])

        def fake_get_album_tracks(album_id):
            return {
                'day_prec': [{'id': 't_day', 'name': 'Song', 'duration_ms': 1000}],
                'year_prec': [{'id': 't_year', 'name': 'Song', 'duration_ms': 1000}],
            }.get(album_id, [])

        client._get_album_tracks = MagicMock(side_effect=fake_get_album_tracks)

        result = client._fetch_artist_tracks('artist1')
        # Tie on both type and padded date → first seen wins, not the year-precision one
        assert [t.id for t in result] == ['t_day']



# ── get_artist_names ──────────────────────────────────────────────────────────

@pytest.fixture
def cache(tmp_path):
    from cache import Cache
    return Cache(db_path=tmp_path / 'test.db')


class TestGetArtistNames:
    def test_returns_canonical_names(self, client, cache):
        client.sp.artist.side_effect = lambda aid: {
            'a1': {'id': 'a1', 'name': 'Prince Daddy & the Hyena'},
            'a2': {'id': 'a2', 'name': 'Walter Etc.'},
        }[aid]

        assert client.get_artist_names(['a1', 'a2'], cache) == {
            'a1': 'Prince Daddy & the Hyena',
            'a2': 'Walter Etc.',
        }

    def test_second_call_is_served_from_cache(self, client, cache):
        client.sp.artist.return_value = {'id': 'a1', 'name': 'Combat'}

        client.get_artist_names(['a1'], cache)
        client.get_artist_names(['a1'], cache)

        assert client.sp.artist.call_count == 1

    def test_failed_lookup_is_omitted_not_cached(self, client, cache):
        client.sp.artist.side_effect = Exception('403 Forbidden')

        assert client.get_artist_names(['a1'], cache) == {}

        # A later run must retry rather than inherit the failure
        client.sp.artist.side_effect = None
        client.sp.artist.return_value = {'id': 'a1', 'name': 'Combat'}
        assert client.get_artist_names(['a1'], cache) == {'a1': 'Combat'}


# ── get_artist_top_scores (rank-aware) ────────────────────────────────────────

def _top_artists_response(names_by_range):
    """Serve current_user_top_artists from {time_range: [artist_name, ...]}."""
    def _fetch(limit=50, time_range='short_term'):
        return {'items': [
            {'id': n, 'name': n} for n in names_by_range.get(time_range, [])
        ]}
    return _fetch


class TestGetArtistTopScores:
    def test_rank_one_gets_the_tier_ceiling(self, client, cache):
        client.sp.current_user_top_artists = _top_artists_response(
            {'short_term': ['first', 'second', 'third']}
        )
        scores = client.get_artist_top_scores(cache)
        assert abs(scores['first'].score - 1.0) < 1e-9

    def test_last_rank_falls_a_full_span_below_the_ceiling(self, client, cache):
        client.sp.current_user_top_artists = _top_artists_response(
            {'short_term': ['first', 'second', 'third']}
        )
        scores = client.get_artist_top_scores(cache)
        assert abs(scores['third'].score - (1.0 - TOP_ARTIST_RANK_SPAN)) < 1e-9

    def test_score_decreases_with_rank(self, client, cache):
        # The whole point: artists sharing a tier must not share a score.
        names = [f'a{i}' for i in range(50)]
        client.sp.current_user_top_artists = _top_artists_response({'short_term': names})
        scores = client.get_artist_top_scores(cache)
        ranked = [scores[n].score for n in names]
        assert all(a > b for a, b in zip(ranked, ranked[1:]))

    def test_single_item_list_gets_the_ceiling(self, client, cache):
        client.sp.current_user_top_artists = _top_artists_response({'short_term': ['solo']})
        scores = client.get_artist_top_scores(cache)
        assert abs(scores['solo'].score - 1.0) < 1e-9

    def test_higher_band_wins_across_overlapping_tiers(self, client, cache):
        # 'x' is last of fifty short-term (1.0 - span = 0.70) but #1 medium-term
        # (0.80). The bands overlap, so first-tier-wins would understate it.
        short = [f'a{i}' for i in range(49)] + ['x']
        client.sp.current_user_top_artists = _top_artists_response(
            {'short_term': short, 'medium_term': ['x', 'y']}
        )
        scores = client.get_artist_top_scores(cache)
        assert abs(scores['x'].score - 0.8) < 1e-9

    def test_lower_band_does_not_displace_a_higher_one(self, client, cache):
        client.sp.current_user_top_artists = _top_artists_response(
            {'short_term': ['x', 'y'], 'long_term': ['x', 'y']}
        )
        scores = client.get_artist_top_scores(cache)
        assert abs(scores['x'].score - 1.0) < 1e-9

    def test_name_is_kept_alongside_the_score(self, client, cache):
        client.sp.current_user_top_artists = _top_artists_response({'short_term': ['solo']})
        assert client.get_artist_top_scores(cache)['solo'].name == 'solo'

    def test_second_call_is_served_from_cache(self, client, cache):
        client.sp.current_user_top_artists = _top_artists_response({'short_term': ['a', 'b']})
        first = client.get_artist_top_scores(cache)

        client.sp.current_user_top_artists = _top_artists_response({'short_term': ['z']})
        assert client.get_artist_top_scores(cache) == first

    def test_api_failure_is_survivable(self, client, cache):
        def _boom(limit=50, time_range='short_term'):
            if time_range == 'short_term':
                raise RuntimeError('429')
            return {'items': [{'id': 'm', 'name': 'm'}]}
        client.sp.current_user_top_artists = _boom
        scores = client.get_artist_top_scores(cache)
        assert 'm' in scores and abs(scores['m'].score - 0.8) < 1e-9
