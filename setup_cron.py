#!/usr/bin/env python3
"""
Installs (or updates) the daily launchd job(s) for concert-playlist.

Uses ~/Library/LaunchAgents/ plists so jobs fire at the scheduled time and
catch up at next wake if the Mac was asleep when the schedule fired.

Safe to run multiple times — replaces any existing concert-playlist entries
rather than adding duplicates.  Removes legacy cron entries on install.

Usage:
    python setup_cron.py            # interactive: choose which playlists to schedule
    python setup_cron.py --remove   # unload and remove all concert-playlist plists
    python setup_cron.py --status   # show what's currently installed / running
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ── Paths & labels ────────────────────────────────────────────────────────────

LABEL_UPDATE   = 'com.concert-playlist.update'
LABEL_DISCOVER = 'com.concert-playlist.discover'

LAUNCH_AGENTS = Path.home() / 'Library' / 'LaunchAgents'
PLIST_UPDATE  = LAUNCH_AGENTS / f'{LABEL_UPDATE}.plist'
PLIST_DISCOVER = LAUNCH_AGENTS / f'{LABEL_DISCOVER}.plist'

LOG_DIR  = Path.home() / '.concert-playlist'
LOG_FILE = LOG_DIR / 'launchd.log'

PROJECT_DIR = Path(__file__).parent.resolve()
MAIN        = PROJECT_DIR / 'main.py'

PYTHON = Path.home() / 'opt' / 'anaconda3' / 'envs' / 'concert-playlist' / 'bin' / 'python3.12'
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
else:
    PYTHON = PYTHON.resolve()

# Prep runs daily. Discovery changes slowly (Ticketmaster listings, not your
# calendar), so it runs every DISCOVER_EVERY_HOURS instead. launchd can't
# express that directly: StartInterval drops any firing that falls while the
# Mac is asleep — the same gap that ruled out cron. So discovery ticks every
# few hours on the calendar, which launchd does catch up at wake, and main.py's
# --skip-if-ran-within turns all but the due tick into a no-op. The real
# cadence is DISCOVER_EVERY_HOURS rounded up to the next tick.
UPDATE_TIMES          = [(9, 0)]
DISCOVER_TIMES        = [(3, 0), (9, 0), (15, 0), (21, 0)]
DISCOVER_EVERY_HOURS  = 60

# Legacy cron markers to clean up on install
_CRON_MARKERS = ('# concert-playlist',)


# ── Plist generation ──────────────────────────────────────────────────────────

def _calendar_interval(times: list[tuple[int, int]]) -> str:
    entries = ''.join(
        f"""
        <dict>
            <key>Hour</key>
            <integer>{hour}</integer>
            <key>Minute</key>
            <integer>{minute}</integer>
        </dict>"""
        for hour, minute in times
    )
    return f'<array>{entries}\n    </array>'


def _plist(label: str, args: list[str], times: list[tuple[int, int]]) -> str:
    arguments = ''.join(f'\n        <string>{a}</string>' for a in [str(PYTHON), str(MAIN), *args])
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>{arguments}
    </array>

    <key>WorkingDirectory</key>
    <string>{PROJECT_DIR}</string>

    <key>StartCalendarInterval</key>
    {_calendar_interval(times)}

    <key>StandardOutPath</key>
    <string>{LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_FILE}</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def update_plist() -> str:
    return _plist(LABEL_UPDATE, ['--update', '--cron'], UPDATE_TIMES)


def discover_plist() -> str:
    return _plist(
        LABEL_DISCOVER,
        ['--discover', '--cron', '--skip-if-ran-within', str(DISCOVER_EVERY_HOURS)],
        DISCOVER_TIMES,
    )


# ── launchctl helpers ─────────────────────────────────────────────────────────

def _uid() -> int:
    return os.getuid()


def _load(plist: Path) -> None:
    """Load a plist into the user's launchd domain."""
    subprocess.run(
        ['launchctl', 'bootstrap', f'gui/{_uid()}', str(plist)],
        check=True, capture_output=True,
    )


def _unload(label: str) -> None:
    """Unload a service by label, ignoring 'not found' errors."""
    result = subprocess.run(
        ['launchctl', 'bootout', f'gui/{_uid()}/{label}'],
        capture_output=True,
    )
    # Exit code 36 = ESRCH — service not loaded, which is fine
    if result.returncode not in (0, 36):
        # Try by plist path as a fallback
        plist = LAUNCH_AGENTS / f'{label}.plist'
        if plist.exists():
            subprocess.run(
                ['launchctl', 'bootout', f'gui/{_uid()}', str(plist)],
                capture_output=True,
            )


