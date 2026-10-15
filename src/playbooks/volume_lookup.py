"""Conservative Google volume lookup using public bibliographic metadata only."""
import json
import os
from pathlib import Path
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

ID = re.compile(r'^[A-Za-z0-9_-]{12}$')


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text or '').casefold()
                   if c.isalnum())


def metadata(opf):
    root = ET.fromstring(opf)
    def values(tag):
        return [e.text.strip() for e in root.iter() if e.tag.rsplit('}', 1)[-1] == tag and e.text and e.text.strip()]
    return (values('title') or [''])[0], values('creator')


def author_set(authors):
    if isinstance(authors, str):
        authors = [authors]
    return {normalize(a) for value in authors or [] if isinstance(value, str)
            for a in re.split(r'[,，、;；/／]|\s+(?:and|&)\s+', value) if normalize(a)}


def title_keys(title):
    text = unicodedata.normalize('NFKC', title or '')
    # A phonetic reading may be present only in the store title. Edition labels
    # and numbered parentheses are not disposable annotations.
    def reading(match):
        inner = match.group(1)
        if inner in ('ライトノベル', 'コミック', 'マンガ', 'ノベル'):
            return match.group(0)
        return '' if re.fullmatch(r'[\u3040-\u30ff\s・ー]+', inner) else match.group(0)
    without_reading = re.sub(r'\(([^()]*)\)', reading, text)
    def key(value):
        value = re.sub(r'第?\s*(\d+)\s*[巻卷]', r'\1', value)
        value = re.sub(r'\bvol(?:ume)?\.?\s*(\d+)\b', r'\1', value, flags=re.I)
        return normalize(value)
    return {key(text), key(without_reading)} - {''}


def numbers(title):
    return tuple(re.findall(r'\d+', unicodedata.normalize('NFKC', title or '')))


def isbn_set(values):
    result = set()
    for value in values:
        value = re.sub(r'^(?:urn:)?isbn:', '', str(value), flags=re.I)
        value = re.sub(r'[\s-]', '', value).upper()
        if re.fullmatch(r'\d{9}[\dX]', value):
            if sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(value)) % 11:
                continue
            value = '978' + value[:9]
            value += str(-sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(value)) % 10)
        if re.fullmatch(r'97[89]\d{10}', value) and not sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(value)) % 10:
            result.add(value)
    return result


def metadata_isbns(opf):
    root = ET.fromstring(opf)
    return isbn_set(e.text or '' for e in root.iter() if e.tag.rsplit('}', 1)[-1] == 'identifier')


def ranked_candidates(title, authors, candidates, isbns=()):
    ranked = {}
    for item in candidates:
        if not ID.fullmatch(str(item.get('id', ''))) or not item.get('title'):
            continue
        candidate_title = str(item['title'])
        same_title = bool(title_keys(title) & title_keys(candidate_title))
        same_author = bool(author_set(authors) & author_set(item.get('authors')))
        conflict = numbers(title) != numbers(candidate_title)
        same_isbn = bool(set(isbns) & isbn_set(item.get('isbns', [])))
        confident = not conflict and (same_isbn or same_title and same_author)
        similarity = max((SequenceMatcher(None, a, b).ratio() for a in title_keys(title)
                          for b in title_keys(candidate_title)), default=0)
        score = (100 if same_isbn else 95 if confident else similarity * 65 + same_author * 15 - conflict * 25)
        if similarity < .45 and not same_isbn:
            continue
        reason = 'ISBN match' if same_isbn else 'Title and author match' if confident else 'Different volume/number' if conflict else 'Confirm title, author and edition'
        value = dict(item, confident=confident, score=score, reason=reason)
        previous = ranked.get(item['id'])
        if previous is None or score > previous['score']:
            ranked[item['id']] = value
    return sorted(ranked.values(), key=lambda item: (-item['score'], item['id']))


def matching(title, authors, candidates, isbns=()):
    return {item['id']: item for item in ranked_candidates(title, authors, candidates, isbns) if item['confident']}


def unique(matches):
    if len(matches) > 1:
        raise RuntimeError('Multiple editions match the title and author; specify --id: ' + ', '.join(sorted(matches)))
    return next(iter(matches), None)


def local_candidates(work):
    for path in Path(work).glob('*/manifest.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8')).get('metadata', {})
            yield {'id': path.parent.name, 'title': data.get('title'), 'authors': data.get('authors')}
        except (ValueError, OSError, AttributeError):
            continue


