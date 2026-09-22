"""Flet desktop interface; book processing runs through the shared CLI."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import tempfile

import flet as ft
from .gui_commands import ROOT, MODES, build_command
from .ui_i18n import LANGUAGES, translate, refresh_ui
from .runtime import cli_python, child_environment, hidden_process_options
from .paths import SOURCE_DIR

# Semantic accents follow the active palette, including newly added queue rows.
ACCENT = ft.Colors.PRIMARY
APP_NAME = 'Play Books EPUB Box'
APP_VERSION = '0.1.1'
PROJECT_URL = 'https://github.com/DreamGallery/PlayBooks_EPUB-Box'


class Workbench:
    def __init__(self, page):
        self.page = page
        self.running = False
        self.cancelled = False
        self.proc = None
        self.cancel_file = None
        self.jobs = []
        self.cards = []
        self.nav_buttons = []
        self.nav_index = 0
        self.settings_file = ROOT / '.flet-settings.json'
        self.language = self.load_preferences().get('language', 'zh_CN')
        if self.language not in LANGUAGES:
            self.language = 'zh_CN'
        self.apply_locale()
        page.title = APP_NAME
        page.padding = 0
        page.window.width, page.window.height = 1160, 900
        page.window.min_width, page.window.min_height = 960, 760
        page.window.prevent_close = True
        page.window.on_event = self.window_event
        page.theme = ft.Theme(color_scheme_seed='#8A6B37', use_material3=True,
            color_scheme=ft.ColorScheme(
                primary='#8A6B37', on_primary='#FFFFFF',
                primary_container='#EEDFC4', on_primary_container='#49391F',
                secondary='#75664E', on_secondary='#FFFFFF',
                secondary_container='#EAE1D1', on_secondary_container='#423A2D',
                tertiary='#7B7057', on_tertiary='#FFFFFF',
                tertiary_container='#EAE2CD', on_tertiary_container='#433C2C',
                surface='#FFFCF6', on_surface='#34312B', on_surface_variant='#70695E',
                surface_dim='#E5E0D7', surface_bright='#FFFCF6',
                surface_container_lowest='#FFFFFF', surface_container_low='#FAF7F0',
                surface_container='#F3EEE4', surface_container_high='#ECE6DB',
                surface_container_highest='#E5DED2', surface_tint='#8A6B37',
                outline='#8B8274', outline_variant='#D5CCBD',
                inverse_surface='#34312B', on_inverse_surface='#F6F0E5',
                inverse_primary='#D8BE8D'))
        page.dark_theme = ft.Theme(color_scheme_seed='#C8AC7C', use_material3=True,
            color_scheme=ft.ColorScheme(
                primary='#C8AC7C', on_primary='#292318',
                primary_container='#443B2D', on_primary_container='#EDDCBC',
                secondary='#BDB09A', on_secondary='#29251F',
                secondary_container='#39352E', on_secondary_container='#E6DDCE',
                tertiary='#C4B49B', on_tertiary='#2B251C',
                tertiary_container='#40382E', on_tertiary_container='#EADBC5',
                surface='#20201E', on_surface='#EAE6DF', on_surface_variant='#B8B2A8',
                surface_dim='#1B1B19', surface_bright='#393833',
                surface_container_lowest='#191917', surface_container_low='#242421',
                surface_container='#2C2B28', surface_container_high='#34332F',
                surface_container_highest='#3D3B36', surface_tint='#C8AC7C',
                outline='#827B70', outline_variant='#49463F',
                inverse_surface='#EAE6DF', on_inverse_surface='#30302C',
                inverse_primary='#705B35'))
        self.picker = ft.FilePicker()
        page.services.append(self.picker)
        self.status = ft.Text(self.tr('就绪'), size=12, text_align=ft.TextAlign.RIGHT)
        self.progress = ft.ProgressBar(value=0, height=3, color=ACCENT)
        self.logs = [ft.ListView(height=130, spacing=3, auto_scroll=True),
                     ft.ListView(height=130, spacing=3, auto_scroll=True, visible=False)]
        self.count = ft.Text(self.tr('0 本待处理'), size=12)
        self.queue = ft.Column(spacing=8)
        self.options = {
            'mode': ft.Dropdown(label=self.tr('处理方式'), value=MODES[0], options=[ft.DropdownOption(m, self.tr(m)) for m in MODES], expand=True),
        }
        for key, label, value in [
            ('output', self.tr('输出目录 · 留空使用源文件旁的 hires/'), ''),
            ('images', self.tr('本地原图目录 · 可选'), ''),
            ('profile', self.tr('Chrome 配置目录'), str(ROOT / '.chrome-profile')),
            ('work', self.tr('抓图缓存目录'), str(ROOT / 'playbooks_work')),
            ('state', self.tr('ADE 授权与准备缓存'), str(ROOT / '.playbooks-state')),
            ('gain', self.tr('最小像素增益'), '1.1'), ('distance', self.tr('匹配距离'), '0.2'),
            ('pace', self.tr('请求间隔 / 秒'), '0.25'),
        ]:
            self.options[key] = ft.TextField(label=label, value=value, text_size=13, expand=True)
        for key, label in [('show', self.tr('显示抓图浏览器')), ('keep', self.tr('处理后保留 Chrome')),
                           ('dry_run', self.tr('仅预览替换')), ('overwrite', self.tr('允许覆盖已有输出'))]:
            self.options[key] = ft.Checkbox(label=label, value=False)
        self.email = ft.TextField(label=self.tr('ByteBooks ID（邮箱）'), text_size=13, expand=True)
        self.password = ft.TextField(label=self.tr('ByteBooks 密码'), password=True, can_reveal_password=True, text_size=13, expand=True)
        self.start_button = ft.FilledButton(self.tr('开始处理'), icon=ft.Icons.PLAY_ARROW_ROUNDED, on_click=self.start)
        self.stop_button = ft.TextButton(self.tr('取消任务'), disabled=True, on_click=self.cancel)
        self.theme_select = ft.Dropdown(value=self.load_theme(), width=176, text_size=12,
            options=[ft.DropdownOption('system', self.tr('跟随系统')), ft.DropdownOption('light', self.tr('浅色')), ft.DropdownOption('dark', self.tr('深色'))],
            on_select=self.change_theme)
        self.language_select = ft.Dropdown(value=self.language, width=176, text_size=12,
            options=[ft.DropdownOption(code, name) for code, name in LANGUAGES.items()], on_select=self.change_language)
        self.workspace = self.make_workspace()
        self.account = self.make_account()
        self.about = self.make_about()
        self.body = ft.Column([self.workspace, self.account, self.about], spacing=0)
        self.account.visible = self.about.visible = False
        for i, (label, icon) in enumerate([(self.tr('文件与处理'), ft.Icons.AUTO_STORIES_OUTLINED),
                                          (self.tr('ADE 授权'), ft.Icons.VERIFIED_USER_OUTLINED),
                                          (self.tr('关于'), ft.Icons.INFO_OUTLINE_ROUNDED)]):
            self.nav_buttons.append(ft.Container(content=ft.Row([ft.Icon(icon, size=20), ft.Text(label, size=13, expand=True)]),
                padding=14, border_radius=14, on_click=lambda e, n=i: self.navigate(n)))
        self.logo_svg = (SOURCE_DIR / 'assets' / 'google-play-books.svg').read_text(encoding='utf-8')
        self.logo = ft.Image(src=self.logo_svg.encode('utf-8'), width=30, height=34,
                             fit=ft.BoxFit.CONTAIN, semantics_label='Google Play Books')
        self.sidebar = self.card(ft.Column([
            ft.Row([self.logo,
                    ft.Text('Play Books', size=22, weight=ft.FontWeight.BOLD)]),
            ft.Container(height=12),
            *self.nav_buttons[:2], ft.Container(expand=True),
            ft.Divider(height=1), self.nav_buttons[2], ft.Divider(height=1),
            ft.Text(self.tr('外观'), size=11), self.theme_select,
            ft.Text(self.tr('语言'), size=11), self.language_select,
        ], spacing=10), width=236)
        self.log_card = self.card(ft.Column([
            ft.Row([ft.Text(self.tr('运行日志'), size=12, weight=ft.FontWeight.BOLD), ft.Container(expand=True),
                    ft.TextButton(self.tr('清除'), icon=ft.Icons.DELETE_SWEEP_OUTLINED, on_click=self.clear_log)]), *self.logs,
        ], spacing=5))
        # One content scroll region: no expanding spacer between form and log.
        self.content_scroll = ft.Column([self.body, self.log_card], spacing=16,
            expand=True, scroll=ft.ScrollMode.AUTO)
        self.footer = self.card(ft.Column([
            ft.Row([self.start_button, self.stop_button,
                    ft.Container(self.status, expand=True, alignment=ft.Alignment.CENTER_RIGHT)], spacing=8),
            self.progress,
        ], spacing=10))
        self.background = ft.Container(expand=True, padding=22, content=ft.Row([
            self.sidebar, ft.Column([
                ft.Text('EPUB Box', size=24, weight=ft.FontWeight.BOLD),
                self.content_scroll, self.footer,
            ], expand=True, spacing=18),
        ], spacing=20, vertical_alignment=ft.CrossAxisAlignment.STRETCH))
        self.apply_theme()
        page.on_platform_brightness_change = self.system_theme
        page.add(self.background)
        self.render_queue()

    def card(self, content, **kwargs):
        result = ft.Container(content=content, padding=20, border_radius=22,
            blur=ft.Blur(18, 18), **kwargs)
        self.cards.append(result)
        return result

    def folder_field(self, key):
        async def choose(e):
            folder = await self.picker.get_directory_path()
            if folder:
                self.options[key].value = folder
                self.page.update()
        return ft.Row([self.options[key], ft.IconButton(ft.Icons.FOLDER_OPEN_ROUNDED, tooltip=self.tr('选择目录'), on_click=choose)])

    def make_workspace(self):
        return ft.Column([
            self.card(ft.Column([
                ft.Row([ft.Text(self.tr('文件队列'), size=16, weight=ft.FontWeight.BOLD), ft.Container(expand=True), self.count,
                    ft.FilledTonalButton(self.tr('添加电子书'), icon=ft.Icons.ADD_ROUNDED, on_click=self.add_files)]),
                self.queue,
            ], spacing=14)),
            self.card(ft.Column([
                ft.Row([ft.Text(self.tr('输出与处理'), size=16, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.TextButton(self.tr('登录 Google'), icon=ft.Icons.LOGIN_ROUNDED, on_click=self.login_google)]),
                ft.Text(self.tr('首次使用请先登录 Google，关闭登录浏览器后再开始处理。'), size=12),
                ft.Row([self.options['mode']]), self.folder_field('output'),
                self.make_settings(),
            ], spacing=14)),
        ], spacing=16)

    def make_settings(self):
        return ft.ExpansionTile(
            title=ft.Text(self.tr('处理设置'), size=14, weight=ft.FontWeight.BOLD),
            leading=ft.Icon(ft.Icons.TUNE_ROUNDED),
            expanded=False, maintain_state=True,
            tile_padding=0, controls_padding=ft.Padding.only(top=12),
            controls=[ft.Column([
            *[self.folder_field(k) for k in ('images', 'profile', 'work')],
            ft.TextButton(self.tr('清除本地图源缓存'), icon=ft.Icons.DELETE_SWEEP_OUTLINED,
                on_click=self.confirm_clear_cache),
            ft.Row([self.options[k] for k in ('gain', 'distance', 'pace')]),
            ft.Row([self.options[k] for k in ('show', 'keep', 'dry_run', 'overwrite')], wrap=True),
        ], spacing=16)])

    def make_account(self):
        return self.card(ft.Column([
            ft.Text(self.tr('ADE 授权'), size=20, weight=ft.FontWeight.BOLD),
            ft.Text(self.tr('已有授权可直接导入；Google 登录在处理设置中管理。'), size=12),
            self.folder_field('state'),
            ft.Row([ft.OutlinedButton(self.tr('检查授权与依赖'), on_click=self.check_status),
                    ft.TextButton(self.tr('导入已有授权'), on_click=self.import_auth)]),
            ft.Divider(),
            ft.Row([self.email, ft.IconButton(ft.Icons.ERROR_OUTLINE_ROUNDED,
                tooltip=self.tr('ByteBooks ID 说明与迁移帮助'), icon_color=ACCENT,
                on_click=self.show_account_help)]),
            ft.Row([self.password]),
            ft.Text(self.tr('登录会注册设备。密码不保存，授权文件请勿分享。'), size=12),
            ft.FilledTonalButton(self.tr('登录并授权'), on_click=self.confirm_authorize),
        ], spacing=16))

    async def confirm_clear_cache(self, e=None):
        if self.running:
            self.note(self.tr('请等待当前任务结束后再清除缓存。'))
            return
        from .cache_cleanup import scan_cache, trash_cache
        protected = [str(ROOT / '.chrome-profile'), str(ROOT / '.playbooks-state'),
            self.options['profile'].value, self.options['state'].value,
            self.options['output'].value, *[job['path'] for job in self.jobs]]
        try:
            root, entries, skipped = await asyncio.to_thread(
                scan_cache, self.options['work'].value, ROOT, protected)
        except (OSError, ValueError):
            self.note(self.tr('无法安全识别缓存，请检查抓图缓存目录和权限。'))
            return
        if self.running:
            return
        if not entries:
            self.note(self.tr('没有可清除的图源缓存（跳过 {v0} 个未识别或受保护项目）。', v0=skipped))
            return
        state = self.options['state'].value

        async def confirm(e):
            self.page.pop_dialog()
            if self.running:
                self.note(self.tr('请等待当前任务结束后再清除缓存。'))
                return
            self.running = True
            self.body.disabled = self.start_button.disabled = True
            self.note(self.tr('正在将图源缓存移入废纸篓…'))
            try:
                moved, failed = await asyncio.to_thread(trash_cache, entries, state)
                message = f'Image cache: {moved} books moved to trash; {failed} not removed; {skipped} other items skipped.'
            except (OSError, RuntimeError, ImportError):
                message = 'Cache cleanup incomplete. Check dependencies, permissions and other running tasks.'
            finally:
                self.running = False
                self.body.disabled = self.start_button.disabled = False
            self.logs[0].controls.append(ft.Text(message, size=11, selectable=True))
            self.note(message)

        size_mb = sum(entry.size for entry in entries) / (1024 * 1024)
        self.page.show_dialog(ft.AlertDialog(title=ft.Text(self.tr('清除本地图源缓存？')), scrollable=True,
            content=ft.Container(width=510, content=ft.Column([
                ft.Text(str(root), selectable=True),
                ft.Text(self.tr('已识别 {v0} 本书的缓存，共 {v1:.1f} MB；另有 {v2} 个项目将跳过。', v0=len(entries), v1=size_mb, v2=skipped)),
                ft.Text(self.tr('包含下载原图、章节和匹配索引；移入系统废纸篓，可恢复。再次使用时需重新下载，自动卷 ID 查找也可能需要联网。')),
                ft.Text(self.tr('不会清除 Chrome 登录、ADE 授权、ACSM 准备缓存或输出 EPUB。请先停止其他窗口和 CLI 中的抓图任务。')),
            ], spacing=12)),
            actions=[ft.TextButton(self.tr('取消'), on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton(self.tr('移入废纸篓'), on_click=confirm)]))

    def make_about(self):
        return self.card(ft.Column([
            ft.Text(self.tr('关于 Play Books EPUB Box'), size=22, weight=ft.FontWeight.BOLD),
            ft.Text(self.tr('准备 ACSM / EPUB，获取 Google Play 图书原图并替换低清插图。支持桌面界面与命令行使用。')),
            ft.Row([
                ft.TextButton('DreamGallery / PlayBooks_EPUB-Box ↗', url=PROJECT_URL),
                ft.Text(f'v{APP_VERSION}', size=12, color=ACCENT),
            ], spacing=12, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(),
            ft.Text(self.tr('开源致谢'), size=16, weight=ft.FontWeight.BOLD),
            ft.Text(self.tr('感谢以下项目及其贡献者为本工具提供的基础能力。')),
            ft.TextButton('DeACSM / ACSM Input ↗', url='https://github.com/Leseratte10/acsm-calibre-plugin'),
            ft.Text(self.tr('提供 ACSM 兑换与 ADE 授权相关实现。'), size=13),
            ft.TextButton('DeDRM tools ↗', url='https://github.com/apprenticeharper/DeDRM_tools'),
            ft.Text(self.tr('提供 Adobe ADEPT EPUB 处理相关实现。'), size=13),
            ft.Divider(),
            ft.Text(self.tr('第三方代码保留原版权与许可证；版本和本地修改详见项目 docs/THIRD_PARTY.md。'), size=12),
        ], spacing=16))

    def show_account_help(self, e=None):
        # Sources checked 2026-09-18. Static links never contain account data.
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(self.tr('ByteBooks ID 与账户迁移')), scrollable=True,
            content=ft.Container(width=510, content=ft.Column([
                ft.Text(self.tr('ADE 的新登录已由 Adobe ID 改为 ByteBooks ID。ByteBooks 由 Wipro 运营，官网为 dtsbytebooks.com。')),
                ft.Text(self.tr('已有授权仍能使用时，无需注销、删除或重新授权。仅在新设备授权或需要重新登录时迁移。')),
                ft.Text(self.tr('如何迁移'), weight=ft.FontWeight.BOLD),
                ft.Text(self.tr('1. 打开下方官方迁移帮助，选择已有 Adobe 账户的迁移入口。\n2. 使用原 Adobe ID 的同一邮箱，按官网步骤验证并设置 ByteBooks 密码，以保留旧书籍许可证关联。\n3. 回到本页，填写该邮箱和 ByteBooks 密码；不要继续使用原 Adobe 密码。')),
                ft.Text(self.tr('从未用 Adobe ID 授权的新用户，可在官网创建 ByteBooks 账户。旧版 ADE 中即使仍显示“Adobe ID”，也填写 ByteBooks 凭据。')),
                ft.Text(self.tr('遇到 E_ADEPT_RESET_PW_REQUIRED，应先完成迁移或设置/重置 ByteBooks 密码；无法访问原邮箱时，请联系 ebooks-support@wipro.com。')),
                ft.Text(self.tr('迁移在官方网站完成，本工具不会自动迁移账户或删除既有授权。'), size=12),
                ft.Row([
                    ft.TextButton(self.tr('官方迁移帮助 ↗'), url='https://dtsbytebooks.com/transition-help'),
                    ft.TextButton(self.tr('Adobe 官方说明 ↗'), url='https://helpx.adobe.com/enterprise/kb/eol-faq-adobe-digital-editions.html'),
                ], wrap=True),
            ], spacing=14)),
            actions=[ft.TextButton(self.tr('知道了'), on_click=lambda e: self.page.pop_dialog())]))

    def tr(self, key, **values):
        return translate(self.language, key, **values)

    def apply_locale(self):
        locales = {'zh_CN': ft.Locale('zh', 'CN', 'Hans'),
                   'zh_TW': ft.Locale('zh', 'TW', 'Hant'),
                   'en': ft.Locale('en', 'US'), 'ja': ft.Locale('ja', 'JP')}
        self.page.locale_configuration = ft.LocaleConfiguration(
            supported_locales=list(locales.values()), current_locale=locales[self.language])

    def load_preferences(self):
        try:
            value = json.loads(self.settings_file.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def load_theme(self):
        value = self.load_preferences().get('theme')
        return value if value in ('system', 'light', 'dark') else 'system'

    def save_preferences(self):
        settings = self.load_preferences()
        settings.update(theme=self.theme_select.value, language=self.language)
        self.settings_file.write_text(json.dumps(settings, ensure_ascii=False), encoding='utf-8')

    def change_language(self, e):
        if self.language_select.value not in LANGUAGES:
            return
        self.language = self.language_select.value
        self.apply_locale()
        refresh_ui(self.background, self.language)
        self.page.title = APP_NAME
        try:
            self.save_preferences()
        except OSError:
            self.note(self.tr('语言已切换，但无法保存偏好。'))
        self.page.update()

    def apply_theme(self):
        mode = self.theme_select.value
        self.page.theme_mode = ft.ThemeMode(mode)
        dark = mode == 'dark' or (mode == 'system' and self.page.platform_brightness == ft.Brightness.DARK)
        self.logo.src = (self.logo_svg.replace('#8A6B37', '#C8AC7C').replace('#EEDFC4', '#F3E5CC')
                         if dark else self.logo_svg).encode('utf-8')
        self.selection_color = '#26C8AC7C' if dark else '#208A6B37'
        self.background.gradient = ft.LinearGradient(begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT,
            colors=['#20201E', '#28251F', '#20211F'] if dark else ['#F5F2EB', '#EFE7D8', '#F7F5EF'])
        for card in self.cards:
            card.bgcolor = '#B32C2B28' if dark else '#D9FFFCF6'
            card.border = ft.Border.all(1, '#20D8C5A3' if dark else '#40C6B697')
        for i, button in enumerate(self.nav_buttons):
            button.bgcolor = self.selection_color if i == self.nav_index else None

    def change_theme(self, e):
        self.apply_theme()
        try:
            self.save_preferences()
        except OSError:
            self.note(self.tr('配色已切换，但无法保存偏好。'))
        self.page.update()

    def system_theme(self, e):
        self.apply_theme()
        self.page.update()

    def navigate(self, index):
        self.nav_index = index
        for i, control in enumerate((self.workspace, self.account, self.about)):
            control.visible = i == index
            self.nav_buttons[i].bgcolor = self.selection_color if i == index else None
        for i, log in enumerate(self.logs):
            log.visible = i == index
        self.log_card.visible = index != 2
        self.footer.visible = index != 2
        self.page.update()

    def note(self, message):
        self.status.value = message
        self.page.update()

    async def add_files(self, e):
        if self.running:
            return
        files = await self.picker.pick_files(allow_multiple=True, file_type=ft.FilePickerFileType.CUSTOM,
                                             allowed_extensions=['epub', 'acsm'])
        for item in files or []:
            if item.path and item.path not in [j['path'] for j in self.jobs]:
                self.jobs.append({'path': item.path, 'id': ft.TextField(label=self.tr('Google 卷 ID · 自动'), width=200, text_size=12, dense=True),
                                  'status': ft.Text(self.tr('待处理'), size=12, width=64)})
        self.render_queue()

    def render_queue(self):
        self.queue.controls.clear()
        if not self.jobs:
            self.queue.controls.append(ft.Container(padding=24, alignment=ft.Alignment.CENTER,
                content=ft.Column([ft.Icon(ft.Icons.LIBRARY_ADD_OUTLINED, size=36, color=ACCENT),
                    ft.Text(self.tr('添加 ACSM 或 EPUB 文件'), size=14)],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER)))
        for job in self.jobs:
            self.queue.controls.append(ft.Row([
                ft.Icon(ft.Icons.MENU_BOOK_ROUNDED, color=ACCENT),
                ft.Column([ft.Text(Path(job['path']).name, size=13, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(str(Path(job['path']).parent), size=10, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)], expand=True),
                job['id'], job['status'], ft.IconButton(ft.Icons.CLOSE_ROUNDED, tooltip=self.tr('移除'), on_click=lambda e, j=job: self.remove(j)),
            ], spacing=12))
        self.count.value = self.tr('{v0} 本电子书', v0=len(self.jobs))
        self.page.update()

    def remove(self, job):
        if not self.running:
            self.jobs.remove(job)
            self.render_queue()

    def clear_log(self, e):
        if self.nav_index >= len(self.logs):
            return
        self.logs[self.nav_index].controls.clear()
        self.page.update()

    async def start(self, e):
        if self.running:
            return
        if not self.jobs:
            self.note(self.tr('请先添加 ACSM 或 EPUB 文件。'))
            return
        try:
            options = {key: control.value for key, control in self.options.items()}
            gain, distance, pace = (float(options[k]) for k in ('gain', 'distance', 'pace'))
            if not all(math.isfinite(x) for x in (gain, distance, pace)) or gain <= 0 or not 0 <= distance <= .5 or pace < 0:
                raise ValueError(self.tr('参数范围：增益 > 0；距离 0～0.5；间隔 ≥ 0'))
            tasks = [(job, build_command(job['path'], (job['id'].value or '').strip(), options), None) for job in self.jobs]
        except ValueError as exc:
            self.note(str(exc))
            return
        await self.run_jobs(tasks)

    def auth_command(self, command):
        return [cli_python(), '-u', str(ROOT / 'playbooks_app.py'), command, '--state', self.options['state'].value]

    async def login_google(self, e):
        if self.running:
            return
        profile = (self.options['profile'].value or '').strip()
        if not profile:
            self.note(self.tr('请先选择独立的 Chrome 配置目录。'))
            return
        command = [cli_python(), '-u', str(ROOT / 'playbooks_app.py'), 'login-google', '--profile', profile]
        if await self.run_jobs([(None, command, None)], log_page=0):
            self.note(self.tr('浏览器已关闭；登录状态将在处理时检查。'))

    async def check_status(self, e):
        await self.run_jobs([(None, self.auth_command('status'), None)], log_page=1)

    async def import_auth(self, e):
        if self.running:
            return
        folder = await self.picker.get_directory_path(dialog_title=self.tr('选择含 activation.xml、device.xml、devicesalt 的目录'))
        if folder:
            await self.run_jobs([(None, self.auth_command('import-auth') + ['--folder', folder], None)], log_page=1)

    def confirm_authorize(self, e):
        if self.running:
            return
        if not self.email.value or not self.password.value:
            self.note(self.tr('请输入 ByteBooks ID 邮箱和 ByteBooks 密码。'))
            return
        async def authorize(e):
            self.page.pop_dialog()
            secret = json.dumps({'email': self.email.value.strip(), 'password': self.password.value}) + '\n'
            self.password.value = ''
            await self.run_jobs([(None, self.auth_command('authorize') + ['--stdin-json'], secret)], log_page=1)
        self.page.show_dialog(ft.AlertDialog(title=ft.Text(self.tr('授权此设备？')),
            content=ft.Text(self.tr('此操作会注册 ADE 设备，可能占用设备额度。已有授权建议导入。')),
            actions=[ft.TextButton(self.tr('返回'), on_click=lambda e: self.page.pop_dialog()), ft.FilledButton(self.tr('确认授权'), on_click=authorize)]))

    async def run_jobs(self, tasks, log_page=0):
        if self.running:
            return
        # Bind output to its originating function, not the currently visible page.
        log = self.logs[log_page]
        self.running, self.cancelled = True, False
        self.body.disabled = self.start_button.disabled = True
        self.stop_button.disabled = False
        self.progress.value = None
        self.note(self.tr('处理中…'))
        failures = 0
        job = None
        cancel_dir = None
        try:
            cancel_dir = tempfile.TemporaryDirectory(prefix='playbooks-cancel-')
            self.cancel_file = Path(cancel_dir.name) / 'cancel'
            for job, command, secret in tasks:
                if self.cancelled:
                    break
                if job:
                    job['status'].value = self.tr('处理中')
                self.page.update()
                self.proc = await asyncio.create_subprocess_exec(*command, cwd=ROOT,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, limit=1024 * 1024,
                    env=child_environment(self.cancel_file), **hidden_process_options())
                if secret:
                    self.proc.stdin.write(secret.encode())
                    await self.proc.stdin.drain()
                secret = None
                self.proc.stdin.close()
                if self.cancelled:
                    self.signal_cancel()
                while line := await self.proc.stdout.readline():
                    log.controls.append(ft.Text(line.decode('utf-8', errors='replace').rstrip(), size=11, selectable=True))
                    if len(log.controls) > 600:
                        del log.controls[:100]
                    self.page.update()
                rc = await self.proc.wait()
                failures += int(rc != 0)
                if job:
                    job['status'].value = self.tr('已取消') if self.cancelled else self.tr('完成') if rc == 0 else self.tr('失败')
                self.proc = None
        except Exception:
            failures += 1
            if job:
                job['status'].value = self.tr('失败')
            # Never expose third-party exceptions that may contain credentials.
            log.controls.append(ft.Text('Process error. Check the environment, paths and input files.', size=12))
        finally:
            if self.proc and self.proc.returncode is None:
                self.signal_cancel()
                await self.proc.wait()
            self.proc = None
            self.cancel_file = None
            if cancel_dir:
                cancel_dir.cleanup()
            self.running = False
            self.body.disabled = self.start_button.disabled = False
            self.stop_button.disabled = True
            self.progress.value = 0
            self.note(self.tr('已取消 · 已下载缓存保留') if self.cancelled else self.tr('结束 · {v0} 个任务失败', v0=failures) if failures else self.tr('全部完成'))
        return not self.cancelled and failures == 0

    def signal_cancel(self):
        if self.cancel_file and self.proc and self.proc.returncode is None:
            self.cancel_file.touch()

    async def cancel(self, e=None):
        self.cancelled = True
        self.signal_cancel()
        self.note(self.tr('正在取消，等待请求结束及 Chrome 清理…'))

    async def window_event(self, e):
        if e.type == ft.WindowEventType.CLOSE:
            if self.running:
                self.page.show_dialog(ft.AlertDialog(title=ft.Text(self.tr('任务仍在运行')),
                    content=ft.Text(self.tr('请先取消任务，等待 Chrome 清理完成后再关闭窗口。')),
                    actions=[ft.TextButton(self.tr('知道了'), on_click=lambda e: self.page.pop_dialog())]))
            else:
                await self.page.window.destroy()


def main():
    ft.run(lambda page: Workbench(page))


if __name__ == '__main__':
    main()
