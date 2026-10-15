"""Read the signed-in library and use its official EPUB export controls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET
import zipfile

from .adobe_backend import private_dir
from .playbooks_hires import BrowserAuth, _http_json
from .volume_lookup import ID
from .library_grouping import group_by_title
from .book_typography import source_language

LIBRARY_URL = 'https://play.google.com/books?hl=en'
CATALOG_EVENT = 'Library catalog updated.'

SNAPSHOT_JS = r"""(() => {
  const anchors = [...document.querySelectorAll('a[href]')];
  const links = [];
  for (const a of anchors) {
    const u = new URL(a.href, location.href);
    if (u.hostname !== 'play.google.com' || u.pathname !== '/books/reader') continue;
    const text = (a.textContent || '').trim() || a.getAttribute('aria-label') ||
                 a.querySelector('img')?.getAttribute('alt') || '';
    if (!text) continue;
    let authors = [];
    for (let p = a.parentElement; p && p !== document.body; p = p.parentElement) {
      const ids = new Set([...p.querySelectorAll('a[href*="/books/reader"]')]
        .map(e => new URL(e.href).searchParams.get('id')));
      if (ids.size > 1) break;
      const names = [...p.querySelectorAll('a[href*="/store/books/author"]')]
        .map(e => new URL(e.href).searchParams.get('id') || (e.textContent || '').trim()).filter(Boolean);
      if (names.length) { authors = names; break; }
    }
    links.push({href: u.href, title: text, authors});
  }
  return {links, href: location.href, ready: document.readyState, signed_out: location.hostname === 'accounts.google.com' ||
    anchors.some(a => a.getClientRects().length && getComputedStyle(a).visibility !== 'hidden' &&
      /accounts\.google\.com\/(?:ServiceLogin|signin)/.test(a.href))};
})()"""

SCROLL_JS = r"""(() => {
  const roots = [document.scrollingElement];
  for (const a of document.querySelectorAll('a[href*="/books/reader"]')) {
    for (let p = a.parentElement; p; p = p.parentElement) {
      if (p.scrollHeight > p.clientHeight + 8 && /auto|scroll/.test(getComputedStyle(p).overflowY))
        roots.push(p);
    }
  }
  let bottom = true;
  for (const p of new Set(roots.filter(Boolean))) {
    p.scrollTop += Math.max(p.clientHeight * .85, 300);
    if (p.scrollTop + p.clientHeight < p.scrollHeight - 4) bottom = false;
  }
  return bottom;
})()"""

MENU_JS = r"""(id => {
  for (const a of document.querySelectorAll('a[href*="/books/reader"]')) {
    if (new URL(a.href).searchParams.get('id') !== id) continue;
    for (let p = a.parentElement; p && p !== document.body; p = p.parentElement) {
      const ids = new Set([...p.querySelectorAll('a[href*="/books/reader"]')]
        .map(e => new URL(e.href).searchParams.get('id')));
      if (ids.size > 1) break;
      const button = [...p.querySelectorAll('[aria-haspopup]')].find(e =>
        /more|更多|その他|オプション/i.test(e.getAttribute('aria-label') || e.textContent || ''));
      if (button) { button.scrollIntoView({block:'center'}); button.click(); return true; }
    }
  }
  return false;
})(%s)"""

EXPORT_JS = r"""(() => {
  const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const item = [...document.querySelectorAll('[role="menuitem"]')].find(e =>
    visible(e) && /^(Export|导出|匯出|エクスポート)$/i.test(e.textContent.trim()));
  if (!item) return false;
  item.click(); return true;
})()"""

EPUB_JS = r"""(() => {
  const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const button = [...document.querySelectorAll('button,[role="button"],a[href]')].find(e =>
    visible(e) && /epub/i.test(e.textContent) && /export|download|导出|匯出|エクスポート|ダウンロード/i.test(e.textContent));
  if (!button) return false;
  button.click(); return true;
})()"""


def clean_text(value):
    return ' '.join(str(value or '').split())[:1000]


def parse_links(links):
    result = {}
    for link in links:
        url = urlparse(link.get('href', ''))
        if url.scheme != 'https' or url.hostname != 'play.google.com':
            continue
        if url.path == '/books/reader':
            key = parse_qs(url.query).get('id', [''])[0]
            if not ID.fullmatch(key):
                continue
        else:
            continue
        title = clean_text(link.get('title'))
        if title:
            previous = result.get(key, {})
            result[key] = {'id': key, 'title': title,
                           'authors': [clean_text(a) for a in link.get('authors', []) if isinstance(a, str)] or previous.get('authors', [])}
    return result


def write_private_json(path, data):
    path = Path(path)
    private_dir(path.parent)
    temp = path.with_suffix(path.suffix + '.part')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        if os.name != 'nt':
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_catalog(state, profile):
    """Never reuse another profile's owned-book list, including legacy catalogs."""
    try:
        folder = Path(state) / 'library'
        catalog = json.loads((folder / 'catalog.json').read_text(encoding='utf-8'))
        owner = catalog.get('profile')
        if owner is None:
            owner = json.loads((folder / 'session.json').read_text(encoding='utf-8')).get('profile')
        if owner != str(Path(profile).expanduser().resolve()):
            return {}
        books = catalog['books']
        if not isinstance(books, list) or any(not isinstance(book, dict) or
                not ID.fullmatch(str(book.get('id', ''))) or not isinstance(book.get('title'), str) for book in books):
            return {}
        for book in books:
            if not book.get('language'):
                for suffix in ('.epub', '.acsm'):
                    language = source_language(folder / 'exports' / book['id'] / ('source' + suffix))
                    if language:
                        book['language'] = language
                        break
        catalog['books'] = group_by_title(books)
        return catalog
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


