"""Tests for setup_cron's plist generation — no launchctl calls."""

import plistlib

import setup_cron


def _parse(xml: str) -> dict:
    return plistlib.loads(xml.encode())


class TestPlists:
    def test_update_runs_daily_as_a_scheduled_run(self):
        plist = _parse(setup_cron.update_plist())
        assert plist['Label'] == setup_cron.LABEL_UPDATE
        assert plist['ProgramArguments'][2:] == ['--update', '--cron']
        assert plist['StartCalendarInterval'] == [{'Hour': 9, 'Minute': 0}]

    def test_discover_is_gated_to_its_interval(self):
        plist = _parse(setup_cron.discover_plist())
        assert plist['Label'] == setup_cron.LABEL_DISCOVER
        assert plist['ProgramArguments'][2:] == [
            '--discover', '--cron', '--skip-if-ran-within', str(setup_cron.DISCOVER_EVERY_HOURS),
        ]

    def test_discover_ticks_divide_the_interval(self):
        # The gate can only fire on a tick, so ticks must be evenly spaced and
        # divide the interval — otherwise the real cadence drifts past it.
        hours = sorted(h for h, _ in setup_cron.DISCOVER_TIMES)
        gaps = {(b - a) % 24 for a, b in zip(hours, hours[1:] + hours[:1])}
        assert len(gaps) == 1
        assert setup_cron.DISCOVER_EVERY_HOURS % gaps.pop() == 0

    def test_calendar_intervals_not_start_interval(self):
        # StartInterval firings that fall during sleep are dropped, not caught up.
        for xml in (setup_cron.update_plist(), setup_cron.discover_plist()):
            assert 'StartInterval' not in _parse(xml)
