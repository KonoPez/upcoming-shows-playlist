"""
Google Calendar integration via private ICS feed.

No OAuth or Google Cloud project required. Google Calendar exposes a
"Secret address in iCal format" for each calendar — a private URL that
returns a standard ICS file readable with a plain HTTP GET.

To find it:
  Google Calendar → Settings (gear) → [your calendar] → Integrate calendar
  → "Secret address in iCal format"

The URL looks like:
  https://calendar.google.com/calendar/ical/.../private-.../basic.ics
"""

import logging
from datetime import date, datetime
from typing import Optional

import requests
from icalendar import Calendar

from artist_resolver import extract_artist_from_calendar_title, is_likely_concert, split_artist_names
from sources.models import Concert

logger = logging.getLogger(__name__)


class GoogleCalendarClient:
    def __init__(self, ics_url: str):
        self.ics_url = ics_url

    def get_concerts(self, start_date: date, end_date: date) -> list[Concert]:
        try:
            resp = requests.get(self.ics_url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f'Failed to fetch Google Calendar ICS: {e}')
            return []

        try:
            cal = Calendar.from_ical(resp.content)
        except Exception as e:
            logger.error(f'Failed to parse Google Calendar ICS: {e}')
            return []

        concerts: list[Concert] = []
        for component in cal.walk('VEVENT'):
            concerts.extend(self._parse(component, start_date, end_date))

        logger.info(f'Google Calendar: {len(concerts)} concert-like events found')
        return concerts

    def _parse(self, component, start_date: date, end_date: date) -> list[Concert]:
        try:
            summary = str(component.get('SUMMARY', ''))
            description = str(component.get('DESCRIPTION', ''))

            if not is_likely_concert(summary, description):
                return []

            dtstart = component.get('DTSTART')
            if not dtstart:
                return []

            dt = dtstart.dt
            event_date = dt.date() if isinstance(dt, datetime) else dt

            if not (start_date <= event_date <= end_date):
                return []

            artist_string = extract_artist_from_calendar_title(summary)
            if not artist_string:
                return []

            location = str(component.get('LOCATION', 'Unknown Venue'))

            return [
                Concert(
                    event_name=summary,
                    artist_name=name,
                    event_date=event_date,
                    venue=location,
                    source='google_calendar',
                )
                for name in split_artist_names(artist_string)
            ]
        except Exception as e:
            logger.debug(f'Failed to parse Google Calendar event: {e}')
            return []
