#!/usr/bin/env python3
"""
Installs (or updates) the daily cron job for concert-playlist.

Safe to run multiple times — replaces any existing concert-playlist cron entry
rather than adding a duplicate.

Usage:
    python setup_cron.py            # install/update cron job
    python setup_cron.py --remove   # remove the cron job
    python setup_cron.py --status   # print current cron entry (if any)
"""

import argparse
import subprocess
import sys
from pathlib import Path

MARKER = '# concert-playlist'
LOG_DIR = Path.home() / '.concert-playlist'
LOG_FILE = LOG_DIR / 'cron.log'

PYTHON = Path(__file__).parent / '..' / 'opt' / 'anaconda3' / 'envs' / 'concert-playlist' / 'bin' / 'python'
# Use the same interpreter that runs this script if the conda env isn't found
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
else:
    PYTHON = PYTHON.resolve()

PROJECT_DIR = Path(__file__).parent.resolve()
MAIN = PROJECT_DIR / 'main.py'

# Daily at 9 AM
CRON_SCHEDULE = '0 9 * * *'
CRON_ENTRY = (
    f'{CRON_SCHEDULE}  cd {PROJECT_DIR} && '
    f'{PYTHON} {MAIN} --update '
    f'>> {LOG_FILE} 2>&1  {MARKER}'
)


def get_current_crontab() -> str:
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    # Exit code 1 with "no crontab" means empty — not an error
    if result.returncode != 0 and 'no crontab' not in result.stderr:
        print(f'Error reading crontab: {result.stderr}', file=sys.stderr)
        sys.exit(1)
    return result.stdout


def set_crontab(content: str) -> None:
    result = subprocess.run(['crontab', '-'], input=content, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'Error writing crontab: {result.stderr}', file=sys.stderr)
        sys.exit(1)


def install():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    current = get_current_crontab()
    lines = [l for l in current.splitlines() if MARKER not in l]
    lines.append(CRON_ENTRY)
    # Ensure trailing newline
    new_crontab = '\n'.join(lines).strip() + '\n'

    set_crontab(new_crontab)
    print('Cron job installed. Runs daily at 9 AM.')
    print(f'  {CRON_ENTRY}')
    print(f'\nLogs: {LOG_FILE}')


def remove():
    current = get_current_crontab()
    lines = [l for l in current.splitlines() if MARKER not in l]
    if len(lines) == len(current.splitlines()):
        print('No concert-playlist cron entry found — nothing to remove.')
        return
    new_crontab = '\n'.join(lines).strip()
    if new_crontab:
        new_crontab += '\n'
    set_crontab(new_crontab)
    print('Cron job removed.')


def status():
    current = get_current_crontab()
    entries = [l for l in current.splitlines() if MARKER in l]
    if entries:
        print('Active cron entry:')
        for e in entries:
            print(f'  {e}')
    else:
        print('No concert-playlist cron entry found.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Manage the concert-playlist cron job.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--remove', action='store_true', help='Remove the cron job')
    group.add_argument('--status', action='store_true', help='Print the current cron entry')
    args = parser.parse_args()

    if args.remove:
        remove()
    elif args.status:
        status()
    else:
        install()
