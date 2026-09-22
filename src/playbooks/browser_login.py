"""Human-only Google sign-in, separate from browser debugging sessions."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import time

from .adobe_backend import state_lock
from .runtime import hidden_process_options


def dedicated_profile(profile):
    profile = Path(profile).expanduser().resolve()
    home = Path.home().resolve()
    defaults = [home / 'Library/Application Support' / name for name in
                ('Google/Chrome', 'Google/Chrome Canary', 'Chromium', 'Microsoft Edge', 'BraveSoftware/Brave-Browser')]
    defaults += [home / '.config' / name for name in
                 ('google-chrome', 'chromium', 'microsoft-edge', 'BraveSoftware/Brave-Browser')]
    if os.environ.get('LOCALAPPDATA'):
        defaults += [Path(os.environ['LOCALAPPDATA']) / name / 'User Data' for name in
                     ('Google/Chrome', 'Google/Chrome SxS', 'Chromium', 'Microsoft/Edge', 'BraveSoftware/Brave-Browser')]
    if profile in (home, Path(profile.anchor)) or any(
            profile == p.resolve() or p.resolve() in profile.parents for p in defaults):
        raise ValueError('Use a dedicated Chrome profile, not your everyday browser data directory')
    return profile


@contextmanager
def profile_session(profile):
    profile = dedicated_profile(profile)
    with state_lock(profile / '.playbooks-session',
                    busy_message='Another PlayBooks task is using this Chrome profile. Close its browser and finish that task first.'):
        yield profile


def login_google(profile, browser=None):
    from .playbooks_hires import find_browser, _http_json
    executable = find_browser(browser)
    if not executable:
        raise RuntimeError('Chrome/Chromium not found; specify --browser')
    with profile_session(profile) as profile:
        discovery = profile / 'DevToolsActivePort'
        if discovery.is_file():
            try:
                port = int(discovery.read_text(encoding='utf-8').splitlines()[0])
                active = 1 <= port <= 65535 and bool(_http_json(f'http://127.0.0.1:{port}/json/version', timeout=2))
            except (OSError, ValueError, IndexError):
                active = False
            if active:
                raise RuntimeError('A debugging browser is using this profile. Close that browser before Google login.')
        args = [executable, f'--user-data-dir={profile}', '--new-window',
                '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
                'https://play.google.com/books']
        options = hidden_process_options()
        if os.name != 'nt':
            options['start_new_session'] = True
        print('Opening a normal browser for manual Google sign-in (no debugging or headless mode).', flush=True)
        print('Sign in yourself, then close ALL windows of this dedicated browser to continue. No login credentials are read by this command.', flush=True)
        started = time.monotonic()
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **options)
        try:
            while True:
                try:
                    result = proc.wait(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    pass
        except KeyboardInterrupt:
            print('Stopped waiting. The login browser was left open; close it manually before processing books.', flush=True)
            raise
        if result != 0 or time.monotonic() - started < 1:
            raise RuntimeError('Browser exited early or handed off to an existing instance. Close all browser windows using this profile, then retry Google login.')
        print('Login browser closed. Sign-in was not verified; processing will check access to the selected book.', flush=True)