def _is_loaded(label: str) -> bool:
    result = subprocess.run(
        ['launchctl', 'list', label],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# ── Legacy cron cleanup ───────────────────────────────────────────────────────

def _remove_legacy_cron() -> bool:
    """Strip any old concert-playlist cron entries. Returns True if any were removed."""
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    lines = result.stdout.splitlines()
    kept  = [l for l in lines if not any(m in l for m in _CRON_MARKERS)]
    if len(kept) == len(lines):
        return False
    new_crontab = '\n'.join(kept).strip()
    if new_crontab:
        new_crontab += '\n'
    subprocess.run(['crontab', '-'], input=new_crontab, text=True, check=True)
    return True


# ── User prompts ──────────────────────────────────────────────────────────────

def _times(times: list[tuple[int, int]]) -> str:
    return ', '.join(f'{h:02d}:{m:02d}' for h, m in times)


def _ask(prompt: str, default: bool = True) -> bool:
    hint = '[Y/n]' if default else '[y/N]'
    answer = input(f'{prompt} {hint}: ').strip().lower()
    if not answer:
        return default
    return answer in ('y', 'yes')


# ── Commands ──────────────────────────────────────────────────────────────────

def install() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    print('\n=== Concert Playlist — Scheduler Setup ===\n')
    run_update   = _ask('Schedule daily prep playlist update (--update)?',     default=True)
    run_discover = _ask(f'Schedule discovery playlist update every {DISCOVER_EVERY_HOURS}h (--discover)?',
                        default=False)

    if not run_update and not run_discover:
        print('Nothing selected — no jobs installed.')
        return

    # Remove legacy cron entries
    if _remove_legacy_cron():
        print('Removed legacy cron entries.')

    installed = []

    if run_update:
        _unload(LABEL_UPDATE)
        PLIST_UPDATE.write_text(update_plist())
        _load(PLIST_UPDATE)
        installed.append(f'  [prep]      daily at {_times(UPDATE_TIMES)} — {PLIST_UPDATE}')

    if run_discover:
        _unload(LABEL_DISCOVER)
        PLIST_DISCOVER.write_text(discover_plist())
        _load(PLIST_DISCOVER)
        installed.append(f'  [discover]  every {DISCOVER_EVERY_HOURS}h, checked at {_times(DISCOVER_TIMES)} — {PLIST_DISCOVER}')

    print('\nInstalled. Jobs fire at the scheduled time, or at next wake if the Mac was asleep.')
    for line in installed:
        print(line)
    print(f'\nLogs: {LOG_FILE}')


def remove() -> None:
    removed_any = False

    for label, plist in [(LABEL_UPDATE, PLIST_UPDATE), (LABEL_DISCOVER, PLIST_DISCOVER)]:
        if plist.exists() or _is_loaded(label):
            _unload(label)
            if plist.exists():
                plist.unlink()
            print(f'Removed: {plist.name}')
            removed_any = True

    if _remove_legacy_cron():
        print('Removed legacy cron entries.')
        removed_any = True

    if not removed_any:
        print('No concert-playlist jobs found — nothing to remove.')


def status() -> None:
    found_any = False

    for label, plist in [(LABEL_UPDATE, PLIST_UPDATE), (LABEL_DISCOVER, PLIST_DISCOVER)]:
        if plist.exists():
            loaded = _is_loaded(label)
            state  = 'loaded' if loaded else 'plist exists but not loaded'
            print(f'  {label}: {state}')
            print(f'    plist: {plist}')
            found_any = True

    legacy = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
    cron_lines = [l for l in legacy.splitlines() if any(m in l for m in _CRON_MARKERS)]
    if cron_lines:
        print('\n  Legacy cron entries (run --remove or install to clean up):')
        for l in cron_lines:
            print(f'    {l}')
        found_any = True

    if not found_any:
        print('No concert-playlist jobs found.')


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Manage concert-playlist launchd jobs.')
    group  = parser.add_mutually_exclusive_group()
    group.add_argument('--remove', action='store_true', help='Unload and remove all jobs')
    group.add_argument('--status', action='store_true', help='Show current job status')
    args = parser.parse_args()

    if args.remove:
        remove()
    elif args.status:
        status()
    else:
        install()
