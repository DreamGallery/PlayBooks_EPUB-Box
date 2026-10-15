"""Translate known CLI messages for display only; preserve their original payloads."""
import re
from string import Formatter

from .gui_log_messages import MESSAGES


def _compile(template):
    parts = []
    for literal, field, _, _ in Formatter().parse(template):
        parts.append(re.escape(literal))
        if field:
            value = r'PIL|requests|cryptography|websocket|lxml|Crypto|flet' if field == 'module' else '.*?'
            parts.append(f'(?P<{field}>{value})')
    return re.compile(''.join(parts))


_RULES = [(_compile(source), dict(zip(('zh_CN', 'zh_TW', 'ja'), translations)))
          for source, *translations in MESSAGES]
_REASONS = ('ISBN match', 'Title and author match', 'Different volume/number',
            'Confirm title, author and edition')
_CANDIDATE = re.compile(r'(\d+\. .* \| .* \| ID: [\w-]+ \| )(' + '|'.join(_REASONS) + r')')
_ACTIONS = ('Skip', 'Unmatched', 'Keep', 'Failed', 'Replace')
_SUMMARY = re.compile(r'(?:' + '|'.join(_ACTIONS) + r') \d+(?:, (?:' + '|'.join(_ACTIONS) + r') \d+)*')
_REPORT_ROW = re.compile(
    r'(?P<prefix>\d+ +.+?\d+x\d+ +(?:\d+x\d+)? +(?:[\d.]+x)? +(?:[\d.]+)? +.*?)'
    r'(?P<action>Skip {4}|Unmatched|Keep {4}|Failed {2}|Replace ?)'
    r'(?P<note>(?:SVG / too small / unreadable|No matching Play Books image|'
    r'New image has no higher resolution|Gain below [\d.]+x|Format conversion failed: .*|'
    r'[A-Z0-9]+→[A-Z0-9]+)?(?: \[[^\[\]]+\])?)')
_HEADERS = ('#', 'EPUB image', 'Old size', 'New size', 'Gain', 'Dist.', 'Source (pg)', 'Action', 'Notes')
_WIDTHS = (4, 42, 12, 12, 8, 7, 34, 8, 0)
_ADE_STAGES = ('Failed to create ADE device', 'Failed to create ADE account authorization',
               'ADE sign-in failed', 'ADE device activation failed', 'ADE authorization failed',
               'ACSM fulfillment failed')
_ADE_ERROR = re.compile('(' + '|'.join(_ADE_STAGES) + r')(: E_[A-Z0-9_]+(?:, E_[A-Z0-9_]+)*| \(check your account, network and authorization\))')


def _pad(text, width):
    return text + ' ' * max(0, width - sum(2 if ord(ch) > 0x2E7F else 1 for ch in text))


_REPORT_HEADER = ''.join(_pad(text, width) for text, width in zip(_HEADERS, _WIDTHS))


def localize_log(raw, language):
    """Full-line matching avoids translating arbitrary words inside titles or paths."""
    if language not in ('zh_CN', 'zh_TW', 'ja'):
        return raw
    if raw.startswith('⚠ '):
        return '⚠ ' + localize_log(raw[2:], language)
    if raw.startswith('Failed: '):
        return _message('Failed', language) + ': ' + localize_log(raw[8:], language)
    if match := _ADE_ERROR.fullmatch(raw):
        return _message(match[1], language) + _message(match[2], language)
    if raw == _REPORT_HEADER:
        return ''.join(_pad(_message(text, language), width) for text, width in zip(_HEADERS, _WIDTHS))
    if match := _CANDIDATE.fullmatch(raw):
        return match[1] + _message(match[2], language)
    if raw.startswith('Summary: ') and _SUMMARY.fullmatch(raw[9:]):
        parts = [part.rsplit(' ', 1) for part in raw[9:].split(', ')]
        return _message('Summary', language) + ': ' + ', '.join(
            f'{_message(action, language)} {count}' for action, count in parts)
    if match := _REPORT_ROW.fullmatch(raw):
        note, via = match['note'], ''
        if tail := re.fullmatch(r'(.*)( \[[^\[\]]+\])', note):
            note, via = tail.groups()
        return match['prefix'] + _pad(_message(match['action'].strip(), language), 8) + _message(note, language) + via
    return _message(raw, language)


def _message(raw, language):
    for pattern, translations in _RULES:
        if match := pattern.fullmatch(raw):
            return translations[language].format(**match.groupdict())
    return raw
