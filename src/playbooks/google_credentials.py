"""Recoverable removal of this tool's dedicated Chromium login data."""
import os
from pathlib import Path
import re

from .adobe_backend import state_lock
from .browser_login import dedicated_profile, profile_session
from .google_library import save_session


def credential_targets(directory, project, protected=()):
    raw = Path(directory).expanduser()
    root = dedicated_profile(raw)
    project = Path(project).resolve()
    if not str(directory).strip() or raw.is_symlink() or root == project or root in project.parents:
        raise ValueError('Unsafe Chrome profile directory.')
    if not (root / '.playbooks-session' / '.lock').is_file():
        raise ValueError('This directory has not been used as a dedicated PlayBooks profile.')
    protected = [Path(p).expanduser().resolve() for p in protected if p]
    targets = []
    for path in sorted(root.iterdir()):
        if path.name not in ('Default', 'Guest Profile', 'System Profile', 'Local State') and not re.fullmatch(r'Profile \d+', path.name):
            continue
        if path.is_symlink() or path.resolve() != path:
            raise ValueError('Linked browser data cannot be cleared safely.')
        if any(path == p or path in p.parents or p in path.parents for p in protected):
            raise ValueError('Browser data overlaps protected book, output or authorization files.')
        if path.name == 'Local State':
            if not path.is_file():
                raise ValueError('Invalid Chrome local state.')
        elif not path.is_dir() or not (path / 'Preferences').is_file():
            raise ValueError('Unrecognized Chrome profile data.')
        targets.append(path)
    # Leave the encryption key until all profile directories have been moved.
    return root, sorted(targets, key=lambda p: p.name == 'Local State')


def ensure_browser_closed(root, targets):
    if (root / 'SingletonLock').exists() or (root / 'SingletonLock').is_symlink():
        raise RuntimeError('Close all Chrome windows using this profile before clearing credentials.')
    discovery = root / 'DevToolsActivePort'
    if discovery.is_file():
        from .playbooks_hires import _http_json
        try:
            port = int(discovery.read_text(encoding='utf-8').splitlines()[0])
        except (OSError, ValueError, IndexError):
            port = 0
        if 1 <= port <= 65535:
            try:
                active = bool(_http_json(f'http://127.0.0.1:{port}/json/version', timeout=2))
            except OSError:
                active = False
            if active:
                raise RuntimeError('Close the running browser before clearing credentials.')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for path in targets:
            files = [path] if path.is_file() else [path / name for name in
                ('Preferences', 'Cookies', 'Network/Cookies', 'Login Data', 'History')]
            for file in files:
                if not file.is_file():
                    continue
                handle = kernel.CreateFileW(str(file), 0x80000000, 0, None, 3, 0, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise RuntimeError('Browser data is in use. Close Chrome and retry.')
                kernel.CloseHandle(handle)


def trash_credentials(directory, project, state, protected=()):
    from send2trash import send2trash
    root, _ = credential_targets(directory, project, protected)
    with state_lock(Path(state).expanduser().resolve() / 'pipeline'), profile_session(root):
        root, targets = credential_targets(directory, project, protected)
        ensure_browser_closed(root, targets)
        for path in targets:
            send2trash(str(path))
        save_session(state, root, False)
    return len(targets)
