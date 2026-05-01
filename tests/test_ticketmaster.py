"""
Tests for sources/ticketmaster.py — pure functions and mocked HTTP calls only.
No real Ticketmaster API calls are made.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from sources.models import Concert
from sources.ticketmaster import (
    TicketmasterClient,
    _extract_spotify_id,
    _normalize_venue,
    _venue_matches,
    enrich_with_openers,
)


# ── _normalize_venue ───────────────────────────────────────────────────────────

class TestNormalizeVenue:
    def test_lowercases(self):
        assert _normalize_venue("The Fillmore") == "thefillmore"

    def test_strips_spaces_and_punctuation(self):
        assert _normalize_venue("The Fox Theatre!") == "thefoxtheatre"

    def test_strips_commas(self):
        assert _normalize_venue("Fillmore, San Francisco") == "fillmoresanfrancisco"

    def test_empty_string(self):
        assert _normalize_venue("") == ""

    def test_already_plain(self):
        assert _normalize_venue("fillmore") == "fillmore"


# ── _venue_matches ─────────────────────────────────────────────────────────────

class TestVenueMatches:
    def test_exact_match(self):
        assert _venue_matches("The Fillmore", "The Fillmore")

    def test_cal_venue_contained_in_tm_venue(self):
        # Calendar often has just the name; TM adds city/state
        assert _venue_matches("Fillmore", "The Fillmore SF")

    def test_tm_venue_contained_in_cal_venue(self):
        # Calendar includes full address, TM just the name
        assert _venue_matches("The Fillmore, San Francisco, CA", "The Fillmore")

    def test_no_match(self):
        assert not _venue_matches("The Fillmore", "Madison Square Garden")

    def test_empty_cal_venue_defaults_true(self):
        assert _venue_matches("", "The Fillmore")

    def test_empty_tm_venue_defaults_true(self):
        assert _venue_matches("The Fillmore", "")

    def test_unknown_venue_defaults_true(self):
        # Calendar may populate "Unknown Venue" when no location is set
        assert _venue_matches("Unknown Venue", "The Fillmore")


# ── _extract_spotify_id ────────────────────────────────────────────────────────

class TestExtractSpotifyId:
    def test_https_url(self):
        assert _extract_spotify_id(
            "https://open.spotify.com/artist/abc123XYZ"
        ) == "abc123XYZ"

    def test_spotify_uri(self):
        assert _extract_spotify_id("spotify:artist:def456") == "def456"

    def test_returns_none_for_unrecognised_url(self):
        assert _extract_spotify_id("https://example.com/artist/foo") is None

    def test_returns_none_for_empty_string(self):
        assert _extract_spotify_id("") is None

    def test_alphanumeric_id_preserved(self):
        assert _extract_spotify_id(
            "https://open.spotify.com/artist/0OdUWJ0sBjDrqHygGUXeCF"
        ) == "0OdUWJ0sBjDrqHygGUXeCF"


# ── TicketmasterClient._best_event ─────────────────────────────────────────────

class TestBestEvent:
    def _client(self):
        return TicketmasterClient(api_key='fake', cache=MagicMock())

    def _event(self, artist_names, venue_name="The Fillmore"):
        return {
            'name': f'{artist_names[0]} Tour',
            '_embedded': {
                'attractions': [{'name': n} for n in artist_names],
                'venues': [{'name': venue_name}],
            },
        }

    def test_returns_event_when_headliner_present(self):
        client = self._client()
        events = [self._event(['Good Kid', 'INOHA'])]
        result = client._best_event(events, 'Good Kid', 'The Fillmore')
        assert result is not None

    def test_returns_none_when_headliner_absent(self):
        client = self._client()
        events = [self._event(['Some Other Artist'])]
        result = client._best_event(events, 'Good Kid', 'The Fillmore')
        assert result is None

    def test_prefers_event_with_venue_match(self):
        client = self._client()
        event_wrong_venue = self._event(['Good Kid'], venue_name='Madison Square Garden')
        event_right_venue = self._event(['Good Kid'], venue_name='The Fillmore')
        result = client._best_event(
            [event_wrong_venue, event_right_venue], 'Good Kid', 'The Fillmore'
        )
        assert result is event_right_venue

    def test_case_insensitive_headliner_match(self):
        client = self._client()
        events = [self._event(['good kid', 'INOHA'])]
        result = client._best_event(events, 'Good Kid', 'The Fillmore')
        assert result is not None

    def test_partial_name_match_accepted(self):
        # TM may store "Good Kid - Can We Hang Out? Tour" as the attraction name
        client = self._client()
        events = [self._event(['Good Kid - Can We Hang Out? Tour'])]
        result = client._best_event(events, 'Good Kid', 'The Fillmore')
        assert result is not None

    def test_empty_events_returns_none(self):
        assert self._client()._best_event([], 'Good Kid', 'The Fillmore') is None


# ── TicketmasterClient._extract_openers ───────────────────────────────────────

class TestExtractOpeners:
    def _client(self):
        return TicketmasterClient(api_key='fake', cache=MagicMock())

    def _event(self, attractions):
        return {
            'name': 'Good Kid Tour',
            '_embedded': {'attractions': attractions},
        }

    def test_headliner_excluded_from_openers(self):
        client = self._client()
        event = self._event([
            {'name': 'Good Kid'},
            {'name': 'INOHA'},
        ])
        openers = client._extract_openers(event, 'Good Kid', date(2026, 4, 26), 'The Fillmore')
        names = [c.artist_name for c in openers]
        assert 'Good Kid' not in names
        assert 'INOHA' in names

    def test_returns_concert_objects(self):
        client = self._client()
        event = self._event([{'name': 'Good Kid'}, {'name': 'INOHA'}])
        openers = client._extract_openers(event, 'Good Kid', date(2026, 4, 26), 'The Fillmore')
        assert all(isinstance(c, Concert) for c in openers)

    def test_source_is_ticketmaster(self):
        client = self._client()
        event = self._event([{'name': 'Good Kid'}, {'name': 'INOHA'}])
        openers = client._extract_openers(event, 'Good Kid', date(2026, 4, 26), 'The Fillmore')
        assert all(c.source == 'ticketmaster' for c in openers)

    def test_spotify_id_extracted_from_link(self):
        client = self._client()
        event = self._event([
            {'name': 'Good Kid'},
            {
                'name': 'INOHA',
                'externalLinks': {
                    'spotify': [{'url': 'https://open.spotify.com/artist/inoha123'}]
                },
            },
        ])
        openers = client._extract_openers(event, 'Good Kid', date(2026, 4, 26), 'The Fillmore')
        assert openers[0].tm_spotify_id == 'inoha123'

    def test_no_spotify_link_gives_none(self):
        client = self._client()
        event = self._event([{'name': 'Good Kid'}, {'name': 'INOHA'}])
        openers = client._extract_openers(event, 'Good Kid', date(2026, 4, 26), 'The Fillmore')
        assert openers[0].tm_spotify_id is None

    def test_empty_attractions_returns_empty(self):
        client = self._client()
        event = self._event([])
        assert client._extract_openers(event, 'Good Kid', date(2026, 4, 26), 'The Fillmore') == []

    def test_event_date_and_venue_propagated(self):
        client = self._client()
        event_date = date(2026, 4, 26)
        event = self._event([{'name': 'Good Kid'}, {'name': 'INOHA'}])
        openers = client._extract_openers(event, 'Good Kid', event_date, 'The Fillmore')
        assert openers[0].event_date == event_date
        assert openers[0].venue == 'The Fillmore'


# ── TicketmasterClient.lookup_openers (HTTP mocked) ───────────────────────────

class TestLookupOpeners:
    def _tm_response(self, artist_names, venue='The Fillmore'):
        return {
            '_embedded': {
                'events': [{
                    'name': f'{artist_names[0]} Tour',
                    '_embedded': {
                        'attractions': [{'name': n} for n in artist_names],
                        'venues': [{'name': venue}],
                    },
                }]
            }
        }

    def test_cache_hit_skips_http_call(self, tmp_path):
        from cache import Cache
        cache = Cache(db_path=tmp_path / 'test.db')
        cache.set(
            'tm_openers:good kid:2026-04-26',
            [{'event_name': 'Good Kid Tour', 'name': 'INOHA', 'spotify_id': None}],
            3600,
        )
        client = TicketmasterClient(api_key='fake', cache=cache)

        with patch('sources.ticketmaster.requests.get') as mock_get:
            openers = client.lookup_openers('Good Kid', date(2026, 4, 26), 'The Fillmore')
            mock_get.assert_not_called()

        assert len(openers) == 1
        assert openers[0].artist_name == 'INOHA'

    def test_cache_miss_makes_http_call_and_caches(self, tmp_path):
        from cache import Cache
        cache = Cache(db_path=tmp_path / 'test.db')
        client = TicketmasterClient(api_key='fake', cache=cache)

        mock_resp = MagicMock()
        mock_resp.json.return_value = self._tm_response(['Good Kid', 'INOHA'])

        with patch('sources.ticketmaster.requests.get', return_value=mock_resp):
            openers = client.lookup_openers('Good Kid', date(2026, 4, 26), 'The Fillmore')

        assert len(openers) == 1
        assert openers[0].artist_name == 'INOHA'

        # Result cached — second call must not hit network
        with patch('sources.ticketmaster.requests.get') as mock_get2:
            openers2 = client.lookup_openers('Good Kid', date(2026, 4, 26), 'The Fillmore')
            mock_get2.assert_not_called()
        assert len(openers2) == 1

    def test_api_error_returns_empty_and_does_not_cache(self, tmp_path):
        import requests as req
        from cache import Cache
        cache = Cache(db_path=tmp_path / 'test.db')
        client = TicketmasterClient(api_key='fake', cache=cache)

        with patch('sources.ticketmaster.requests.get', side_effect=req.RequestException('timeout')):
            openers = client.lookup_openers('Good Kid', date(2026, 4, 26), 'The Fillmore')

        assert openers == []
        # Nothing cached — next run will retry
        assert cache.get('tm_openers:good kid:2026-04-26') is None

    def test_no_matching_event_returns_empty_and_caches(self, tmp_path):
        from cache import Cache
        cache = Cache(db_path=tmp_path / 'test.db')
        client = TicketmasterClient(api_key='fake', cache=cache)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'_embedded': {'events': []}}

        with patch('sources.ticketmaster.requests.get', return_value=mock_resp):
            openers = client.lookup_openers('Good Kid', date(2026, 4, 26), 'The Fillmore')

        assert openers == []
        # Empty result IS cached (event checked, no openers exist)
        assert cache.get('tm_openers:good kid:2026-04-26') == []


# ── enrich_with_openers ────────────────────────────────────────────────────────

class TestEnrichWithOpeners:
    def _concert(self, artist, event_name='Good Kid @ The Fillmore',
                 event_date=date(2026, 4, 26), source='apple_calendar'):
        return Concert(
            event_name=event_name,
            artist_name=artist,
            event_date=event_date,
            venue='The Fillmore',
            source=source,
        )

    def test_single_artist_event_gets_enriched(self):
        concerts = [self._concert('Good Kid')]
        mock_client = MagicMock()
        mock_client.lookup_openers.return_value = [
            self._concert('INOHA', source='ticketmaster')
        ]
        result = enrich_with_openers(concerts, mock_client)
        assert len(result) == 2
        assert any(c.artist_name == 'INOHA' for c in result)

    def test_multi_artist_event_not_re_queried(self):
        concerts = [
            self._concert('Mitski'),
            self._concert('boygenius'),  # same event_name + date
        ]
        mock_client = MagicMock()
        result = enrich_with_openers(concerts, mock_client)
        mock_client.lookup_openers.assert_not_called()
        assert len(result) == 2

    def test_ticketmaster_sourced_concerts_not_re_queried(self):
        concerts = [self._concert('INOHA', source='ticketmaster')]
        mock_client = MagicMock()
        enrich_with_openers(concerts, mock_client)
        mock_client.lookup_openers.assert_not_called()

    def test_original_concerts_preserved(self):
        concerts = [self._concert('Good Kid')]
        mock_client = MagicMock()
        mock_client.lookup_openers.return_value = []
        result = enrich_with_openers(concerts, mock_client)
        assert result[0].artist_name == 'Good Kid'

    def test_two_separate_events_each_enriched(self):
        concert_a = self._concert('Good Kid', event_name='Good Kid @ Fillmore',
                                  event_date=date(2026, 4, 26))
        concert_b = Concert(
            event_name='Mitski @ Hollywood Bowl',
            artist_name='Mitski',
            event_date=date(2026, 5, 10),
            venue='Hollywood Bowl',
            source='apple_calendar',
        )
        mock_client = MagicMock()
        mock_client.lookup_openers.return_value = []
        enrich_with_openers([concert_a, concert_b], mock_client)
        assert mock_client.lookup_openers.call_count == 2

    def test_opener_inherits_headliner_event_name(self):
        # Openers are part of the same calendar event, not a separate one
        headliner = self._concert('Good Kid', event_name='Ticket: Good Kid @ The Fillmore')
        opener_from_tm = Concert(
            event_name='Good Kid - Can We Hang Out? Tour',  # TM event name
            artist_name='INOHA',
            event_date=date(2026, 4, 26),
            venue='The Fillmore',
            source='ticketmaster',
        )
        mock_client = MagicMock()
        mock_client.lookup_openers.return_value = [opener_from_tm]

        result = enrich_with_openers([headliner], mock_client)

        inoha = next(c for c in result if c.artist_name == 'INOHA')
        assert inoha.event_name == 'Ticket: Good Kid @ The Fillmore'

    def test_empty_concert_list(self):
        mock_client = MagicMock()
        result = enrich_with_openers([], mock_client)
        assert result == []
        mock_client.lookup_openers.assert_not_called()
