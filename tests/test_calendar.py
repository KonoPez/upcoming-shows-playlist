"""
Tests for Apple and Google Calendar clients.
No real network calls or CalDAV connections are made.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from sources.apple_calendar import AppleCalendarClient
from sources.google_calendar import GoogleCalendarClient
from sources.models import Concert

# ── shared constants ──────────────────────────────────────────────────────────

START = date(2024, 6, 1)
END   = date(2024, 9, 1)


# ── fake object builders ──────────────────────────────────────────────────────

def _gcal_component(
    summary: str = '',
    description: str = '',
    dtstart=None,
    location: str = 'The Venue',
):
    """Fake icalendar VEVENT component — matches the dict-like API _parse uses."""
    data = {'SUMMARY': summary, 'DESCRIPTION': description, 'LOCATION': location}
    dtstart_obj = SimpleNamespace(dt=dtstart) if dtstart is not None else None

    class _Component:
        def get(self, key, default=''):
            if key == 'DTSTART':
                return dtstart_obj
            return data.get(key, default)

    return _Component()


def _apple_event(
    summary: str = '',
    description=None,
    dtstart=None,
    location=None,
):
    """Fake CalDAV event — matches the vobject attribute structure _parse uses."""
    attrs = {}
    if summary:
        attrs['summary'] = SimpleNamespace(value=summary)
    if description is not None:
        attrs['description'] = SimpleNamespace(value=description)
    attrs['dtstart'] = SimpleNamespace(value=dtstart)
    if location is not None:
        attrs['location'] = SimpleNamespace(value=location)
    vevent = SimpleNamespace(**attrs)
    return SimpleNamespace(vobject_instance=SimpleNamespace(vevent=vevent))


# ── GoogleCalendarClient._parse ───────────────────────────────────────────────

class TestGoogleCalendarParse:
    client = GoogleCalendarClient('http://example.com/cal.ics')

    def test_basic_concert_returns_one_concert(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 7, 1))
        result = self.client._parse(evt, START, END)
        assert len(result) == 1
        assert isinstance(result[0], Concert)

    def test_source_is_google_calendar(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt, START, END)[0].source == 'google_calendar'

    def test_artist_extracted_from_at_style_title(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt, START, END)[0].artist_name == 'Radiohead'

    def test_event_name_is_raw_summary(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt, START, END)[0].event_name == 'Radiohead @ MSG'

    def test_venue_is_location_field(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 7, 1), location='Madison Square Garden')
        assert self.client._parse(evt, START, END)[0].venue == 'Madison Square Garden'

    def test_non_concert_event_excluded(self):
        evt = _gcal_component('Dentist appointment', description='checkup', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt, START, END) == []

    def test_event_before_start_excluded(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 5, 31))
        assert self.client._parse(evt, START, END) == []

    def test_event_after_end_excluded(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 9, 2))
        assert self.client._parse(evt, START, END) == []

    def test_event_on_start_date_included(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=START)
        assert len(self.client._parse(evt, START, END)) == 1

    def test_event_on_end_date_included(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=END)
        assert len(self.client._parse(evt, START, END)) == 1

    def test_date_only_dtstart(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=date(2024, 7, 4))
        concerts = self.client._parse(evt, START, END)
        assert concerts[0].event_date == date(2024, 7, 4)

    def test_datetime_dtstart_converted_to_date(self):
        dt = datetime(2024, 7, 4, 20, 0, tzinfo=timezone.utc)
        evt = _gcal_component('Radiohead @ MSG', dtstart=dt)
        concerts = self.client._parse(evt, START, END)
        assert concerts[0].event_date == date(2024, 7, 4)

    def test_missing_dtstart_returns_empty(self):
        evt = _gcal_component('Radiohead @ MSG', dtstart=None)
        assert self.client._parse(evt, START, END) == []

    def test_missing_location_defaults_to_unknown_venue(self):
        data = {'SUMMARY': 'Radiohead @ MSG', 'DESCRIPTION': ''}
        dtstart_obj = SimpleNamespace(dt=date(2024, 7, 1))

        class _NoLocation:
            def get(self, key, default=''):
                if key == 'DTSTART':
                    return dtstart_obj
                return data.get(key, default)

        result = self.client._parse(_NoLocation(), START, END)
        assert result[0].venue == 'Unknown Venue'

    def test_multi_artist_title_returns_multiple_concerts(self):
        evt = _gcal_component('Radiohead w/ Portishead @ MSG', dtstart=date(2024, 7, 1))
        result = self.client._parse(evt, START, END)
        assert len(result) == 2
        names = {c.artist_name for c in result}
        assert 'Radiohead' in names
        assert 'Portishead' in names

    def test_multi_artist_concerts_share_event_name(self):
        evt = _gcal_component('Radiohead w/ Portishead @ MSG', dtstart=date(2024, 7, 1))
        result = self.client._parse(evt, START, END)
        assert all(c.event_name == 'Radiohead w/ Portishead @ MSG' for c in result)

    def test_concert_keyword_in_description_passes_filter(self):
        evt = _gcal_component('Local Band', description='live concert tonight', dtstart=date(2024, 7, 1))
        assert len(self.client._parse(evt, START, END)) == 1


# ── GoogleCalendarClient.get_concerts ────────────────────────────────────────

_VALID_ICS = b'\r\n'.join([
    b'BEGIN:VCALENDAR',
    b'VERSION:2.0',
    b'PRODID:-//Test//Test//EN',
    b'BEGIN:VEVENT',
    b'SUMMARY:Radiohead @ Madison Square Garden',
    b'DTSTART;VALUE=DATE:20240701',
    b'LOCATION:Madison Square Garden',
    b'DESCRIPTION:concert',
    b'END:VEVENT',
    b'END:VCALENDAR',
    b'',
])


class TestGoogleCalendarGetConcerts:
    def _mock_response(self, content: bytes, status_ok: bool = True):
        resp = MagicMock()
        resp.content = content
        if not status_ok:
            resp.raise_for_status.side_effect = requests.HTTPError('404')
        return resp

    def test_returns_concerts_from_valid_ics(self):
        client = GoogleCalendarClient('http://example.com/cal.ics')
        with patch('sources.google_calendar.requests.get', return_value=self._mock_response(_VALID_ICS)):
            result = client.get_concerts(START, END)
        assert len(result) == 1
        assert result[0].artist_name == 'Radiohead'

    def test_network_error_returns_empty(self):
        client = GoogleCalendarClient('http://example.com/cal.ics')
        with patch('sources.google_calendar.requests.get', side_effect=requests.ConnectionError()):
            result = client.get_concerts(START, END)
        assert result == []

    def test_http_error_returns_empty(self):
        client = GoogleCalendarClient('http://example.com/cal.ics')
        with patch('sources.google_calendar.requests.get', return_value=self._mock_response(b'', status_ok=False)):
            result = client.get_concerts(START, END)
        assert result == []

    def test_invalid_ics_returns_empty(self):
        client = GoogleCalendarClient('http://example.com/cal.ics')
        with patch('sources.google_calendar.requests.get', return_value=self._mock_response(b'not ics data')):
            result = client.get_concerts(START, END)
        assert result == []

    def test_event_outside_range_not_returned(self):
        ics = b'\r\n'.join([
            b'BEGIN:VCALENDAR', b'VERSION:2.0',
            b'BEGIN:VEVENT',
            b'SUMMARY:Radiohead @ MSG',
            b'DTSTART;VALUE=DATE:20241201',  # outside START..END
            b'DESCRIPTION:concert',
            b'END:VEVENT',
            b'END:VCALENDAR', b'',
        ])
        client = GoogleCalendarClient('http://example.com/cal.ics')
        with patch('sources.google_calendar.requests.get', return_value=self._mock_response(ics)):
            result = client.get_concerts(START, END)
        assert result == []


# ── AppleCalendarClient._parse ────────────────────────────────────────────────

class TestAppleCalendarParse:
    client = AppleCalendarClient('user@icloud.com', 'app-password')

    def test_basic_concert_returns_one_concert(self):
        evt = _apple_event('Radiohead @ MSG', dtstart=date(2024, 7, 1), location='MSG')
        result = self.client._parse(evt)
        assert len(result) == 1
        assert isinstance(result[0], Concert)

    def test_source_is_apple_calendar(self):
        evt = _apple_event('Radiohead @ MSG', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt)[0].source == 'apple_calendar'

    def test_artist_extracted_from_at_style_title(self):
        evt = _apple_event('Radiohead @ MSG', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt)[0].artist_name == 'Radiohead'

    def test_event_name_is_raw_summary(self):
        evt = _apple_event('Radiohead @ MSG', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt)[0].event_name == 'Radiohead @ MSG'

    def test_venue_is_location_field(self):
        evt = _apple_event('Radiohead @ MSG', dtstart=date(2024, 7, 1), location='Madison Square Garden')
        assert self.client._parse(evt)[0].venue == 'Madison Square Garden'

    def test_non_concert_event_excluded(self):
        evt = _apple_event('Dentist appointment', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt) == []

    def test_concert_keyword_in_description_passes_filter(self):
        evt = _apple_event('Local Band', description='concert tonight', dtstart=date(2024, 7, 1))
        assert len(self.client._parse(evt)) == 1

    def test_date_dtstart(self):
        evt = _apple_event('Radiohead @ MSG', dtstart=date(2024, 7, 4))
        assert self.client._parse(evt)[0].event_date == date(2024, 7, 4)

    def test_datetime_dtstart_converted_to_date(self):
        dt = datetime(2024, 7, 4, 20, 0, tzinfo=timezone.utc)
        evt = _apple_event('Radiohead @ MSG', dtstart=dt)
        assert self.client._parse(evt)[0].event_date == date(2024, 7, 4)

    def test_missing_location_defaults_to_unknown_venue(self):
        evt = _apple_event('Radiohead @ MSG', dtstart=date(2024, 7, 1), location=None)
        assert self.client._parse(evt)[0].venue == 'Unknown Venue'

    def test_no_summary_returns_empty(self):
        # vevent has no 'summary' attribute → treated as empty title → no artist extracted
        evt = _apple_event('', dtstart=date(2024, 7, 1))
        assert self.client._parse(evt) == []

    def test_multi_artist_title_returns_multiple_concerts(self):
        evt = _apple_event('Radiohead w/ Portishead @ MSG', dtstart=date(2024, 7, 1))
        result = self.client._parse(evt)
        assert len(result) == 2
        names = {c.artist_name for c in result}
        assert 'Radiohead' in names
        assert 'Portishead' in names

    def test_multi_artist_concerts_share_event_name(self):
        evt = _apple_event('Radiohead w/ Portishead @ MSG', dtstart=date(2024, 7, 1))
        result = self.client._parse(evt)
        assert all(c.event_name == 'Radiohead w/ Portishead @ MSG' for c in result)

    def test_unrecognised_dtstart_type_returns_empty(self):
        # dtstart.value that is neither date nor datetime → _parse returns []
        evt = _apple_event('Radiohead @ MSG', dtstart='not-a-date')
        assert self.client._parse(evt) == []

    def test_parse_exception_returns_empty(self):
        # Completely broken event object → exception caught → []
        assert self.client._parse(object()) == []


# ── AppleCalendarClient.get_concerts ─────────────────────────────────────────

class TestAppleCalendarGetConcerts:
    def test_caldav_not_available_returns_empty(self):
        client = AppleCalendarClient('user@icloud.com', 'pass')
        with patch('sources.apple_calendar.CALDAV_AVAILABLE', False):
            result = client.get_concerts(START, END)
        assert result == []

    def test_fetch_exception_returns_empty(self):
        client = AppleCalendarClient('user@icloud.com', 'pass')
        with patch.object(client, '_fetch', side_effect=Exception('connection refused')):
            result = client.get_concerts(START, END)
        assert result == []

    def test_fetch_returns_concert_list(self):
        concerts = [Concert('Radiohead @ MSG', 'Radiohead', date(2024, 7, 1), 'MSG', 'apple_calendar')]
        client = AppleCalendarClient('user@icloud.com', 'pass')
        with patch.object(client, '_fetch', return_value=concerts):
            result = client.get_concerts(START, END)
        assert result == concerts
