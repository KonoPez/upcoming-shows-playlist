"""
Tests for artist_resolver.py — pure-function logic only, no Spotify API calls.
"""

import sqlite3
import time
from unittest.mock import MagicMock

import pytest

from artist_resolver import (
    RESOLUTION_TTL,
    UNRESOLVED_SENTINEL,
    UNRESOLVED_TTL,
    _normalize,
    _search_spotify,
    extract_artist_from_calendar_title,
    is_likely_concert,
    resolve_artist,
    split_artist_names,
)
from cache import Cache


class TestExtractArtistFromCalendarTitle:
    # ── Bug-fix regression cases ─────────────────────────────────────────────

    def test_ticket_prefix_with_dash_tour(self):
        # "Ticket: Artist - Tour Name" → should extract the artist, not "Ticket"
        assert extract_artist_from_calendar_title(
            "Ticket: Good Kid - Can We Hang Out? Tour"
        ) == "Good Kid"

    def test_ticket_prefix_artist_only(self):
        # "Ticket: Artist" with no venue or tour suffix → full post-prefix string
        assert extract_artist_from_calendar_title(
            "Ticket: Black Country, New Road"
        ) == "Black Country, New Road"

    # ── Ticket/Tickets prefix variants ───────────────────────────────────────

    def test_tickets_plural_prefix(self):
        assert extract_artist_from_calendar_title(
            "Tickets: boygenius - Summer Tour"
        ) == "boygenius"

    def test_ticket_prefix_case_insensitive(self):
        assert extract_artist_from_calendar_title(
            "TICKET: Mitski @ The Fillmore"
        ) == "Mitski"

    def test_ticket_prefix_with_at_venue(self):
        assert extract_artist_from_calendar_title(
            "Ticket: Hozier @ The Forum"
        ) == "Hozier"

    # ── @ Venue ───────────────────────────────────────────────────────────────

    def test_at_venue(self):
        assert extract_artist_from_calendar_title("Hozier @ The Forum") == "Hozier"

    def test_at_venue_multi_word_artist(self):
        assert extract_artist_from_calendar_title(
            "Vampire Weekend @ Brixton Academy"
        ) == "Vampire Weekend"

    # ── "at Venue" ────────────────────────────────────────────────────────────

    def test_at_the_venue(self):
        assert extract_artist_from_calendar_title(
            "Phoebe Bridgers at The Greek Theatre"
        ) == "Phoebe Bridgers"

    def test_at_venue_without_the(self):
        assert extract_artist_from_calendar_title(
            "Phoebe Bridgers at Hollywood Bowl"
        ) == "Phoebe Bridgers"

    # ── "An Evening with" ─────────────────────────────────────────────────────

    def test_an_evening_with(self):
        assert extract_artist_from_calendar_title(
            "An Evening with Norah Jones"
        ) == "Norah Jones"

    # ── Artist: Subtitle (colon) ──────────────────────────────────────────────

    def test_colon_subtitle(self):
        assert extract_artist_from_calendar_title(
            "Paramore: This Is Why Tour"
        ) == "Paramore"

    # ── Artist | Tour Name (pipe — ticketing format) ──────────────────────────

    def test_pipe_tour(self):
        # Ticketing calendars format events as "ARTIST | Tour Name"; without a
        # pipe rule the "...Tour" suffix pattern would greedily keep the whole
        # subtitle. The pipe must win so only the artist survives.
        assert extract_artist_from_calendar_title(
            "Ticket: INOHA | This Might Be Useful Tour 2026"
        ) == "INOHA"

    # ── Artist - Tour Name (dash) ─────────────────────────────────────────────

    def test_dash_tour(self):
        assert extract_artist_from_calendar_title(
            "Radiohead - In Rainbows Tour"
        ) == "Radiohead"

    def test_em_dash_tour(self):
        assert extract_artist_from_calendar_title(
            "The National – Laugh Track Tour"
        ) == "The National"

    # ── Keyword suffixes ──────────────────────────────────────────────────────

    def test_live_suffix(self):
        assert extract_artist_from_calendar_title("Vampire Weekend Live") == "Vampire Weekend"

    def test_tour_suffix(self):
        assert extract_artist_from_calendar_title("Mitski Tour") == "Mitski"

    # ── Suffix stripping ──────────────────────────────────────────────────────

    def test_sold_out_stripped(self):
        assert extract_artist_from_calendar_title("boygenius (SOLD OUT)") == "boygenius"

    def test_rescheduled_stripped_before_at(self):
        assert extract_artist_from_calendar_title(
            "Arcade Fire (RESCHEDULED) @ The Forum"
        ) == "Arcade Fire"

    def test_postponed_stripped(self):
        assert extract_artist_from_calendar_title(
            "The National - Laugh Track Tour - POSTPONED"
        ) == "The National"

    # ── Fallback ──────────────────────────────────────────────────────────────

    def test_plain_artist_name_returned_as_is(self):
        # No pattern matches → full title is the best guess
        assert extract_artist_from_calendar_title("boygenius") == "boygenius"

    def test_whitespace_stripped_from_fallback(self):
        assert extract_artist_from_calendar_title("  Radiohead  ") == "Radiohead"


