"""
Ticketmaster Discovery API integration for concert lineup enrichment.

When a calendar event contains only one artist, this module queries
Ticketmaster to find supporting acts on the same bill.

Requires a free API key from https://developer.ticketmaster.com/
Set TICKETMASTER_API_KEY in .env to enable.
"""

import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

import requests

from cache import Cache
from sources.models import Concert

logger = logging.getLogger(__name__)

BASE_URL = 'https://app.ticketmaster.com/discovery/v2'
LINEUP_TTL = 7 * 24 * 3600        # 7 days — lineups rarely change once announced
LOCAL_EVENTS_TTL = 6 * 3600       # 6 hours — new shows get announced frequently
LOCAL_EVENTS_PAGE_SIZE = 100       # results per page
LOCAL_EVENTS_MAX_PAGES = 5         # cap at 500 events; enough for any metro area
CALENDAR_SOURCES = {'apple_calendar', 'google_calendar'}


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _normalize_venue(venue: str) -> str:
    """Lowercase, alphanumeric only, for fuzzy venue comparison."""
    return re.sub(r'[^a-z0-9]', '', venue.lower())


def _venue_matches(cal_venue: str, tm_venue: str) -> bool:
    """True when the two venue strings likely refer to the same place."""
    a = _normalize_venue(cal_venue)
    b = _normalize_venue(tm_venue)
    # Empty or the calendar fallback sentinel both mean "no location data"
    if not a or not b or a == 'unknownvenue':
        return True   # can't disprove — give benefit of the doubt
    return a in b or b in a


