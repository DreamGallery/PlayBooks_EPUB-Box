#!/usr/bin/env python3
"""Entry point for the desktop GUI and standalone pipeline CLI."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from playbooks.playbooks_app import main

if __name__ == '__main__':
    raise SystemExit(main())
