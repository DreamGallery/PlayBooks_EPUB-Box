"""Conservative title-based grouping; original titles and volume IDs stay intact."""
import hashlib
import re
import unicodedata


PROMO = re.compile(r'【[^】]*(?:電子版限定|電子特別|電子限定|電子特典|特典付き|限定SS)[^】]*】', re.I)
NUMBER = r'\d+(?:\.\d+)?'
VOLUME = re.compile(
    rf'(?:第\s*{NUMBER}\s*[巻卷集冊册]|(?<=\s)(?:vol(?:ume)?|book|ep(?:isode)?)\.?\s*{NUMBER}|'
    rf'\(\s*{NUMBER}\s*\)|{NUMBER}(?:\s*[巻卷集冊册])?)(?=$|\s|[:：〜～~「『【])', re.I)
ROMAN = re.compile(r'(?<=\s)[IVXLCDM]+(?=$|\s|[:：〜～~「『【])')
VARIANTS = (
    (r'コミック|マンガ|漫画|漫畫|\bmanga\b|\bcomic\b', 'コミック'),
    (r'ライトノベル|小説|小說|小说|\bnovel\b', '小説'),
    (r'外伝|外傳|外传|\bside story\b', '外伝'),
    (r'短編集|短篇集|\bshort stories\b', '短編集'),
    (r'番外|スピンオフ|\bspin[ -]?off\b', '番外編'),
    (r'新装版|新裝版', '新装版'),
    (r'完全版', '完全版'),
    (r'合本|\bomnibus\b', '合本版'),
)


def natural_title(title):
    text = unicodedata.normalize('NFKC', title).casefold()
    return tuple((1, float(part)) if re.fullmatch(NUMBER, part) else (0, part)
                 for part in re.split(rf'({NUMBER})', text))


def roman_number(value):
    if not re.fullmatch(r'X{0,3}(?:IX|IV|V?I{0,3})', value) or not value:
        return None
    values = {'I': 1, 'V': 5, 'X': 10}
    return sum(-values[c] if i + 1 < len(value) and values[c] < values[value[i + 1]] else values[c]
               for i, c in enumerate(value))


def title_group(title):
    text = ' '.join(PROMO.sub('', unicodedata.normalize('NFKC', title)).split())
    candidates = [(match, float(re.search(NUMBER, match.group()).group())) for match in VOLUME.finditer(text)]
    candidates += [(match, roman_number(match.group())) for match in ROMAN.finditer(text)]
    for match, number in sorted(candidates, key=lambda item: item[0].start()):
        prefix = text[:match.start()].rstrip()
        if number is None or len(prefix) < 2:
            continue
        # Keep title-internal numbers such as “Catch-22” and “100の異世界で2周目”.
        if match.start() and text[match.start() - 1] in '-0123456789.':
            continue
        order = [number]
        group = prefix
        if number == 0:
            group += ' 0'
            continuation = re.match(r'\s+([IVXLCDM]+)(?=\s|$)', text[match.end():])
            if continuation and (part := roman_number(continuation[1])):
                order.append(part)
        suffix = text[match.end():]
        for pattern, label in VARIANTS:
            if re.search(pattern, suffix, re.I) and not re.search(pattern, group, re.I):
                group += f' ({label})'
        return group, order
    return text or title, []


def group_by_title(books):
    result = []
    for book in books:
        title, order = title_group(book['title'])
        key = re.sub(r'\s+', '', title).casefold()
        result.append(dict(book, series_id='title:' + hashlib.sha256(key.encode()).hexdigest()[:16],
                           series_title=title, series_order=order))
    return result


def book_order(book):
    return tuple(book.get('series_order') or [0]), natural_title(book['title'])
