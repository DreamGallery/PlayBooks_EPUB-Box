"""Portable child-process setup and cooperative GUI cancellation."""
from __future__ import annotations

import _thread
from functools import wraps
import os
from pathlib import Path
import re
import subprocess
import sys
import threading


def safe_log_text(text) -> str:
    # Requests exceptions may include signed URLs, including relative URLs.
    return re.sub(r'([?&](?:sig|signature|token|access_token|key|auth|password)=)[^&\s\'"<>]*',
                  r'\1[hidden]', str(text), flags=re.I)


def cli_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        console = executable.with_name('python.exe')
        if console.is_file():
            return str(console)
    return str(executable)


def hidden_process_options() -> dict:
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def child_environment(cancel_file: Path) -> dict:
    return dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1',
                PLAYBOOKS_CANCEL_FILE=str(cancel_file))


def cancellable(main):
    @wraps(main)
    def run(*args, **kwargs):
        if os.name == 'nt':
            for stream in (sys.stdout, sys.stderr):
                if hasattr(stream, 'reconfigure'):
                    stream.reconfigure(encoding='utf-8', errors='replace')
        filename = os.environ.get('PLAYBOOKS_CANCEL_FILE')
        if not filename:
            return main(*args, **kwargs)
        stopped = threading.Event()

        def watch():
            while not stopped.wait(.2):
                if Path(filename).exists():
                    # Raise in the main thread so browser/authorization finally blocks run.
                    _thread.interrupt_main()
                    return

        watcher = threading.Thread(target=watch, daemon=True, name='gui-cancel')
        watcher.start()
        try:
            return main(*args, **kwargs)
        finally:
            stopped.set()
            watcher.join()
    return run
