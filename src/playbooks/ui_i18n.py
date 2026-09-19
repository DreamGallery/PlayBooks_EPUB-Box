"""Explicit UI strings; book metadata and English process logs are never translated."""
from dataclasses import fields, is_dataclass
from pathlib import Path
import json

LANGUAGES = {'zh_CN': '简体中文', 'zh_TW': '繁體中文', 'en': 'English', 'ja': '日本語'}
CATALOG = json.loads(Path(__file__).with_name('ui_translations.json').read_text(encoding='utf-8'))


class UIString(str):
    def __new__(cls, value, key, values):
        obj = super().__new__(cls, value)
        obj.key, obj.values = key, values
        return obj


def translate(language, key, **values):
    template = key if language == 'zh_CN' else CATALOG[key][language]
    return UIString(template.format(**values) if values else template, key, values)


def refresh_ui(root, language):
    """Only update explicitly tagged strings; never touch raw input or log values."""
    seen = set()
    def visit(obj):
        if isinstance(obj, UIString):
            return translate(language, obj.key, **obj.values)
        if id(obj) in seen:
            return obj
        seen.add(id(obj))
        if isinstance(obj, list):
            for i, value in enumerate(obj):
                obj[i] = visit(value)
        elif isinstance(obj, dict):
            for key, value in list(obj.items()):
                obj[key] = visit(value)
        elif is_dataclass(obj):
            for field in fields(obj):
                if field.name.startswith('_') or field.name in ('data', 'ref'):
                    continue
                value = getattr(obj, field.name, None)
                updated = visit(value)
                if isinstance(value, UIString):
                    setattr(obj, field.name, updated)
        return obj
    visit(root)