class SignedOutError(RuntimeError):
    pass


class EmptyLibraryError(RuntimeError):
    pass


def save_session(state, profile, signed_in):
    write_private_json(Path(state) / 'library' / 'session.json', {
        'profile': str(Path(profile).expanduser().resolve()),
        'signed_in': signed_in, 'checked_at': time.time(),
    })


class LibraryBrowser(BrowserAuth):
    def __init__(self, profile, browser=None, headless=True):
        super().__init__('library', profile, browser_path=browser, headless=headless)
        self.reader_url = LIBRARY_URL
        self.target_id = None

    def _pick_target(self):
        target = _http_json(self.base + '/json/new?' + LIBRARY_URL, method='PUT')
        self.target_id = target.get('id')
        return target

    def _close(self, quit_browser=True):
        if self.proc is None and self.page and self.target_id:
            try:
                self.page.call('Target.closeTarget', {'targetId': self.target_id})
            except Exception:
                pass
        super()._close(quit_browser)

    def scan(self, url, allow_empty=False):
        self.page.call('Page.navigate', {'url': url})
        found, stable, last_count = {}, 0, -1
        deadline = time.monotonic() + 90
        started = time.monotonic()
        while time.monotonic() < deadline:
            state = self.page.evaluate(SNAPSHOT_JS) or {}
            if state.get('signed_out'):
                raise SignedOutError('Google sign-in required. Use Play library > Sign in; the library refreshes after the login browser closes.')
            if state.get('ready') == 'loading' or urlparse(state.get('href', '')).path != urlparse(url).path:
                time.sleep(.5)
                continue
            found.update(parse_links(state.get('links', [])))
            bottom = self.page.evaluate(SCROLL_JS)
            stable = stable + 1 if bottom and len(found) == last_count else 0
            last_count = len(found)
            if stable >= 5 and (found or allow_empty and time.monotonic() - started > 8):
                return found
            time.sleep(.5)
        raise RuntimeError('Library loading did not complete. Refresh later; the previous catalog was not replaced.')

    def wait_click(self, script, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.page.evaluate(script):
                return
            time.sleep(.25)
        raise RuntimeError('Official EPUB export is not available for this book. Select a local ACSM/EPUB instead.')

    def export(self, vid, folder):
        try:
            return self._export(vid, folder)
        finally:
            try:
                self.page.call('Browser.setDownloadBehavior', {'behavior': 'default'})
            except Exception:
                pass

    def _export(self, vid, folder):
        private_dir(folder)
        with tempfile.TemporaryDirectory(prefix='export-', dir=folder) as download:
            self.page.call('Browser.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': download})
            self.page.evaluate("document.querySelectorAll('*').forEach(e => { if (e.scrollTop) e.scrollTop = 0; }); true")
            deadline = time.monotonic() + 45
            while not self.page.evaluate(MENU_JS % json.dumps(vid)):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Selected book could not be found in the loaded library.')
                self.page.evaluate(SCROLL_JS)
                time.sleep(.4)
            self.wait_click(EXPORT_JS)
            self.wait_click(EPUB_JS)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                files = [p for p in Path(download).iterdir() if p.is_file() and not p.name.endswith(('.crdownload', '.tmp'))]
                if len(files) == 1:
                    source = files[0]
                    suffix = exported_format(source)
                    target = folder / ('source' + suffix)
                    if os.name != 'nt':
                        source.chmod(0o600)
                    source.replace(target)
                    return target
                time.sleep(.3)
        raise RuntimeError('EPUB export did not finish. Select a local ACSM/EPUB or retry later.')


