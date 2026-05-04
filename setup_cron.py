#!/usr/bin/env python3
"""
Installs (or updates) the daily cron job(s) for concert-playlist.

Safe to run multiple times — replaces any existing concert-playlist cron
entries rather than adding duplicates.

Usage:
    python setup_cron.py            # interactive: choose which playlists to schedule
    python setup_cron.py --remove   # remove all concert-playlist cron entries
    python setup_cron.py --status   # print current cron entries (if any)
"""

import argparse
import subprocess
import sys
from pathlib import Path

MARKER_UPDATE   = '# concert-playlist-update'
MARKER_DISCOVER = '# concert-playlist-discover'
ALL_MARKERS     = (MARKER_UPDATE, MARKER_DISCOVER)

LOG_DIR  = Path.home() / '.concert-playlist'
LOG_FILE = LOG_DIR / 'cron.log'

PYTHON = Path(__file__).parent / '..' / 'opt' / 'anaconda3' / 'envs' / 'concert-playlist' / 'bin' / 'python'
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
else:
    PYTHON = PYTHON.resolve()

PROJECT_DIR = Path(__file__).parent.resolve()
MAIN        = PROJECT_DIR / 'main.py'

CRON_SCHEDULE = '0 9 * * *'   # daily at 9 AM


def _entry(flag: str, marker: str) -> str:
    return (
        f'{CRON_SCHEDULE}  cd {PROJECT_DIR} && '
        f'{PYTHON} {MAIN} {flag} --cron '
        f'>> {LOG_FILE} 2>&1  {marker}'
    )


UPDATE_ENTRY   = _entry('--update',   MARKER_UPDATE)
DISCOVER_ENTRY = _entry('--discover', MARKER_DISCOVER)


def get_current_crontab() -> str:
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if result.returncode != 0 and 'no crontab' not in result.stderr:
        print(f'Error reading crontab: {result.stderr}', file=sys.stderr)
        sys.exit(1)
    return result.stdout


def set_crontab(content: str) -> None:
    result = subprocess.run(['crontab', '-'], input=content, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'Error writing crontab: {result.stderr}', file=sys.stderr)
        sys.exit(1)


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = '[Y/n]' if default else '[y/N]'
    answer = input(f'{prompt} {hint}: ').strip().lower()
    if not answer:
        return default
    return answer in ('y', 'yes')


def install() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print('\n=== Concert Playlist — Cron Setup ===\n')
    run_update   = _ask_yes_no('Schedule daily prep playlist update (--update)?', default=True)
    run_discover = _ask_yes_no('Schedule daily discovery playlist update (--discover)?', default=False)

    if not run_update and not run_discover:
        print('Nothing selected — no cron entries installed.')
        return

    # Strip all existing concert-playlist entries
    current = get_current_crontab()
    lines = [l for l in current.splitlines() if not any(m in l for m in ALL_MARKERS)]

    if run_update:
        lines.append(UPDATE_ENTRY)
    if run_discover:
        lines.append(DISCOVER_ENTRY)

    new_crontab = '\n'.join(lines).strip() + '\n'
    set_crontab(new_crontab)

    print('\nCron job(s) installed. Each runs daily at 9 AM.')
    if run_update:
        print(f'  [prep]      {UPDATE_ENTRY}')
    if run_discover:
        print(f'  [discover]  {DISCOVER_ENTRY}')
    print(f'\nLogs: {LOG_FILE}')


def remove() -> None:
    current = get_current_crontab()
    lines = [l for l in current.splitlines() if not any(m in l for m in ALL_MARKERS)]
    if len(lines) == len(current.splitlines()):
        print('No concert-playlist cron entries found — nothing to remove.')
        return
    new_crontab = '\n'.join(lines).strip()
    if new_crontab:
        new_crontab += '\n'
    set_crontab(new_crontab)
    print('Concert-playlist cron entries removed.')


def status() -> None:
    current = get_current_crontab()
    entries = [l for l in current.splitlines() if any(m in l for m in ALL_MARKERS)]
    if entries:
        print('Active cron entries:')
        for e in entries:
            print(f'  {e}')
    else:
        print('No concert-playlist cron entries found.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Manage the concert-playlist cron job(s).')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--remove', action='store_true', help='Remove all cron entries')
    group.add_argument('--status', action='store_true', help='Print current cron entries')
    args = parser.parse_args()

    if args.remove:
        remove()
    elif args.status:
        status()
    else:
        install()
