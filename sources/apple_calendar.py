"""
Apple Calendar / iCloud integration via caldav.

Authentication requires an app-specific password (NOT your Apple ID password).
Generate one at: https://appleid.apple.com → Sign-In and Security → App-Specific Passwords

Events are filtered by keyword heuristics to find likely concert events, then
the artist name is extracted from the event title.
"""

import concurrent.futures
import logging
from datetime import date, datetime, timezone

import caldav
from icalendar import Calendar

from artist_resolver import extract_artist_from_calendar_title, is_likely_concert, split_artist_names
from sources.models import Concert

logger = logging.getLogger(__name__)

ICLOUD_URL = 'https://caldav.icloud.com'


class AppleCalendarClient:
    def __init__(self, username: str, app_password: str):
        self.username = username
        self.app_password = app_password

    def get_concerts(self, start_date: date, end_date: date) -> list[Concert]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._fetch, start_date, end_date)
            try:
                return future.result(timeout=60)
            except concurrent.futures.TimeoutError:
                logger.warning('Apple Calendar: timed out after 60s — skipping')
                return []
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

    def _parse(self, event: caldav.Event) -> list[Concert]:
        try:
            vevents = Calendar.from_ical(event.data).walk('VEVENT')
            if not vevents:
                return []
            vevent = vevents[0]

            summary = str(vevent.get('SUMMARY', ''))
            description = str(vevent.get('DESCRIPTION', ''))

            if not is_likely_concert(summary, description):
                return []

            dtstart = vevent.get('DTSTART')
            if not dtstart:
                return []

            dt = dtstart.dt
            if isinstance(dt, datetime):
                event_date = dt.date()
            elif isinstance(dt, date):
                event_date = dt
            else:
                return []

            artist_string = extract_artist_from_calendar_title(summary)
            if not artist_string:
                return []

            location = str(vevent.get('LOCATION', 'Unknown Venue'))

            # Bills are written headliner-first ("Headliner w/ Support1, Support2"),
            # so everything after the first name is a supporting act. Same
            # convention TicketmasterClient.get_local_events uses for attractions.
            return [
                Concert(
                    event_name=summary,
                    artist_name=name,
                    event_date=event_date,
                    venue=location,
                    source='apple_calendar',
                    is_opener=(i > 0),
                )
                for i, name in enumerate(split_artist_names(artist_string))
            ]
        except Exception as e:
            logger.debug(f'Failed to parse Apple Calendar event: {e}')
            return []
