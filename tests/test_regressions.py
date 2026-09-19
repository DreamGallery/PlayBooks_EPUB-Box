"""Offline regression tests: no accounts, network or user caches required."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import string
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
from playbooks import adobe_backend as adobe, playbooks_hires as hires, runtime
from playbooks.cache_cleanup import inspect_entry
from playbooks.output_naming import title_filename
from playbooks.volume_lookup import author_set


class Regressions(unittest.TestCase):
    def test_vendor_load_without_calibre(self):
        with tempfile.TemporaryDirectory() as folder:
            code = (f'import sys; sys.path.insert(0, {str(SRC)!r}); '
                    'from pathlib import Path; from playbooks.adobe_backend import load_modules; '
                    f'modules = load_modules(Path({folder!r})); '
                    'assert modules[-1].AES is not None and modules[-1].RSA is not None; '
                    'assert not any(n == "calibre" or n.startswith("calibre.") for n in sys.modules)')
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lock_contention_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            code = (f'import sys; sys.path.insert(0, {str(SRC)!r}); '
                    'from pathlib import Path; from playbooks.adobe_backend import state_lock\n'
                    f'with state_lock(Path({folder!r})): print("acquired")')
            with adobe.state_lock(state):
                result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'Another task', result.stderr)
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_lock_byte_and_unlock(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            calls = []
            def locking(fd, mode, size):
                calls.append((mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
            msvcrt = SimpleNamespace(locking=locking, LK_NBLCK=2, LK_UNLCK=0)
            with patch.object(adobe, 'os', SimpleNamespace(name='nt', SEEK_END=os.SEEK_END)), patch.dict(sys.modules, msvcrt=msvcrt):
                with self.assertRaisesRegex(ValueError, 'test'):
                    with adobe.state_lock(state):
                        raise ValueError('test')
            self.assertEqual(calls, [(2, 1, 0), (0, 1, 0)])

    def test_hidden_windows_child_and_utf8(self):
        with patch.object(runtime, 'os', SimpleNamespace(name='nt')), patch.object(runtime.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True):
            self.assertEqual(runtime.hidden_process_options(), {'creationflags': 0x08000000})
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, '-c', 'print("日本語 · 繁體中文")'],
                                    env=runtime.child_environment(Path(folder) / 'cancel'),
                                    capture_output=True, timeout=10)
            self.assertEqual(result.stdout.decode('utf-8').strip(), '日本語 · 繁體中文')

    def test_windows_cli_redirect_encoding(self):
        output = Mock()
        with patch.object(runtime, 'os', SimpleNamespace(name='nt', environ={})), patch.object(runtime, 'sys', SimpleNamespace(stdout=output, stderr=None)):
            self.assertEqual(runtime.cancellable(lambda: 0)(), 0)
        output.reconfigure.assert_called_once_with(encoding='utf-8', errors='replace')

    def test_cooperative_cancel_runs_finally(self):
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / 'cancel'
            flag.touch()
            code = (f'import sys, time; sys.path.insert(0, {str(SRC)!r})\n'
                    'from playbooks.runtime import cancellable\n'
                    '@cancellable\ndef main():\n'
                    ' try:\n  while True: time.sleep(.01)\n'
                    ' except KeyboardInterrupt: return 130\n'
                    ' finally: print("cleaned")\n'
                    'raise SystemExit(main())')
            result = subprocess.run([sys.executable, '-c', code], env=runtime.child_environment(flag), capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 130, result.stderr)
            self.assertIn(b'cleaned', result.stdout)

    def test_invalid_volume_does_not_create_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'should-not-exist'
            with self.assertRaises(ValueError):
                hires.do_fetch('../bad', target, 'browser', target, None, None, None, None, False, 10, 0, 0, True)
            self.assertFalse(target.exists())

    def test_windows_names_and_author_delimiters(self):
        self.assertEqual(hires.safe_name('CON.txt'), '_CON.txt')
        self.assertEqual(title_filename('NUL'), '_NUL.epub')
        self.assertEqual(title_filename('日本語:本?'), '日本語_本_.epub')
        self.assertEqual(author_set('甲，乙、丙'), author_set(['甲', '乙', '丙']))

    def test_log_tokens_hidden(self):
        text = runtime.safe_log_text('https://example.test/?id=book&sig=secret&token=other')
        self.assertNotIn('secret', text)
        self.assertNotIn('other', text)
        self.assertIn('id=book', text)
        self.assertEqual(runtime.safe_log_text('Title: 日本語の本'), 'Title: 日本語の本')

    def test_publish_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            src, dst = base / 'src.epub', base / 'out.epub'
            dst.write_bytes(b'existing-output')
            with zipfile.ZipFile(src, 'w') as z:
                z.writestr('mimetype', b'application/epub+zip')
            with patch.object(hires, 'verify_epub', return_value=['bad EPUB']):
                with self.assertRaises(RuntimeError):
                    hires.write_epub(src, dst, {}, {})
            self.assertEqual(dst.read_bytes(), b'existing-output')
            self.assertEqual(list(base.glob('*.tmp')), [])

    def test_real_image_replacement(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            im = Image.new('RGB', (256, 256), 'white')
            draw = ImageDraw.Draw(im)
            draw.rectangle((15, 20, 100, 200), fill='red')
            draw.ellipse((65, 60, 220, 240), fill='blue')
            cache = base / 'images'
            cache.mkdir()
            im.save(cache / 'i-001.png')
            low = io.BytesIO()
            im.resize((128, 128)).save(low, format='PNG')
            source, out = base / '書籍.epub', base / 'output.epub'
            opf = '<package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>日本語の本</dc:title></metadata><manifest><item id="i-001" href="i-001.png" media-type="image/png"/><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>'
            with zipfile.ZipFile(source, 'w') as z:
                z.writestr('mimetype', 'application/epub+zip')
                z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
                z.writestr('content.opf', opf)
                z.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="i-001.png"/></body></html>')
                z.writestr('i-001.png', low.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hires.do_replace(source, cache, out, 1.1, .2, False, None), 1)
            self.assertEqual(hires.verify_epub(out, {'i-001.png': (256, 256)}), [])
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.read('content.opf').decode(), opf)
                self.assertEqual(z.read('i-001.png'), (cache / 'i-001.png').read_bytes())

    def test_cache_links_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'abcdefghijkl'
            cache.mkdir()
            (cache / 'images.json').write_text(json.dumps({'volume_id': cache.name, 'images': []}), encoding='utf-8')
            self.assertIsNotNone(inspect_entry(cache.resolve()))
            images = cache / 'images'
            try:
                images.symlink_to(Path(folder), target_is_directory=True)
            except OSError:
                self.skipTest('Symlink creation requires Windows developer mode or permission')
            self.assertIsNone(inspect_entry(cache.resolve()))

    def test_gui_locales_and_task_lifecycle(self):
        import flet as ft
        from playbooks.playbooks_flet import Workbench
        from playbooks.ui_i18n import CATALOG, LANGUAGES
        class Page:
            def __init__(self):
                self.window = ft.Window()
                self.services = []
                self.platform_brightness = ft.Brightness.LIGHT
            def add(self, *args): pass
            def update(self): pass
        def fields(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}
        for key, translations in CATALOG.items():
            self.assertEqual(set(translations), {'zh_TW', 'en', 'ja'})
            for value in translations.values():
                self.assertEqual(fields(key), fields(value))
        with tempfile.TemporaryDirectory() as folder, patch.object(Workbench, 'load_preferences', return_value={}):
            w = Workbench(Page())
            w.settings_file = Path(folder) / 'preferences.json'
            for language in LANGUAGES:
                w.language_select.value = language
                w.change_language(None)
            w.language_select.value = 'en'
            w.change_language(None)
            async def tasks():
                await w.run_jobs([(None, [sys.executable, '-c', 'print("Title: 日本語")'], None)])
                self.assertEqual(w.logs[0].controls[-1].value, 'Title: 日本語')
                self.assertFalse(w.logs[1].controls)
                await w.run_jobs([(None, [sys.executable, '-c', 'raise SystemExit(1)'], None)], log_page=1)
                self.assertIn('failed', w.status.value)
                code = (f'import sys, time; sys.path.insert(0, {str(SRC)!r})\n'
                        'from playbooks.runtime import cancellable\n'
                        '@cancellable\ndef main():\n'
                        ' try:\n  while True: time.sleep(.01)\n'
                        ' except KeyboardInterrupt: return 130\n'
                        ' finally: print("cleaned")\n'
                        'raise SystemExit(main())')
                task = asyncio.create_task(w.run_jobs([(None, [sys.executable, '-c', code], None)]))
                try:
                    await asyncio.sleep(.4)
                    await w.cancel()
                    await asyncio.wait_for(task, 10)
                finally:
                    if w.proc and w.proc.returncode is None:
                        w.proc.kill()
                self.assertFalse(w.running)
                self.assertIn('Cancelled', w.status.value)
                self.assertIsNone(w.cancel_file)
            asyncio.run(tasks())


if __name__ == '__main__':
    unittest.main()
