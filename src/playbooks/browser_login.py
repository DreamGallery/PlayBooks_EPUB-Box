"""Normal-browser sign-in followed by live, nonempty library verification."""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import subprocess
import sqlite3
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


def fresh_google_sign_in(profile, since):
    """Read only cookie names/timestamps, never cookie values or encrypted data."""
    profile = Path(profile).resolve()
    now = int((time.time() + 11644473600) * 1_000_000)
    threshold = int((since + 11644473600) * 1_000_000)
    for path in (profile / 'Default' / 'Network' / 'Cookies', profile / 'Default' / 'Cookies'):
        if not path.is_file() or path.is_symlink() or path.resolve() != path:
            continue
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
                db.execute('PRAGMA query_only=ON')
                columns = {row[1] for row in db.execute('PRAGMA table_info(cookies)')}
                stamp = 'last_update_utc' if 'last_update_utc' in columns else 'creation_utc'
                count, updated = db.execute(f'''
                    SELECT COUNT(DISTINCT name), MAX({stamp}) FROM cookies
                    WHERE host_key IN ('.google.com', 'google.com')
                      AND name IN ('SID', 'HSID') AND (expires_utc = 0 OR expires_utc > ?)
                ''', (now,)).fetchone()
                if count == 2 and updated is not None and updated >= threshold:
                    return True
        except (OSError, sqlite3.Error):
            # Locked or unsupported stores are not copied or decrypted.
            continue
    return False


def request_browser_close(proc):
    if proc.poll() is not None:
        return True
    if os.name != 'nt':
        proc.terminate()  # SIGTERM to this Popen child only; never kill by browser name.
        return True
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    posted = []

    @callback_type
    def close_window(window, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == proc.pid:
            posted.append(bool(user32.PostMessageW(window, 0x0010, 0, 0)))  # WM_CLOSE
        return True

    user32.EnumWindows(close_window, 0)
    return any(posted)


def login_browser(profile, browser=None):
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
        args = [executable, f'--user-data-dir={profile}', '--profile-directory=Default', '--new-window',
                '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
                'https://play.google.com/books']
        options = hidden_process_options()
        if os.name != 'nt':
            options['start_new_session'] = True
        print('Opening a normal browser for manual Google sign-in (no debugging or headless mode).', flush=True)
        print('Sign in yourself. New sign-in markers will trigger automatic closure of this dedicated browser only.', flush=True)
        print('Detection may take a little while after sign-in while Chrome saves its state.', flush=True)
        print('Passwords and cookie values are not read. If automatic detection is unavailable, close this browser manually to continue.', flush=True)
        started = time.monotonic()
        since = time.time()
        next_check, candidate_since = started + 2, None
        auto_closed, monitor = False, True
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **options)
        try:
            while True:
                try:
                    result = proc.wait(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    if not monitor or now < next_check:
                        continue
                    next_check = now + 2
                    if not fresh_google_sign_in(profile, since):
                        candidate_since = None
                        continue
                    if candidate_since is None:
                        candidate_since = now
                    if now - candidate_since < 5:
                        continue
                    print('New sign-in markers detected. Closing only the browser opened for this login...', flush=True)
                    monitor = False
                    try:
                        if request_browser_close(proc):
                            result = proc.wait(timeout=15)
                            auto_closed = True
                            break
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    print('Automatic closure could not finish. Close this dedicated browser manually to continue.', flush=True)
        except KeyboardInterrupt:
            print('Stopped waiting. The login browser was left open; close it manually before processing books.', flush=True)
            raise
        if (result != 0 and not (auto_closed and result == -15)) or time.monotonic() - started < 1:
            raise RuntimeError('Browser exited early or handed off to an existing instance. Close all browser windows using this profile, then retry Google login.')
        print('Login browser closed. Checking the live Play library next.', flush=True)


def login_google(profile, browser=None, state=None):
    from .google_library import EmptyLibraryError, refresh_library, save_session
    from .paths import ROOT
    profile = dedicated_profile(profile)
    state = Path(state if state is not None else ROOT / '.playbooks-state').expanduser().resolve()
    with state_lock(state / 'pipeline'):
        save_session(state, profile, False)
        while True:
            # Release the normal browser's profile lock before opening the verifier.
            login_browser(profile, browser)
            print('Verifying sign-in by refreshing the library in the background...', flush=True)
            try:
                catalog = refresh_library(profile, state, browser, headless=True, require_books=True)
                if not catalog.get('books'):
                    raise EmptyLibraryError('No books found in the live library.')
            except EmptyLibraryError:
                save_session(state, profile, False)
                print('The library is empty. Sign-in is not complete; reopening the dedicated login browser. Check the account or cancel this task.', flush=True)
            except Exception:
                save_session(state, profile, False)
                print('Library verification failed. Reopening the dedicated login browser. Check sign-in and network access, or cancel this task.', flush=True)
            else:
                print(f'Google sign-in verified: {len(catalog["books"])} books loaded. Login browser closed.', flush=True)
                return catalog
