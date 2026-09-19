#!/usr/bin/env python3
"""Direct desktop GUI entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from playbooks.playbooks_flet import main

if __name__ == '__main__':
    main()