class TestIsLikelyConcert:
    def test_tour_in_title(self):
        assert is_likely_concert("Radiohead - In Rainbows Tour")

    def test_at_symbol_in_title(self):
        assert is_likely_concert("Hozier @ The Forum")

    def test_live_in_title(self):
        assert is_likely_concert("Vampire Weekend Live")

    def test_concert_in_title(self):
        assert is_likely_concert("Summer Concert Series")

    def test_festival_in_title(self):
        assert is_likely_concert("Coachella Music Festival")

    def test_tickets_in_description(self):
        assert is_likely_concert("Some Event", "Get your tickets here")

    def test_performing_in_description(self):
        assert is_likely_concert("Friday Night", "Radiohead performing live")

    def test_not_a_concert_no_keywords(self):
        assert not is_likely_concert("Team lunch", "Grab a burger with the team")

    def test_birthday_party_not_a_concert(self):
        assert not is_likely_concert("Birthday Party", "Celebrating 30 years!")

    def test_empty_strings(self):
        assert not is_likely_concert("", "")

    # Ticketing-service calendar invites (Ticketmaster, AXS, etc.)
    def test_ticket_colon_prefix_in_title(self):
        # "Ticket: Artist" with no other keywords — the exact BCNR case
        assert is_likely_concert(
            "Ticket: Black Country, New Road",
            "Reservation Number: 44-44222/CH5\n\nProvider: Ticketmaster",
        )

    def test_ticketmaster_provider_in_description(self):
        assert is_likely_concert("Some Event", "Provider: Ticketmaster")

    def test_axs_provider_in_description(self):
        assert is_likely_concert("Some Event", "Provider: AXS.com")


class TestNormalize:
    def test_lowercase(self):
        assert _normalize("Radiohead") == "radiohead"

    def test_strips_leading_the(self):
        assert _normalize("The National") == "national"

    def test_the_in_middle_kept(self):
        # "the" only stripped at start
        assert _normalize("Into the Wild") == "into the wild"

    def test_strips_parentheticals(self):
        assert _normalize("Radiohead (Reissue)") == "radiohead"

    def test_strips_and_the_band_suffix(self):
        assert _normalize("Bruce Springsteen and the Band") == "bruce springsteen"

    def test_strips_ampersand_band_suffix(self):
        assert _normalize("Tom Petty & the Band") == "tom petty"

    def test_collapses_internal_whitespace(self):
        assert _normalize("  The   Cure  ") == "cure"

    def test_strips_punctuation(self):
        assert _normalize("AC/DC") == "ac dc"

    def test_strips_comma_punctuation(self):
        # Commas are stripped so "Black Country, New Road" matches "black country new road"
        assert _normalize("Black Country, New Road") == "black country new road"