def _extract_spotify_id(url: str) -> Optional[str]:
    """Extract a Spotify artist ID from a URL or URI string."""
    m = re.search(r'spotify\.com/artist/([A-Za-z0-9]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'spotify:artist:([A-Za-z0-9]+)', url)
    if m:
        return m.group(1)
    return None


# ── Concert serialization (for KV cache) ─────────────────────────────────────

def _concert_to_dict(c: Concert) -> dict:
    return {
        'event_name':    c.event_name,
        'artist_name':   c.artist_name,
        'event_date':    c.event_date.isoformat(),
        'venue':         c.venue,
        'source':        c.source,
        'tm_spotify_id': c.tm_spotify_id,
        'is_opener':     c.is_opener,
    }


def _concert_from_dict(d: dict) -> Concert:
    return Concert(
        event_name=d['event_name'],
        artist_name=d['artist_name'],
        event_date=date.fromisoformat(d['event_date']),
        venue=d['venue'],
        source=d['source'],
        tm_spotify_id=d.get('tm_spotify_id'),
        is_opener=d.get('is_opener', False),
    )


def _concerts_from_event(event: dict) -> list[Concert]:
    """
    Convert a raw Ticketmaster event object into one Concert per attraction.
    The first attraction is treated as the headliner (is_opener=False);
    remaining attractions are openers (is_opener=True).
    Events with no localDate or no attractions are skipped.
    """
    local_date = event.get('dates', {}).get('start', {}).get('localDate')
    if not local_date:
        return []
    try:
        event_date = date.fromisoformat(local_date)
    except ValueError:
        return []

    event_name   = event.get('name', '')
    embedded     = event.get('_embedded', {})
    attractions  = embedded.get('attractions', [])
    venues       = embedded.get('venues', [])
    venue_name   = venues[0].get('name', 'Unknown Venue') if venues else 'Unknown Venue'

    if not attractions:
        return []

    concerts: list[Concert] = []
    for i, attr in enumerate(attractions):
        name = attr.get('name', '').strip()
        if not name:
            continue

        spotify_id: Optional[str] = None
        for link in attr.get('externalLinks', {}).get('spotify', []):
            spotify_id = _extract_spotify_id(link.get('url', ''))
            if spotify_id:
                break

        concerts.append(Concert(
            event_name=event_name,
            artist_name=name,
            event_date=event_date,
            venue=venue_name,
            source='ticketmaster_discovery',
            tm_spotify_id=spotify_id,
            is_opener=(i > 0),
        ))

    return concerts


# ── Client ────────────────────────────────────────────────────────────────────

class TicketmasterClient:
    def __init__(self, api_key: str, cache: Cache):
        self.api_key = api_key
        self.cache = cache

    # ── Local event discovery ─────────────────────────────────────────────────

    def get_local_events(
        self,
        window_days: int,
        latlong: Optional[str] = None,
        city: Optional[str] = None,
        radius_miles: int = 50,
    ) -> list[Concert]:
        """
        Return Concert objects for all music events within radius_miles of the
        given location over the next window_days.

        Exactly one of latlong ("lat,lng") or city (city name string) must be
        provided. Results are cached for LOCAL_EVENTS_TTL.
        """
        if not latlong and not city:
            raise ValueError('Either latlong or city must be provided.')

        today = date.today()
        end_date = today + timedelta(days=window_days)
        location_key = latlong or city
        cache_key = (
            f'tm_local_events:{location_key}:{radius_miles}'
            f':{today.isoformat()}:{end_date.isoformat()}'
        )

        cached = self.cache.get(cache_key)
        if cached is not None:
            return [_concert_from_dict(d) for d in cached]

        concerts = self._fetch_local_events(
            today, end_date, latlong=latlong, city=city, radius_miles=radius_miles
        )
        self.cache.set(cache_key, [_concert_to_dict(c) for c in concerts], LOCAL_EVENTS_TTL)
        return concerts

    def _fetch_local_events(
        self,
        start_date: date,
        end_date: date,
        latlong: Optional[str],
        city: Optional[str],
        radius_miles: int,
    ) -> list[Concert]:
        concerts: list[Concert] = []
        start_dt = f'{start_date.isoformat()}T00:00:00Z'
        end_dt   = f'{end_date.isoformat()}T23:59:59Z'

        for page in range(LOCAL_EVENTS_MAX_PAGES):
            params: dict = {
                'apikey':               self.api_key,
                'classificationName':   'music',
                'startDateTime':        start_dt,
                'endDateTime':          end_dt,
                'radius':               str(radius_miles),
                'unit':                 'miles',
                'size':                 LOCAL_EVENTS_PAGE_SIZE,
                'page':                 page,
            }
            if latlong:
                params['latlong'] = latlong
            else:
                params['city'] = city

            try:
                resp = requests.get(
                    f'{BASE_URL}/events.json', params=params, timeout=15
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.warning(f'Ticketmaster local events error (page {page}): {e}')
                break

            data  = resp.json()
            events = data.get('_embedded', {}).get('events', [])
            if not events:
                break

            for event in events:
                concerts.extend(_concerts_from_event(event))

            page_info   = data.get('page', {})
            total_pages = page_info.get('totalPages', 1)
            if page + 1 >= total_pages:
                break

        logger.info(f'Ticketmaster: {len(concerts)} artist slots from local events near {latlong or city}')
        return concerts

    def lookup_openers(
        self,
        headliner: str,
        event_date: date,
        venue: str,
    ) -> list[Concert]:
        """
        Return Concert objects for supporting acts on the same bill as headliner.
        Results are cached for LINEUP_TTL seconds.
        Returns an empty list when no openers are found or the API is unavailable.
        """
        cache_key = f'tm_openers:{headliner.lower().strip()}:{event_date}'
        cached = self.cache.get(cache_key)
        if cached is not None:
            return [
                Concert(
                    event_name=item['event_name'],
                    artist_name=item['name'],
                    event_date=event_date,
                    venue=venue,
                    source='ticketmaster',
                    tm_spotify_id=item.get('spotify_id'),
                    is_opener=True,
                )
                for item in cached
            ]

        openers = self._fetch_openers(headliner, event_date, venue)
        if openers is not None:
            self.cache.set(
                cache_key,
                [
                    {
                        'event_name': c.event_name,
                        'name': c.artist_name,
                        'spotify_id': c.tm_spotify_id,
                    }
                    for c in openers
                ],
                LINEUP_TTL,
            )
            return openers
        return []

    def _fetch_openers(
        self,
        headliner: str,
        event_date: date,
        venue: str,
    ) -> Optional[list[Concert]]:
        """
        Hit the Ticketmaster API and return openers, or None on network error.
        Returns [] when the API responds but no matching event is found.
        """
        date_str = event_date.strftime('%Y-%m-%d')
        try:
            resp = requests.get(
                f'{BASE_URL}/events.json',
                params={
                    'apikey': self.api_key,
                    'keyword': headliner,
                    'startDateTime': f'{date_str}T00:00:00Z',
                    'endDateTime': f'{date_str}T23:59:59Z',
                    'classificationName': 'music',
                    'size': 5,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f'Ticketmaster API error for "{headliner}": {e}')
            return None

        events = resp.json().get('_embedded', {}).get('events', [])
        if not events:
            logger.debug(f'Ticketmaster: no events found for "{headliner}" on {date_str}')
            return []

        event = self._best_event(events, headliner, venue)
        if not event:
            logger.debug(
                f'Ticketmaster: could not confidently match an event for '
                f'"{headliner}" on {date_str} at "{venue}"'
            )
            return []

        return self._extract_openers(event, headliner, event_date, venue)

    def _best_event(
        self,
        events: list[dict],
        headliner: str,
        venue: str,
    ) -> Optional[dict]:
        """
        Score each TM event and return the one most likely matching the
        calendar entry. Requires the headliner to appear in the attraction list;
        a venue match is a bonus.
        """
        best, best_score = None, -1

        for ev in events:
            attractions = ev.get('_embedded', {}).get('attractions', [])
            names_lower = [a.get('name', '').lower() for a in attractions]

            headliner_lower = headliner.lower()
            found_headliner = any(
                headliner_lower in n or n in headliner_lower
                for n in names_lower
            )
            if not found_headliner:
                continue   # skip events where the headliner isn't listed

            score = 10   # headliner present

            tm_venues = ev.get('_embedded', {}).get('venues', [])
            if tm_venues and _venue_matches(venue, tm_venues[0].get('name', '')):
                score += 5

            if score > best_score:
                best_score = score
                best = ev

        return best

    def _extract_openers(
        self,
        event: dict,
        headliner: str,
        event_date: date,
        venue: str,
    ) -> list[Concert]:
        """Build Concert objects for every attraction that is not the headliner."""
        event_name = event.get('name', '')
        attractions = event.get('_embedded', {}).get('attractions', [])
        openers: list[Concert] = []

        for attr in attractions:
            name = attr.get('name', '').strip()
            if not name or name.lower() == headliner.lower():
                continue

            spotify_id: Optional[str] = None
            for link in attr.get('externalLinks', {}).get('spotify', []):
                spotify_id = _extract_spotify_id(link.get('url', ''))
                if spotify_id:
                    break

            openers.append(Concert(
                event_name=event_name,
                artist_name=name,
                event_date=event_date,
                venue=venue,
                source='ticketmaster',
                tm_spotify_id=spotify_id,
                is_opener=True,
            ))
            logger.debug(f'Ticketmaster: found opener "{name}" for "{headliner}"')

        if openers:
            logger.info(
                f'Ticketmaster: {len(openers)} opener(s) for '
                f'"{headliner}" on {event_date}: '
                + ', '.join(c.artist_name for c in openers)
            )

        return openers


# ── Enrichment helper ─────────────────────────────────────────────────────────

def enrich_with_openers(
    concerts: list[Concert],
    client: TicketmasterClient,
) -> list[Concert]:
    """
    For each calendar event that yielded fewer than 2 artists, query
    Ticketmaster for supporting acts and append them to the list.
    """
    groups: dict[tuple, list[Concert]] = defaultdict(list)
    for c in concerts:
        if c.source in CALENDAR_SOURCES:
            groups[(c.event_name, c.event_date)].append(c)

    additional: list[Concert] = []
    for (_, event_date), group in groups.items():
        if len(group) < 2:
            headliner = group[0]
            openers = client.lookup_openers(
                headliner.artist_name, event_date, headliner.venue
            )
            for opener in openers:
                opener.event_name = headliner.event_name  # same concert, not a separate event
            additional.extend(openers)

    return concerts + additional
