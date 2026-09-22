#!/usr/bin/env python3
"""Standalone ACSM → EPUB → original-image pipeline, also used by the GUI."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import zipfile

from . import adobe_backend as adobe
from . import playbooks_hires as hires
from .output_naming import output_path

from .paths import ROOT
from .runtime import cancellable, safe_log_text
DEFAULT_STATE = ROOT / '.playbooks-state'


def fingerprint(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_epub(source: Path):
    with zipfile.ZipFile(source) as z:
        if z.testzip() is not None:
            raise RuntimeError('EPUB ZIP validation failed')
        if z.read('mimetype') != b'application/epub+zip':
            raise RuntimeError('Input is not a valid EPUB')
    return hires.load_epub(source, hashes=False)


def process(args) -> Path:
    # Serialize jobs sharing this state, including publication and image caches.
    adobe.private_dir(args.state.expanduser().resolve())
    with adobe.state_lock(args.state.expanduser().resolve() / 'pipeline'):
        return _process(args)


def _process(args) -> Path:
    source = args.input.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in ('.acsm', '.epub'):
        raise RuntimeError('Select an existing .acsm or .epub file')
    def check_output(output):
        if output == source:
            raise RuntimeError('Output must not overwrite the input file')
        if output.exists() and not args.overwrite:
            raise RuntimeError('Output already exists: ' + str(output) + '; choose another path or explicitly use --overwrite')
    if args.out:
        check_output(args.out.expanduser().resolve())
    state = args.state.expanduser().resolve()
    cache = state / 'books' / fingerprint(source)
    adobe.private_dir(cache)
    print('[1/4] Preparing EPUB', flush=True)
    if source.suffix.lower() == '.acsm':
        print('Preparing ACSM download (cache preferred)', flush=True)
        prepared = adobe.fulfill_acsm(source, state, cache)
    else:
        prepared = source
    print('[2/4] Checking/decrypting Adobe EPUB', flush=True)
    prepared = adobe.decrypt_epub(prepared, cache / 'readable.epub', state, args.key)
    info = validate_epub(prepared)
    vid = args.id or info.volume_id
    output = output_path(source, info.title, explicit=args.out, directory=args.output_dir)
    if not args.prepare_only:
        if args.images:
            images = args.images.resolve()
            print('[3/4] Using local original images', flush=True)
        else:
            if not vid:
                from .volume_lookup import resolve_volume_id
                vid = resolve_volume_id(info.opf_xml, args.work.resolve())
            print('[3/4] Downloading Google Play originals', flush=True)
            mode = 'cookies' if args.cookies else 'browser'
            images = hires.do_fetch(vid, args.work.resolve() / vid, mode,
                                    args.profile.resolve(), args.cdp, args.browser, args.cookies,
                                    args.authuser, False, 900, args.pace, 0,
                                    not args.keep_browser, True, not args.show_browser)
        output = output_path(source, info.title, images, args.out, args.output_dir)
        check_output(output)
        print('Output file: ' + str(output), flush=True)
        print('[4/4] Matching, replacing and validating', flush=True)
        count = hires.do_replace(prepared, images, output, args.min_gain,
                                 args.max_dist, args.dry_run, args.report, False)
        if args.dry_run:
            print('Dry run complete; no EPUB written (preparation/download caches may have been created)', flush=True)
            return output
        if count:
            print('Done: ' + str(output), flush=True)
            return output
        print('No images need upgrading; exporting the prepared EPUB', flush=True)
    elif args.dry_run:
        check_output(output)
        print('Dry run complete; no EPUB written', flush=True)
        return output
    check_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + '.part')
    try:
        shutil.copyfile(prepared, temp)
        validate_epub(temp)
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
    print('Done: ' + str(output), flush=True)
    return output


def make_parser():
    parser = argparse.ArgumentParser(description='独立的 ACSM / EPUB 高清插图工作台（不依赖 ADE 或 Calibre）')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('gui', help='打开桌面界面')
    sub.add_parser('gui-flet', help='gui 的兼容别名')
    login = sub.add_parser('login-google', help='打开普通浏览器手动登录 Google，关闭窗口后再处理书籍')
    login.add_argument('--profile', type=Path, default=ROOT / '.chrome-profile')
    login.add_argument('--browser', help='Chrome / Edge 可执行文件路径')
    status = sub.add_parser('status', help='检查依赖和授权状态，不联网')
    auth = sub.add_parser('authorize', help='在本机输入 Adobe ID 并授权；会注册一个设备')
    auth.add_argument('--stdin-json', action='store_true', help=argparse.SUPPRESS)
    imp = sub.add_parser('import-auth', help='导入用户指定的 ACSM 插件授权目录')
    imp.add_argument('--folder', type=Path, required=True)
    job = sub.add_parser('process', help='ACSM/EPUB → 可读 EPUB → 高清原图替换')
    for p in (status, auth, imp, job):
        p.add_argument('--state', type=Path, default=DEFAULT_STATE)
    job.add_argument('--input', type=Path, required=True)
    job.add_argument('--id', help='Google Play 卷 ID（可省略：从 EPUB、本地记录或公开目录自动识别；有歧义则停止）')
    job.add_argument('--key', type=Path, help='已有的 Adobe DER 密钥文件，不会复制或打印密钥')
    destination = job.add_mutually_exclusive_group()
    destination.add_argument('--out', type=Path, help='显式指定输出文件名（优先于自动书名）')
    destination.add_argument('--output-dir', type=Path, help='输出目录；默认 输入目录/hires，文件名使用书名')
    job.add_argument('--images', type=Path, help='直接使用已下载原图，跳过联网抓图')
    job.add_argument('--work', type=Path, default=ROOT / 'playbooks_work')
    job.add_argument('--profile', type=Path, default=ROOT / '.chrome-profile')
    job.add_argument('--cdp')
    job.add_argument('--browser')
    job.add_argument('--cookies', type=Path)
    job.add_argument('--authuser')
    job.add_argument('--show-browser', action='store_true')
    job.add_argument('--keep-browser', action='store_true')
    job.add_argument('--pace', type=float, default=.25)
    job.add_argument('--min-gain', type=float, default=1.1)
    job.add_argument('--max-dist', type=float, default=.2)
    job.add_argument('--report', type=Path)
    job.add_argument('--prepare-only', action='store_true', help='只兑换/解密，不抓图、不替换')
    job.add_argument('--dry-run', action='store_true')
    job.add_argument('--overwrite', action='store_true', help='允许替换已存在的输出 EPUB（不允许覆盖输入）')
    return parser


@cancellable
def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        if args.command in ('gui', 'gui-flet'):
            from .playbooks_flet import main as gui
            gui()
        elif args.command == 'login-google':
            from .browser_login import login_google
            login_google(args.profile, args.browser)
        elif args.command == 'status':
            import importlib.util
            for module in ('PIL', 'requests', 'cryptography', 'websocket', 'lxml', 'Crypto', 'flet'):
                print(module + ': ' + ('OK' if importlib.util.find_spec(module) else 'Not installed'))
            print('Adobe: ' + ('Authorized' if adobe.authorized(args.state) else 'Not authorized'))
            print('Chrome: ' + (hires.find_browser() or 'Not found'))
        elif args.command == 'authorize':
            if args.stdin_json:
                secret = json.loads(sys.stdin.readline())
                email, password = secret['email'], secret['password']
                secret.clear()
            else:
                print('This registers an ADE device. Use import-auth for existing authorization. Passwords are not saved.')
                email = input('ByteBooks ID: ')
                password = getpass.getpass('Password: ')
            adobe.authorize(args.state.resolve(), email, password)
            password = ''
            print('ADE authorization complete; password not saved')
        elif args.command == 'import-auth':
            adobe.import_activation(args.state.resolve(), args.folder.resolve())
            print('Authorization imported')
        else:
            if (not all(math.isfinite(v) for v in (args.pace, args.min_gain, args.max_dist))
                    or args.pace < 0 or args.min_gain <= 0 or not 0 <= args.max_dist <= .5):
                raise RuntimeError('Invalid ranges: pace >= 0, min-gain > 0, max-dist between 0 and 0.5')
            process(args)
        return 0
    except KeyboardInterrupt:
        print('Task cancelled; completed downloads retained', file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError, zipfile.BadZipFile, ImportError) as exc:
        print('Failed: ' + safe_log_text(exc), file=sys.stderr)
        return 1
    except Exception:
        # Third-party exceptions can contain tokens / full XML responses.
        print('Failed: third-party module error. The raw response is hidden to protect authorization data. Check dependencies, authorization and input files.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