# ── _search_spotify ────────────────────────────────────────────────────────────

class TestSearchSpotify:
    def _sp(self, items=None):
        sp = MagicMock()
        sp.search.return_value = {'artists': {'items': items or []}}
        return sp

    def test_query_uses_quoted_artist_name(self):
        # Quoting prevents Spotify's Lucene parser from splitting on commas
        # e.g. "Black Country, New Road" must not be split into separate terms
        sp = self._sp()
        _search_spotify("Black Country, New Road", sp)
        sp.search.assert_called_once_with(
            q='artist:"Black Country, New Road"',
            type='artist',
            limit=10,
        )

    def test_query_quoted_for_simple_names_too(self):
        sp = self._sp()
        _search_spotify("Mitski", sp)
        sp.search.assert_called_once_with(
            q='artist:"Mitski"', type='artist', limit=10
        )

    def test_exact_name_match_returned(self):
        sp = self._sp(items=[
            {'id': 'bcnr123', 'name': 'Black Country, New Road'},
        ])
        assert _search_spotify("Black Country, New Road", sp) == 'bcnr123'

    def test_normalized_name_match_returned(self):
        # "The National" normalizes the same as candidate "The National"
        sp = self._sp(items=[{'id': 'nat1', 'name': 'The National'}])
        assert _search_spotify("The National", sp) == 'nat1'

    def test_no_candidates_returns_none(self):
        assert _search_spotify("Unknown Artist", self._sp()) is None

    def test_unrelated_candidates_return_none(self):
        sp = self._sp(items=[
            {'id': 'z1', 'name': 'Completely Different Band'},
        ])
        assert _search_spotify("Black Country, New Road", sp) is None

    def test_exact_match_beats_unrelated_artist(self):
        sp = self._sp(items=[
            {'id': 'wrong',   'name': 'Something Else'},
            {'id': 'correct', 'name': 'Mitski'},
        ])
        assert _search_spotify("Mitski", sp) == 'correct'


# ── resolve_artist ─────────────────────────────────────────────────────────────

