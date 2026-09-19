"""Resolve source resources separately from the project's private runtime data."""
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
ROOT = SOURCE_DIR.parents[1]
VENDOR_DIR = SOURCE_DIR / 'vendor'
