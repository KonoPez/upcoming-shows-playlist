"""
Track title normalisation.

The artist-name counterpart of this module is `artist_resolver`; this one deals
with the song-title end of the same problem — the same song reaches us spelled
several different ways (a live pressing, an acoustic re-cut, a remix) and has to
collapse to one key before it can be grouped or looked up externally.

Lives at the top level rather than inside a package because every layer needs
it: `spotify_client` when grouping a discography, `playlist_logic` when scoring,
and `main` when printing dry-run output.
"""

import re

# The words that mark a recording as something other than the studio original.
# Shared with spotify_client, which uses the same set to *detect* variants while
# this module *strips* them: a recording the filter rejects and a suffix the
# normaliser removes have to be the same thing, or a variant that slips past the
# filter would also fail to collapse onto its studio original's lookup key.
VARIANT_KEYWORDS = r'(?:live|acoustic|unplugged|remix|instrumental|demo|a\s*cappella|acapella)'

# Suffixes that name which *pressing* of a recording this is rather than a
# different performance of it: "Heart Container - EP Version" is the same take
# as "Heart Container", re-sequenced onto an EP.  Kept separate from
# VARIANT_KEYWORDS because that set does double duty — spotify_client uses it to
# *reject* recordings, and these pressings are not rejects, they are the same
# song under a longer name.  The two must also stay apart at the album level:
# VARIANT_KEYWORDS is matched against album names, where a bare `version` would
# swallow every "(Deluxe Version)" album whole.
#
# The edition word is required, so only a qualified "<qualifier> version/edit/mix"
# strips.  A song actually called "Mix" or "The Edit" is untouched.
EDITION_KEYWORDS = (
    r'(?:ep|album|single|deluxe|extended|radio|clean)\s+(?:version|edit|mix)'
)


def normalize_track_name(name: str) -> str:
    """
    Strip variant and edition suffixes and lowercase a track name for use as a
    lookup key.

    "Dancers - Live at Bush Hall"  → "dancers"
    "Song (Live at Glastonbury)"   → "song"
    "Song (Acoustic Version)"      → "song"
    "Heart Container - EP Version" → "heart container"
    "Concorde"                     → "concorde"

    Used both when grouping tracks by song identity (deduplication) and when
    looking up setlist/Last.fm scores, so that live-only releases match the
    canonical song name used by those external services.

    Both suffix families have to collapse here, and for the same reason:
    Endswell's "Heart Container - EP Version" survived deduplication next to
    "Heart Container", *and* missed setlist.fm — which indexes the song under
    its plain name and reports Endswell playing it at every show — so the 55%
    setlist signal read as a flat zero on the one song they always play.
    """
    stripped = re.sub(rf'\s*\(.*\b{VARIANT_KEYWORDS}\b.*\).*$', '', name, flags=re.IGNORECASE)
    stripped = re.sub(rf'\s*[-–]\s*{VARIANT_KEYWORDS}\b.*$', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(rf'\s*\(.*\b{EDITION_KEYWORDS}\b.*\).*$', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(rf'\s*[-–]\s*{EDITION_KEYWORDS}\b.*$', '', stripped, flags=re.IGNORECASE)
    return stripped.strip().lower()