class TestResolveArtist:
    def test_unresolved_ttl_shorter_than_resolution_ttl(self):
        # Failed resolutions should expire quickly so code fixes take effect promptly
        assert UNRESOLVED_TTL < RESOLUTION_TTL

    def test_unresolved_ttl_is_at_most_one_day(self):
        assert UNRESOLVED_TTL <= 24 * 3600

    def test_unresolved_artist_cached_with_short_ttl(self, tmp_path):
        cache = Cache(db_path=tmp_path / 'test.db')
        sp = MagicMock()
        sp.search.return_value = {'artists': {'items': []}}

        assert resolve_artist("Ghost Artist", sp, cache) is None

        # Sentinel is stored
        assert cache.get('artist_resolve:ghost artist') == UNRESOLVED_SENTINEL

        # TTL is UNRESOLVED_TTL, not the longer RESOLUTION_TTL
        with sqlite3.connect(str(tmp_path / 'test.db')) as conn:
            (expires_at,) = conn.execute(
                "SELECT expires_at FROM kv_cache WHERE key = 'artist_resolve:ghost artist'"
            ).fetchone()
        assert abs(expires_at - (time.time() + UNRESOLVED_TTL)) < 5

    def test_resolved_artist_cached_with_long_ttl(self, tmp_path):
        cache = Cache(db_path=tmp_path / 'test.db')
        sp = MagicMock()
        sp.search.return_value = {'artists': {'items': [
            {'id': 'mitski_id', 'name': 'Mitski'},
        ]}}

        assert resolve_artist("Mitski", sp, cache) == 'mitski_id'

        with sqlite3.connect(str(tmp_path / 'test.db')) as conn:
            (expires_at,) = conn.execute(
                "SELECT expires_at FROM kv_cache WHERE key = 'artist_resolve:mitski'"
            ).fetchone()
        assert abs(expires_at - (time.time() + RESOLUTION_TTL)) < 5

    def test_cached_id_returned_without_api_call(self, tmp_path):
        cache = Cache(db_path=tmp_path / 'test.db')
        sp = MagicMock()
        cache.set('artist_resolve:radiohead', 'rh_spotify_id', RESOLUTION_TTL)

        assert resolve_artist("Radiohead", sp, cache) == 'rh_spotify_id'
        sp.search.assert_not_called()

    def test_cached_sentinel_returns_none_without_api_call(self, tmp_path):
        cache = Cache(db_path=tmp_path / 'test.db')
        sp = MagicMock()
        cache.set('artist_resolve:ghost artist', UNRESOLVED_SENTINEL, UNRESOLVED_TTL)

        assert resolve_artist("Ghost Artist", sp, cache) is None
        sp.search.assert_not_called()

    # ── Priority 1: Ticketmaster-provided Spotify ID ──────────────────────────

    def test_tm_spotify_id_resolves_without_search(self, tmp_path):
        cache = Cache(db_path=tmp_path / 'test.db')
        sp = MagicMock()
        sp.artist.return_value = {'id': 'tm_abc'}

        result = resolve_artist("Some Artist", sp, cache, tm_spotify_id='tm_abc')

        assert result == 'tm_abc'
        sp.artist.assert_called_once_with('tm_abc')
        sp.search.assert_not_called()

    def test_tm_spotify_id_cached_with_long_ttl(self, tmp_path):
        cache = Cache(db_path=tmp_path / 'test.db')
        sp = MagicMock()
        sp.artist.return_value = {'id': 'tm_abc'}

        assert resolve_artist("Some Artist", sp, cache, tm_spotify_id='tm_abc') == 'tm_abc'

        with sqlite3.connect(str(tmp_path / 'test.db')) as conn:
            (expires_at,) = conn.execute(
                "SELECT expires_at FROM kv_cache WHERE key = 'artist_resolve:some artist'"
            ).fetchone()
        assert abs(expires_at - (time.time() + RESOLUTION_TTL)) < 5

    def test_tm_spotify_id_verification_failure_falls_back_to_search(self, tmp_path):
        cache = Cache(db_path=tmp_path / 'test.db')
        sp = MagicMock()
        sp.artist.side_effect = Exception('bad id')
        sp.search.return_value = {'artists': {'items': [
            {'id': 'search_id', 'name': 'Some Artist'},
        ]}}

        result = resolve_artist("Some Artist", sp, cache, tm_spotify_id='tm_bad_id')

        assert result == 'search_id'
        sp.artist.assert_called_once_with('tm_bad_id')
        sp.search.assert_called_once()


