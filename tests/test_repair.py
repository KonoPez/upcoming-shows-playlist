"""
Tests for the repair package: pins, the resolver/setlist hooks that honour
them, resolution inspection, per-artist refresh and the signal audit.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from artist_resolver import UNRESOLVED_SENTINEL, resolve_artist
from cache import Cache
from repair import pins
from repair.audit import audit_artist, fold, possible_duplicates, unmatched_names
from repair.lookup import current_resolution, latest_releases, rank_candidates, resolution_rows
from repair.refresh import refresh_artist
from sources.models import Concert, Track
from sources.setlist import SetlistClient
from spotify_client.client import _artist_name_key, _artist_tracks_key

DAY = 24 * 3600


@pytest.fixture
def cache(tmp_path):
    return Cache(db_path=tmp_path / 'test.db')


def _track(name: str, album: str = 'Album', release_date: str = '2024-01-01', album_id: str = '') -> Track:
    return Track(
        id=name.lower().replace(' ', '_'),
        name=name,
        release_date=release_date,
        release_date_precision='day',
        duration_ms=200_000,
        album_id=album_id or album.lower(),
        album_name=album,
    )


def _concert(artist: str, tm_spotify_id: str = '', is_opener: bool = False) -> Concert:
    return Concert(
        event_name='Ticket: Lowboy // Charlieboy',
        artist_name=artist,
        event_date=date(2026, 12, 4),
        venue='Gamma Ray Bar',
        source='google_calendar',
        tm_spotify_id=tm_spotify_id,
        is_opener=is_opener,
    )


# ── Pin storage ───────────────────────────────────────────────────────────────

class TestArtistPins:
    def test_lookup_ignores_case_and_padding(self, cache):
        pins.pin_artist(cache, 'Lowboy', 'real-id')
        assert pins.lookup_artist(cache, '  LOWBOY ').artist_id == 'real-id'

    def test_none_pin_has_no_artist(self, cache):
        pins.pin_artist(cache, 'Paperback', None)
        pin = pins.lookup_artist(cache, 'Paperback')
        assert pin is not None and pin.artist_id is None

    def test_pin_ignored_after_until(self, cache):
        pins.pin_artist(cache, 'Paperback', None, until=date(2026, 12, 17))
        assert pins.lookup_artist(cache, 'Paperback', today=date(2026, 12, 17)) is not None
        assert pins.lookup_artist(cache, 'Paperback', today=date(2026, 12, 18)) is None

    def test_expired_pins_still_listed(self, cache):
        pins.pin_artist(cache, 'Paperback', None, until=date(2020, 1, 1))
        assert [p.name for p in pins.list_artist_pins(cache)] == ['Paperback']

    def test_repin_replaces(self, cache):
        pins.pin_artist(cache, 'Lowboy', 'wrong')
        pins.pin_artist(cache, 'lowboy', 'right')
        assert pins.lookup_artist(cache, 'Lowboy').artist_id == 'right'
        assert len(pins.list_artist_pins(cache)) == 1

    def test_unpin(self, cache):
        pins.pin_artist(cache, 'Lowboy', 'id')
        assert pins.unpin_artist(cache, 'LOWBOY')
        assert pins.lookup_artist(cache, 'Lowboy') is None
        assert not pins.unpin_artist(cache, 'Lowboy')

    def test_pins_survive_clear_cache(self, cache):
        # The reason pins have their own table: --clear-cache must not undo them.
        pins.pin_artist(cache, 'Lowboy', 'id')
        pins.pin_setlist_mbid(cache, 'id', 'mbid')
        cache.clear_all()
        cache.clear_prefixes(['artist_resolve:'])
        assert pins.lookup_artist(cache, 'Lowboy').artist_id == 'id'
        assert pins.lookup_setlist_mbid(cache, 'id') == 'mbid'

    def test_setlist_mbid_roundtrip(self, cache):
        assert pins.lookup_setlist_mbid(cache, 'id') is None
        pins.pin_setlist_mbid(cache, 'id', 'mbid-1')
        assert pins.list_setlist_mbid_pins(cache) == {'id': 'mbid-1'}
        assert pins.unpin_setlist_mbid(cache, 'id')
        assert pins.lookup_setlist_mbid(cache, 'id') is None


# ── Hooks in the pipeline ─────────────────────────────────────────────────────

class TestResolverHonoursPins:
    def test_pin_beats_cached_resolution(self, cache):
        cache.set('artist_resolve:lowboy', 'namesake-id', DAY)
        pins.pin_artist(cache, 'Lowboy', 'real-id')
        sp = MagicMock()
        assert resolve_artist('Lowboy', sp, cache) == 'real-id'
        sp.search.assert_not_called()

    def test_pin_beats_ticketmaster_link(self, cache):
        pins.pin_artist(cache, 'Lowboy', 'real-id')
        sp = MagicMock()
        assert resolve_artist('Lowboy', sp, cache, tm_spotify_id='tm-id') == 'real-id'
        sp.artist.assert_not_called()

    def test_none_pin_resolves_to_nothing_without_searching(self, cache):
        pins.pin_artist(cache, 'Paperback', None)
        sp = MagicMock()
        assert resolve_artist('Paperback', sp, cache) is None
        sp.search.assert_not_called()

    def test_alias_pins_share_one_artist(self, cache):
        # Sheebie is billed both ways; only one spelling exists on Spotify.
        pins.pin_artist(cache, 'Sheebie & the Lovemakers', 'sheebie-id')
        pins.pin_artist(cache, 'Sheebie', 'sheebie-id')
        sp = MagicMock()
        assert resolve_artist('Sheebie & The Lovemakers', sp, cache) == 'sheebie-id'
        assert resolve_artist('SHEEBIE', sp, cache) == 'sheebie-id'


class TestSetlistMbidPin:
    def _response(self, setlists):
        resp = MagicMock()
        resp.json.return_value = {'setlist': setlists}
        resp.raise_for_status.return_value = None
        return resp

    def _setlist(self, mbid, song, name='Lowboy'):
        return {
            'artist': {'name': name, 'mbid': mbid},
            'eventDate': date.today().strftime('%d-%m-%Y'),
            'sets': {'set': [{'song': [{'name': song}]}]},
        }

    def test_pinned_mbid_queries_by_mbid(self):
        client = SetlistClient('key', MagicMock(get=MagicMock(return_value=None)))
        resp = self._response([self._setlist('local', 'Sunspot')])
        with patch('sources.setlist.requests.get', return_value=resp) as get, patch('sources.setlist.time.sleep'):
            scores = client.get_setlist_scores('Lowboy', mbid='local')
        assert get.call_args.kwargs['params'] == {'artistMbid': 'local', 'p': 1}
        assert scores == {'sunspot': 1.0}

    def test_pinned_mbid_overrides_the_majority_namesake(self):
        # The namesake has more setlists, so the name-based pick would choose it.
        client = SetlistClient('key', MagicMock(get=MagicMock(return_value=None)))
        resp = self._response([
            self._setlist('famous', 'Hit'), self._setlist('famous', 'Hit'), self._setlist('local', 'Sunspot'),
        ])
        with patch('sources.setlist.requests.get', return_value=resp), patch('sources.setlist.time.sleep'):
            assert client.get_setlist_scores('Lowboy', mbid='local') == {'sunspot': 1.0}


# ── Inspection ────────────────────────────────────────────────────────────────

class TestCurrentResolution:
    def test_methods(self, cache):
        assert current_resolution(cache, 'Nobody') == (None, 'not yet resolved')
        cache.set('artist_resolve:gone', UNRESOLVED_SENTINEL, DAY)
        assert current_resolution(cache, 'Gone') == (None, 'unresolved')
        cache.set('artist_resolve:found', 'id-1', DAY)
        assert current_resolution(cache, 'Found') == ('id-1', 'search')
        assert current_resolution(cache, 'Found', tm_spotify_id='id-1') == ('id-1', 'ticketmaster link')
        pins.pin_artist(cache, 'Found', 'id-2')
        assert current_resolution(cache, 'Found') == ('id-2', 'pinned')
        pins.pin_artist(cache, 'Gone', None)
        assert current_resolution(cache, 'Gone') == (None, 'pinned: no artist')


class TestResolutionRows:
    def test_row_shows_title_parse_and_target(self, cache):
        cache.set('artist_resolve:lowboy', 'id-1', DAY)
        cache.set(_artist_name_key('id-1'), 'LOWBOY', DAY)
        cache.set(_artist_tracks_key('id-1'), [_track('Yard Sale', 'Yard Sale', '2026-09-04').to_dict()], DAY)

        [row] = resolution_rows([_concert('Lowboy')], cache)
        assert row['event'] == 'Ticket: Lowboy // Charlieboy'
        assert row['billed_as'] == 'Lowboy'
        assert row['spotify_name'] == 'LOWBOY'
        assert row['latest_releases'] == ['Yard Sale (2026)']

    def test_unresolved_row(self, cache):
        [row] = resolution_rows([_concert('Charlieboy', is_opener=True)], cache)
        assert row['method'] == 'not yet resolved'
        assert row['artist_id'] is None and row['latest_releases'] is None
        assert row['role'] == 'opener'

    def test_venue_newlines_flattened(self, cache):
        c = _concert('Lowboy')
        c.venue = 'High Noon Saloon\nMadison, WI'
        assert resolution_rows([c], cache)[0]['venue'] == 'High Noon Saloon, Madison, WI'


class TestLatestReleases:
    def test_newest_first_one_per_album(self):
        tracks = [
            _track('A', 'Old', '2020-01-01'),
            _track('B', 'Old', '2020-01-01'),
            _track('C', 'New', '2026'),
            _track('D', 'Mid', '2023-05-01'),
        ]
        assert latest_releases(tracks, n=2) == ['New (2026)', 'Mid (2023)']


class TestRankCandidates:
    def test_exact_matches_first_unrelated_dropped(self):
        items = [
            {'id': '1', 'name': 'Lowboy Fatz'},
            {'id': '2', 'name': 'Loverboy'},
            {'id': '3', 'name': 'lowboy'},
            {'id': '4', 'name': 'LOWBOY'},
            {'id': '3', 'name': 'lowboy'},   # repeated across the two queries
        ]
        ranked = rank_candidates('Lowboy', items)
        assert [c['artist_id'] for c in ranked] == ['3', '4', '1']

    def test_limit(self):
        items = [{'id': str(i), 'name': 'Lowboy'} for i in range(20)]
        assert len(rank_candidates('Lowboy', items, limit=5)) == 5


# ── Refresh ───────────────────────────────────────────────────────────────────

class TestRefreshArtist:
    def test_drops_artist_data_keeps_resolution(self, cache):
        cache.set('artist_resolve:lowboy', 'id-1', DAY)
        cache.set(_artist_name_key('id-1'), 'LOWBOY', DAY)
        cache.set(_artist_tracks_key('id-1'), [], DAY)
        cache.set('setlist:lowboy', {}, DAY)
        cache.set('lastfm:lowboy', {'x': 1.0}, DAY)
        cache.set('lastfm_listeners:lowboy', -1, DAY)
        cache.set('tm_openers:lowboy:2026-12-04', [], DAY)
        cache.set('lastfm:charlieboy', {'y': 1.0}, DAY)

        result = refresh_artist(cache, 'Lowboy')

        assert set(result['removed']) == {
            _artist_name_key('id-1'), _artist_tracks_key('id-1'), 'setlist:lowboy',
            'lastfm:lowboy', 'lastfm_listeners:lowboy', 'tm_openers:lowboy:2026-12-04',
        }
        assert cache.get('artist_resolve:lowboy') == 'id-1'
        assert cache.get('lastfm:charlieboy') == {'y': 1.0}

    def test_covers_canonical_name(self, cache):
        # The calendar says "Sheebie & the Lovemakers"; scores are keyed by Spotify's "SHEEBIE".
        pins.pin_artist(cache, 'Sheebie & the Lovemakers', 'id-s')
        cache.set(_artist_name_key('id-s'), 'SHEEBIE', DAY)
        cache.set('lastfm:sheebie', {}, DAY)
        assert 'lastfm:sheebie' in refresh_artist(cache, 'Sheebie & the Lovemakers')['removed']

    def test_drops_cached_failure(self, cache):
        cache.set('artist_resolve:gone', UNRESOLVED_SENTINEL, DAY)
        assert refresh_artist(cache, 'Gone')['removed'] == ['artist_resolve:gone']


# ── Audit ─────────────────────────────────────────────────────────────────────

class TestAudit:
    def test_curly_apostrophe_folds_to_track(self):
        # Issue #7: setlist.fm writes ’ where Spotify writes '.
        [miss] = unmatched_names({'i don’t work here': 1.0}, [_track("I Don't Work Here")])
        assert miss['folds_to'] == "I Don't Work Here"

    def test_live_suffix_on_stored_side_is_a_miss(self):
        # Issue #5: lookups normalise the Spotify title but not the stored name,
        # so the audit must not normalise it either or it would hide the miss.
        [miss] = unmatched_names(
            {'sunspot (live at gamma ray)': 0.6}, [_track('Sunspot (Live at Gamma Ray)')]
        )
        assert miss['folds_to'] == 'Sunspot (Live at Gamma Ray)'

    def test_plain_stored_name_matches_suffixed_track(self):
        assert unmatched_names({'sunspot': 0.6}, [_track('Sunspot (Live at Gamma Ray)')]) == []

    def test_punctuation_difference_folds(self):
        [miss] = unmatched_names({'lazy sunny': 0.5}, [_track('Lazy / Sunny')])
        assert miss['folds_to'] == 'Lazy / Sunny'

    def test_near_spelling_is_close_to(self):
        [miss] = unmatched_names({'milwaukie': 0.9}, [_track('Milwaukee')])
        assert miss == {'stored_as': 'milwaukie', 'score': 0.9, 'close_to': 'Milwaukee'}

    def test_song_not_in_discography(self):
        [miss] = unmatched_names({'in the street': 0.5}, [_track('Milwaukee')])
        assert 'folds_to' not in miss and 'close_to' not in miss

    def test_unmatched_sorted_by_score(self):
        misses = unmatched_names({'a': 0.1, 'b': 0.9}, [_track('Other')])
        assert [m['stored_as'] for m in misses] == ['b', 'a']

    def test_possible_duplicates_only_reports_key_misses(self):
        tracks = [
            _track('Song'), _track('Song (Live)'),            # same key: dedup handles it
            _track('Other'), _track('Other - 2019 Remaster'),  # different keys: reported
        ]
        assert possible_duplicates(tracks) == [['Other', 'Other - 2019 Remaster']]

    def test_tracks_without_signal(self):
        result = audit_artist([_track('Hit'), _track('Deep Cut')], {'hit': 1.0}, {})
        assert result['tracks_without_signal'] == ['Deep Cut']

    def test_fold(self):
        assert fold('Something’s Gotta  Give!') == "something's gotta give"
