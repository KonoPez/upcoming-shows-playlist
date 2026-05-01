"""
Apple Calendar / iCloud integration via caldav.

Requires:
  pip install caldav icalendar

Authentication requires an app-specific password (NOT your Apple ID password).
Generate one at: https://appleid.apple.com → Sign-In and Security → App-Specific Passwords

Events are filtered by keyword heuristics to find likely concert events, then
the artist name is extracted from the event title.
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from artist_resolver import extract_artist_from_calendar_title, is_likely_concert, split_artist_names
from sources.models import Concert

logger = logging.getLogger(__name__)

try:
    import caldav
    CALDAV_AVAILABLE = True
except ImportError:
    CALDAV_AVAILABLE = False

ICLOUD_URL = 'https://caldav.icloud.com'


class AppleCalendarClient:
    def __init__(self, username: str, app_password: str):
        self.username = username
        self.app_password = app_password

    def get_concerts(self, start_date: date, end_date: date) -> list[Concert]:
        if not CALDAV_AVAILABLE:
            logger.warning(
                'caldav not installed. Run: pip install caldav icalendar\n'
                'Apple Calendar integration will be skipped.'
            )
            return []

        try:
            return self._fetch(start_date, end_date)
        except Exception as e:
            logger.error(f'Apple Calendar error: {e}')
            if 'Unauthorized' in str(e) or '401' in str(e):
                logger.info(
                    'Apple Calendar tip: use an app-specific password from '
                    'appleid.apple.com, not your regular Apple ID password.'
                )
            return []

    def _fetch(self, start_date: date, end_date: date) -> list[Concert]:
        client = caldav.DAVClient(
            url=ICLOUD_URL,
            username=self.username,
            password=self.app_password,
            timeout=30,
        )
        principal = client.principal()
        calendars = principal.calendars()

        start_dt = datetime.combine(start_date, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(
            tzinfo=timezone.utc
        )

        concerts: list[Concert] = []
        for cal in calendars:
            try:
                events = cal.date_search(start=start_dt, end=end_dt, expand=False)
            except Exception as e:
                logger.debug(f'Skipping calendar "{getattr(cal, "name", "?")}": {e}')
                continue

            for event in events:
                concerts.extend(self._parse(event))

        logger.info(f'Apple Calendar: {len(concerts)} concert-like events found (searched {len(calendars)} calendars)')
        return concerts

    def _parse(self, event) -> list[Concert]:
        try:
            vobj = event.vobject_instance
            vevent = vobj.vevent

            summary = str(vevent.summary.value) if hasattr(vevent, 'summary') else ''
            description = str(vevent.description.value) if hasattr(vevent, 'description') else ''

            if not is_likely_concert(summary, description):
                return []

            dtstart = vevent.dtstart.value
            if isinstance(dtstart, datetime):
                event_date = dtstart.date()
            elif isinstance(dtstart, date):
                event_date = dtstart
            else:
                return []

            artist_string = extract_artist_from_calendar_title(summary)
            if not artist_string:
                return []

            location = (
                str(vevent.location.value) if hasattr(vevent, 'location') else 'Unknown Venue'
            )

            return [
                Concert(
                    event_name=summary,
                    artist_name=name,
                    event_date=event_date,
                    venue=location,
                    source='apple_calendar',
                )
                for name in split_artist_names(artist_string)
            ]
        except Exception as e:
            logger.debug(f'Failed to parse Apple Calendar event: {e}')
            return []