def search_public(title, authors, session, isbns=()):
    """Books API first; public Play search + manifests on quota/coverage failure."""
    candidates = []
    queries = ['isbn:' + value for value in sorted(isbns)[:1]] + [title]
    for query in queries:
        params = {'q': query, 'maxResults': 40, 'printType': 'books'}
        if os.environ.get('GOOGLE_BOOKS_API_KEY'):
            params['key'] = os.environ['GOOGLE_BOOKS_API_KEY']
        try:
            r = session.get('https://www.googleapis.com/books/v1/volumes', params=params, timeout=(10, 20))
            r.raise_for_status()
            for item in r.json().get('items', []):
                v = item.get('volumeInfo', {})
                main, subtitle = v.get('title', ''), v.get('subtitle', '')
                full = main if not subtitle or normalize(subtitle) in normalize(main) else main + ' ' + subtitle
                candidates.append({'id': item.get('id'), 'title': full, 'authors': v.get('authors', []),
                    'isbns': [i.get('identifier', '') for i in v.get('industryIdentifiers', []) if i.get('type') in ('ISBN_10', 'ISBN_13')]})
        except (ValueError, OSError):
            break  # Do not repeat requests against a quota or network failure.
        if matching(title, authors, candidates, isbns):
            return candidates
    from lxml import html
    params = {'q': title, 'c': 'books', 'hl': 'ja', 'gl': 'JP'}
    try:
        r = session.get('https://play.google.com/store/search', params=params, timeout=(10, 20))
        r.raise_for_status()
    except OSError:
        if candidates:
            return candidates
        raise
    tree = html.fromstring(r.content.decode('utf-8', 'replace'))
    ids = []
    for href in tree.xpath('//a[contains(@href,"/store/books/details")]/@href'):
        vid = parse_qs(urlparse(href).query).get('id', [''])[0]
        if ID.fullmatch(vid) and vid not in ids:
            ids.append(vid)
    # Bound requests; full-title queries normally yield just a few candidates.
    for vid in ids[:12]:
        try:
            r = session.get(f'https://play.google.com/books/volumes/{vid}/manifest',
                            params={'source': 'ge-web-app', 'hl': 'ja'}, timeout=(10, 15))
            r.raise_for_status()
            v = r.json().get('metadata', {})
            candidates.append({'id': vid, 'title': v.get('title'), 'authors': v.get('authors')})
        except (ValueError, OSError):
            continue
    return candidates


def resolve_volume_id(opf, work, *, use_local=True, session=None, library=None):
    title, authors = metadata(opf)
    isbns = metadata_isbns(opf)
    if not normalize(title) or not (author_set(authors) or isbns):
        raise RuntimeError('Missing full title or author; specify --id for safe volume selection')
    if use_local:
        local = list(local_candidates(work))
        if library:
            try:
                local.extend(json.loads(Path(library).read_text(encoding='utf-8')).get('books', []))
            except (OSError, ValueError, AttributeError):
                pass
        local_matches = matching(title, authors, local, isbns)
        if len(local_matches) > 1:
            show_candidates(title, authors, local, isbns)
        vid = unique(local_matches)
        if vid:
            print('Volume ID from local records: ' + vid, flush=True)
            return vid
    print('Searching Google Books by full title and author...', flush=True)
    import requests
    try:
        candidates = search_public(title, authors, session or requests.Session(), isbns)
    except requests.RequestException:
        raise RuntimeError('Google Books search is unavailable. Retry later or specify --id; EPUB download cache retained') from None
    matches = matching(title, authors, candidates, isbns)
    if len(matches) != 1:
        show_candidates(title, authors, candidates, isbns)
    vid = unique(matches)
    if not vid:
        raise RuntimeError('No unique title/author match. Specify --id; no edition or adjacent volume will be guessed')
    print('Volume ID from public catalog: ' + vid, flush=True)
    return vid


def show_candidates(title, authors, candidates, isbns=()):
    ranked = ranked_candidates(title, authors, candidates, isbns)[:6]
    if not ranked:
        return
    print('Candidate volumes (confirm the edition or select from Google library):', flush=True)
    for index, item in enumerate(ranked, 1):
        names = item.get('authors') or []
        names = names if isinstance(names, str) else ', '.join(names)
        print(f"{index}. {item['title']} | {names} | ID: {item['id']} | {item['reason']}", flush=True)
        print(f"   https://play.google.com/store/books/details?id={item['id']}", flush=True)