class TestSplitArtistNames:
    # ── Single-artist passthrough ─────────────────────────────────────────────

    def test_single_artist_unchanged(self):
        assert split_artist_names("Hozier") == ["Hozier"]

    def test_single_artist_with_spaces_unchanged(self):
        assert split_artist_names("Vampire Weekend") == ["Vampire Weekend"]

    # ── w/ separator ──────────────────────────────────────────────────────────

    def test_w_slash_two_artists(self):
        assert split_artist_names("Hozier w/ Allison Russell") == ["Hozier", "Allison Russell"]

    def test_w_slash_three_artists(self):
        assert split_artist_names("Headliner w/ Support1 w/ Support2") == [
            "Headliner", "Support1", "Support2"
        ]

    def test_w_slash_case_insensitive(self):
        assert split_artist_names("Hozier W/ Allison Russell") == ["Hozier", "Allison Russell"]

    # ── feat separator ────────────────────────────────────────────────────────

    def test_feat_period(self):
        assert split_artist_names("Kaytranada feat. Aminé") == ["Kaytranada", "Aminé"]

    def test_feat_no_period(self):
        assert split_artist_names("Kaytranada feat Aminé") == ["Kaytranada", "Aminé"]

    def test_ft_period(self):
        assert split_artist_names("Kaytranada ft. Aminé") == ["Kaytranada", "Aminé"]

    def test_featuring(self):
        assert split_artist_names("Kaytranada featuring Aminé") == ["Kaytranada", "Aminé"]

    # ── Ambiguous single separator — kept intact ──────────────────────────────

    def test_single_comma_kept_intact(self):
        # A lone comma is indistinguishable from a band name like "Black Country, New Road"
        assert split_artist_names("Artist1, Artist2") == ["Artist1, Artist2"]

    def test_single_ampersand_kept_intact(self):
        # A lone & is indistinguishable from a band name like "Simon & Garfunkel"
        assert split_artist_names("Artist1 & Artist2") == ["Artist1 & Artist2"]

    def test_single_and_kept_intact(self):
        # "and" alone is similarly ambiguous
        assert split_artist_names("Artist1 and Artist2") == ["Artist1 and Artist2"]

    # ── Regression: band names with commas or & must not be split ────────────

    def test_bcnr_ticket_prefix_pipeline(self):
        # Full pipeline for "Ticket: Black Country, New Road" (6/27 concert).
        # The comma is part of the band name, not a list separator.
        title = "Ticket: Black Country, New Road"
        artist_string = extract_artist_from_calendar_title(title)
        assert artist_string == "Black Country, New Road"
        assert split_artist_names(artist_string) == ["Black Country, New Road"]

    # ── Clear list: multiple commas ───────────────────────────────────────────

    def test_three_commas_split(self):
        assert split_artist_names("Artist1, Artist2, Artist3") == [
            "Artist1", "Artist2", "Artist3"
        ]

    # ── Clear list: comma + conjunction (&/and) ───────────────────────────────

    def test_comma_and_ampersand_split(self):
        # "A, B & C" — comma plus & together is an unambiguous list
        assert split_artist_names("Artist1, Artist2 & Artist3") == [
            "Artist1", "Artist2", "Artist3"
        ]

    def test_comma_and_and_split(self):
        assert split_artist_names("Artist1, Artist2 and Artist3") == [
            "Artist1", "Artist2", "Artist3"
        ]

    # ── Mixed delimiters (four-artist concert bill) ───────────────────────────

    def test_comma_list_with_ampersand_four_artists(self):
        # Regression: the 4/26 concert with four artists on the bill
        assert split_artist_names("Mitski, Snail Mail, Japanese Breakfast & boygenius") == [
            "Mitski", "Snail Mail", "Japanese Breakfast", "boygenius"
        ]

    def test_w_slash_then_comma_list(self):
        # "Headliner w/ Support1, Support2 & Support3"
        assert split_artist_names("Headliner w/ Support1, Support2 & Support3") == [
            "Headliner", "Support1", "Support2", "Support3"
        ]

    def test_full_title_four_artists_at_venue(self):
        # Full pipeline for the 4/26 case: title extraction then split
        title = "Mitski, Snail Mail, Japanese Breakfast & boygenius @ The Forum"
        artist_string = extract_artist_from_calendar_title(title)
        assert split_artist_names(artist_string) == [
            "Mitski", "Snail Mail", "Japanese Breakfast", "boygenius"
        ]

    def test_full_title_w_slash_format(self):
        title = "Hozier w/ Allison Russell @ Hollywood Bowl"
        artist_string = extract_artist_from_calendar_title(title)
        assert split_artist_names(artist_string) == ["Hozier", "Allison Russell"]

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_minimum_name_length_filters_junk(self):
        # Single-character fragments are dropped (triggered only after a real split)
        result = split_artist_names("A, B & Radiohead")
        assert "Radiohead" in result
        assert "A" not in result

    def test_whitespace_trimmed_from_each_name(self):
        result = split_artist_names("  Artist1  ,  Artist2  ,  Artist3  ")
        assert result == ["Artist1", "Artist2", "Artist3"]
