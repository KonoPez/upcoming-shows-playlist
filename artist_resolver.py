"""
Maps artist names (from Ticketmaster or calendar event titles) to Spotify artist IDs.

This is the highest-risk component: a bad match silently pollutes the playlist
with the wrong artist's tracks. The strategy is:
  1. Use Ticketmaster's externalLinks.spotify if present (most reliable).
  2. Fall back to Spotify name search with fuzzy scoring.
  3. Cache every resolution (including failures) to avoid repeated misses.
  4. Log unresolved names so the user can investigate.
"""

import logging
import re
from typing import Optional
import spotipy

from cache import Cache

logger = logging.getLogger(__name__)

RESOLUTION_TTL = 90 * 24 * 3600    # 90 days — artist IDs rarely change
UNRESOLVED_TTL  =  1 * 24 * 3600   # 1 day  — retry failures quickly after bug fixes
UNRESOLVED_SENTINEL = '__UNRESOLVED__'

# A bare "&"/"and" acting as a list separator. "and the …" is excluded because
# that shape is almost always part of a band name, not a boundary between acts.
_CONJUNCTION = r'\s+(?:&|and)\s+(?!the\s)'


# ── Name normalisation ───────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase, strip articles/punctuation for fuzzy comparison."""
    name = name.lower().strip()
    name = re.sub(r'^the\s+', '', name)
    name = re.sub(r'\s*\([^)]*\)', '', name)          # remove parentheticals
    name = re.sub(r'\s*&\s*', ' and ', name)          # "A & B" and "A and B" are the same act
    name = re.sub(r'\s+and\s+(the\s+)?band$', '', name)
    name = re.sub(r'[^\w\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


# ── Core resolution ──────────────────────────────────────────────────────────

def resolve_artist(
    artist_name: str,
    sp: spotipy.Spotify,
    cache: Cache,
    tm_spotify_id: str = '',
) -> Optional[str]:
    """
    Resolve an artist name to a Spotify artist ID.
    Returns the ID string, or None if unresolvable.
    Results are cached for RESOLUTION_TTL seconds.
    """
    cache_key = f'artist_resolve:{artist_name.lower().strip()}'

    cached = cache.get(cache_key)
    if cached is not None:
        if cached == UNRESOLVED_SENTINEL:
            return None
        return cached

    # Priority 1: Ticketmaster already linked a Spotify ID
    if tm_spotify_id:
        verified = _verify_spotify_id(tm_spotify_id, sp)
        if verified:
            logger.debug(f'"{artist_name}" → {verified} (via TM external link)')
            cache.set(cache_key, verified, RESOLUTION_TTL)
            return verified

    # Priority 2: Spotify search
    spotify_id = _search_spotify(artist_name, sp)
    if spotify_id:
        logger.debug(f'"{artist_name}" → {spotify_id} (via Spotify search)')
        cache.set(cache_key, spotify_id, RESOLUTION_TTL)
        return spotify_id

    logger.warning(f'Could not resolve artist to Spotify: "{artist_name}"')
    cache.set(cache_key, UNRESOLVED_SENTINEL, UNRESOLVED_TTL)
    return None


def _verify_spotify_id(spotify_id: str, sp: spotipy.Spotify) -> Optional[str]:
    """Confirm a Spotify artist ID is valid and return it, or None."""
    try:
        artist = sp.artist(spotify_id)
        return artist.get('id') if artist else None
    except Exception:
        return None


def _search_spotify(name: str, sp: spotipy.Spotify) -> Optional[str]:
    """Search Spotify for an artist and return the best-match ID, or None."""
    # Quote the name so Spotify's Lucene parser treats it as a phrase rather than
    # splitting on commas or other punctuation (e.g. "Black Country, New Road").
    try:
        results = sp.search(q=f'artist:"{name}"', type='artist', limit=10)
    except Exception as e:
        logger.error(f'Spotify search failed for "{name}": {e}')
        return None

    candidates = results.get('artists', {}).get('items', [])
    if not candidates:
        return None

    norm_query = _normalize(name)
    best_id, best_score = None, -1.0

    for c in candidates:
        candidate_name = c.get('name', '')
        candidate_id = c.get('id', '')

        if not candidate_id:
            continue

        norm_candidate = _normalize(candidate_name)

        if candidate_name.lower() == name.lower():
            score = 200.0
        elif norm_candidate == norm_query:
            score = 100.0
        elif norm_query in norm_candidate or norm_candidate in norm_query:
            score = 50.0
        else:
            score = 0.0

        if score > best_score:
            best_score = score
            best_id = candidate_id

    return best_id if best_score >= 1.0 else None


# ── Calendar event parsing ────────────────────────────────────────────────────

def split_artist_names(artist_string: str) -> list[str]:
    """
    Split a multi-act bill ("Headliner w/ Support1, Support2 & Support3") into
    individual artist names.

    Band names contain commas and conjunctions too, so a string whose structure
    doesn't clearly signal a list is returned intact as a single name.
    """
    # "w/" and "feat." generally separate one act from another.
    parts = re.split(
        r'\s+w/\s+|\s+(?:feat\.?|ft\.?|featuring)\s+',
        artist_string,
        flags=re.IGNORECASE,
    )

    result: list[str] = []
    for index, part in enumerate(parts):
        part = part.strip()
        comma_count = part.count(',')
        # Two separate questions. Any conjunction is evidence the part is a
        # list, but only a separator-shaped one is a place to cut: "Cheekface,
        # Prince Daddy and the Hyena" cuts at the comma, not at the "and".
        has_conjunction = bool(re.search(r'\s+(?:&|and)\s+', part, re.IGNORECASE))
        has_separator = bool(re.search(_CONJUNCTION, part, re.IGNORECASE))
        in_support_list = index > 0

        # One comma ("Black Country, New Road") or one conjunction ("Simon &
        # Garfunkel") sits inside band names too often to split on. Two commas,
        # or a comma plus a conjunction, is a list.
        clear_list = comma_count >= 2 or (comma_count >= 1 and has_conjunction)
        if clear_list or (in_support_list and has_separator):
            sub = re.split(rf'\s*,\s*|{_CONJUNCTION}', part, flags=re.IGNORECASE)
            result.extend(s.strip() for s in sub if s.strip())
        else:
            result.append(part)

    names = [n for n in result if len(n) >= 2]
    return names if names else [artist_string.strip()]


def extract_artist_from_calendar_title(title: str) -> str:
    """
    Best-effort extraction of an artist name from a calendar event title.
    Falls back to the full title if no pattern matches.
    """
    # Strip known label prefixes — e.g. "Ticket: Artist Name" or "Tickets: ..."
    title = re.sub(r'^tickets?\s*:\s*', '', title, flags=re.IGNORECASE).strip()

    # Strip common suffixes that are not part of the artist name
    title = re.sub(r'\s*\(SOLD\s*OUT\)', '', title, flags=re.IGNORECASE).strip()
    title = re.sub(r'\s*\(RESCHEDULED\)', '', title, flags=re.IGNORECASE).strip()
    title = re.sub(r'\s*-\s*POSTPONED$', '', title, flags=re.IGNORECASE).strip()

    patterns = [
        r'^(.+?)\s+@\s+',                                  # Artist @ Venue
        r'^(.+?)\s+at\s+(?:the\s+)?\w',                    # Artist at [The] Venue
        r'^An\s+Evening\s+with\s+(.+?)$',                  # An Evening with Artist
        r'^(.+?)\s*:\s*',                                  # Artist: Subtitle
        r'^(.+?)\s*\|\s*',                                 # Artist | Tour Name (ticketing format)
        r'^(.+?)\s+[-–]\s+',                               # Artist - Tour Name
        r'^(.+?)\s+(?:Live|Concert|Tour|Show|Presents)\b',  # Artist Live/Tour/Show
    ]

    for pattern in patterns:
        m = re.match(pattern, title, re.IGNORECASE)
        if m:
            extracted = m.group(1).strip()
            if len(extracted) >= 2:
                return extracted

    return title.strip()


def is_likely_concert(title: str, description: str = '') -> bool:
    """Heuristic: does this calendar event look like a music concert?"""
    text = (title + ' ' + (description or '')).lower()
    keywords = [
        ' concert', ' tour', ' show', ' live', ' gig',
        ' @ ', ' festival', ' performing', ' tickets',
        'ticket:', 'ticketmaster', 'axs.com', 'dice.fm', 'seetickets',
    ]
    return any(kw in text for kw in keywords)
