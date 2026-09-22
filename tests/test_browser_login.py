"""Sign-in lifecycle checks without opening a browser or accessing an account."""
import asyncio
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from playbooks import browser_login as login, playbooks_hires as hires, playbooks_app as cli


class BrowserLoginTests(unittest.TestCase):
    def test_plain_launch_uses_same_profile_without_debugging(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as output:
            profile = Path(folder) / 'separate profile'
            proc = Mock()
            proc.wait.side_effect = [subprocess.TimeoutExpired('chrome', .25), 0]
            with patch.object(hires, 'find_browser', return_value='/fake/chrome'), \
                    patch.object(login.subprocess, 'Popen', return_value=proc) as launch, \
                    patch.object(login.time, 'monotonic', side_effect=[0, 2]):
                login.login_google(profile)
            args = launch.call_args.args[0]
            self.assertIn(f'--user-data-dir={profile.resolve()}', args)
            self.assertEqual(args[-1], 'https://play.google.com/books')
            self.assertFalse(any(word in arg for arg in args for word in
                                 ('remote-debugging', 'headless', 'disable-web-security', 'disable-blink', 'enable-automation')))
            self.assertEqual(launch.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertIn('Sign-in was not verified', output.getvalue())
            proc.terminate.assert_not_called()

    def test_daily_browser_profiles_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, LOCALAPPDATA=folder):
            for profile in (Path.home(), Path(folder) / 'Google/Chrome/User Data',
                            Path(folder) / 'Google/Chrome/User Data/Default',
                            Path.home() / 'Library/Application Support/Google/Chrome'):
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    login.dedicated_profile(profile)

    def test_live_debug_browser_blocks_login(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            (profile / 'DevToolsActivePort').write_text('9222\n/devtools/browser/example', encoding='utf-8')
            with patch.object(hires, 'find_browser', return_value='/fake/chrome'), \
                    patch.object(hires, '_http_json', return_value={'Browser': 'Chrome'}), \
                    patch.object(login.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, 'debugging browser'):
                    login.login_google(profile)
                launch.assert_not_called()

    def test_stale_debug_discovery_does_not_block_plain_login(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            profile = Path(folder)
            (profile / 'DevToolsActivePort').write_text('9222\n', encoding='utf-8')
            with patch.object(hires, 'find_browser', return_value='/fake/chrome'), \
                    patch.object(hires, '_http_json', side_effect=OSError('closed')), \
                    patch.object(login.subprocess, 'Popen', return_value=Mock(wait=Mock(return_value=0))), \
                    patch.object(login.time, 'monotonic', side_effect=[0, 2]):
                login.login_google(profile)

    def test_login_profile_lock_blocks_downloader_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            browser = hires.BrowserAuth('abcdefghijkl', profile)
            with login.profile_session(profile), patch.object(browser, '_open') as start:
                with self.assertRaisesRegex(RuntimeError, 'Another PlayBooks task'):
                    browser.open()
                start.assert_not_called()
            with patch.object(browser, '_open', side_effect=RuntimeError('startup failed')):
                with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                    browser.open()
            with login.profile_session(profile):
                pass

    def test_cancel_leaves_manual_browser_open(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as output:
            proc = Mock(wait=Mock(side_effect=KeyboardInterrupt))
            with patch.object(hires, 'find_browser', return_value='/fake/chrome'), \
                    patch.object(login.subprocess, 'Popen', return_value=proc):
                with self.assertRaises(KeyboardInterrupt):
                    login.login_google(Path(folder))
            self.assertIn('left open', output.getvalue())
            proc.terminate.assert_not_called()
            proc.kill.assert_not_called()
            with login.profile_session(Path(folder)):
                pass

    def test_early_exit_not_reported_as_login_success(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(hires, 'find_browser', return_value='/fake/chrome'), \
                    patch.object(login.subprocess, 'Popen', return_value=Mock(wait=Mock(return_value=0))), \
                    patch.object(login.time, 'monotonic', side_effect=[0, .1]):
                with self.assertRaisesRegex(RuntimeError, 'exited early'):
                    login.login_google(Path(folder))

    def test_cli_dispatch(self):
        with patch.object(login, 'login_google') as run:
            self.assertEqual(cli.main(['login-google', '--profile', 'private profile', '--browser', 'chrome.exe']), 0)
            run.assert_called_once_with(Path('private profile'), 'chrome.exe')

    def test_gui_login_needs_no_book_and_routes_logs_to_processing(self):
        import flet as ft
        from playbooks.playbooks_flet import Workbench
        class Page:
            def __init__(self):
                self.window = ft.Window()
                self.services = []
                self.platform_brightness = ft.Brightness.LIGHT
            def add(self, *args): pass
            def update(self): pass
        with patch.object(Workbench, 'load_preferences', return_value={}):
            w = Workbench(Page())
        w.options['profile'].value = 'custom profile'
        w.run_jobs = AsyncMock(return_value=True)
        asyncio.run(w.login_google(None))
        command = w.run_jobs.call_args.args[0][0][1]
        self.assertEqual(command[-3:], ['login-google', '--profile', 'custom profile'])
        self.assertEqual(w.run_jobs.call_args.kwargs, {'log_page': 0})
        w.running = True
        asyncio.run(w.login_google(None))
        self.assertEqual(w.run_jobs.await_count, 1)


if __name__ == '__main__':
    unittest.main()
