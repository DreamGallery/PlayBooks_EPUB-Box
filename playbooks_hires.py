#!/usr/bin/env python3
"""Compatibility entry point for fetch / replace / run / inspect."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from playbooks.playbooks_hires import main

if __name__ == '__main__':
    raise SystemExit(main())
