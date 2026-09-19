"""Conservative Google volume lookup using public bibliographic metadata only."""
import json
import os
from pathlib import Path
import re
import unicodedata
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
        authors = re.split(r'[,，、;；]', authors)
    return {normalize(a) for a in authors or [] if normalize(a)}


def matching(title, authors, candidates):
    matches = {}
    for item in candidates:
        if (ID.fullmatch(str(item.get('id', ''))) and normalize(title) == normalize(item.get('title', ''))
                and author_set(authors) & author_set(item.get('authors'))):
            matches[item['id']] = item
    return matches


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


def search_public(title, authors, session):
    """Books API first; public Play search + manifests on quota/coverage failure."""
    candidates = []
    params = {'q': title, 'maxResults': 40, 'printType': 'books'}
    if os.environ.get('GOOGLE_BOOKS_API_KEY'):
        params['key'] = os.environ['GOOGLE_BOOKS_API_KEY']
    try:
        r = session.get('https://www.googleapis.com/books/v1/volumes', params=params, timeout=(10, 20))
        r.raise_for_status()
        for item in r.json().get('items', []):
            v = item.get('volumeInfo', {})
            main, subtitle = v.get('title', ''), v.get('subtitle', '')
            # APIs may either split subtitle or include it in title.
            for text in (main, main + ' ' + subtitle):
                candidates.append({'id': item.get('id'), 'title': text, 'authors': v.get('authors', [])})
    except (ValueError, OSError):
        pass
    if matching(title, authors, candidates):
        return candidates
    from lxml import html
    params = {'q': title, 'c': 'books', 'hl': 'ja', 'gl': 'JP'}
    r = session.get('https://play.google.com/store/search', params=params, timeout=(10, 20))
    r.raise_for_status()
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


def resolve_volume_id(opf, work, *, use_local=True, session=None):
    title, authors = metadata(opf)
    if not normalize(title) or not author_set(authors):
        raise RuntimeError('Missing full title or author; specify --id for safe volume selection')
    if use_local:
        vid = unique(matching(title, authors, local_candidates(work)))
        if vid:
            print('Volume ID from local records: ' + vid, flush=True)
            return vid
    print('Searching Google Books by full title and author...', flush=True)
    import requests
    try:
        candidates = search_public(title, authors, session or requests.Session())
    except requests.RequestException:
        raise RuntimeError('Google Books search is unavailable. Retry later or specify --id; EPUB download cache retained') from None
    vid = unique(matching(title, authors, candidates))
    if not vid:
        raise RuntimeError('No unique title/author match. Specify --id; no edition or adjacent volume will be guessed')
    print('Volume ID from public catalog: ' + vid, flush=True)
    return vid