def exported_format(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as book:
            if book.read('mimetype') == b'application/epub+zip':
                return '.epub'
    elif path.stat().st_size <= 2 * 1024 * 1024:
        try:
            root = ET.parse(path).getroot()
            if root.tag == '{http://ns.adobe.com/adept}fulfillmentToken':
                return '.acsm'
        except (ET.ParseError, OSError):
            pass
    raise RuntimeError('Export did not return an EPUB or ACSM file; no book was added.')


def refresh_library(profile, state, browser=None, headless=True, *, require_books=False):
    state = Path(state).expanduser().resolve()
    client = LibraryBrowser(profile, browser, headless)
    try:
        client.open()
        print('Reading owned Google library books...', flush=True)
        owned = client.scan(LIBRARY_URL, allow_empty=True)
        if require_books and not owned:
            raise EmptyLibraryError('No books found in the live library.')
        books = group_by_title(owned.values())
        catalog = {'profile': str(Path(profile).expanduser().resolve()),
                   'books': books, 'grouping': 'title', 'updated_at': time.time()}
        write_private_json(state / 'library' / 'catalog.json', catalog)
        save_session(state, profile, True)
        print(CATALOG_EVENT, flush=True)
        print(f'Library loaded: {len(books)} books, {len({book["series_id"] for book in books})} title groups.', flush=True)
        return catalog
    except (SignedOutError, EmptyLibraryError):
        save_session(state, profile, False)
        raise
    finally:
        client.close(True)


def logout_google(profile, state, browser=None):
    client = LibraryBrowser(profile, browser, headless=True)
    try:
        client.open()
        client.page.call('Network.clearBrowserCookies')
        client.page.call('Page.navigate', {'url': LIBRARY_URL})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snapshot = client.page.evaluate(SNAPSHOT_JS) or {}
            if snapshot.get('signed_out'):
                save_session(state, profile, False)
                print('Signed out of the dedicated browser. ADE authorization and ebook files were not changed.', flush=True)
                return
            time.sleep(.5)
        raise RuntimeError('Browser cookies cleared, but sign-out could not be verified. Refresh the library or clear local Google credentials.')
    finally:
        client.close(True)


def export_books(ids, profile, state, result_file, browser=None, headless=True):
    if not ids or any(not ID.fullmatch(vid) for vid in ids):
        raise ValueError('Invalid library volume ID')
    client = LibraryBrowser(profile, browser, headless)
    results = []
    try:
        client.open()
        owned = client.scan(LIBRARY_URL)
        for index, vid in enumerate(dict.fromkeys(ids)):
            book = owned.get(vid)
            result = {'id': vid, 'title': book['title'] if book else vid, 'path': ''}
            try:
                if not book:
                    raise RuntimeError('This book is not in the current signed-in library.')
                if index:
                    # Reset any dialog left by the previous export.
                    client.scan(LIBRARY_URL)
                print(f'Exporting EPUB source: {vid}', flush=True)
                result['path'] = str(client.export(vid, state / 'library' / 'exports' / vid))
                result['language'] = source_language(result['path'])
                print(f'Export complete: {vid}', flush=True)
            except (RuntimeError, OSError, ValueError, KeyError):
                result['error'] = 'Official EPUB export unavailable; select a local ACSM/EPUB.'
                print(f'Export unavailable: {vid}; select a local ACSM/EPUB.', flush=True)
            results.append(result)
            write_private_json(result_file, {'books': results})
        return results
    finally:
        client.close(True)
