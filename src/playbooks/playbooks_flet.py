"""Flet desktop interface; book processing runs through the shared CLI."""
from __future__ import annotations

import asyncio
import json
import math
from functools import partial
from pathlib import Path
import tempfile

import flet as ft
from .gui_commands import ROOT, MODES, build_command
from .ui_i18n import LANGUAGES, translate, refresh_ui
from .gui_logs import localize_log
from .log_view import LINE_HEIGHT, TEXT_SIZE, LogView
from .log_buffer import LogBuffer
from .runtime import cli_python, child_environment, hidden_process_options
from .paths import SOURCE_DIR
from .library_grouping import natural_title, book_order
from .library_controls import HoverTitle, LibraryBookRow
from .window_layout import configure_window
from .book_typography import source_language, title_language, title_style

# Semantic accents follow the active palette, including newly added queue rows.
ACCENT = ft.Colors.PRIMARY
APP_NAME = 'Play Books EPUB Box'
APP_VERSION = '0.2.0'
PROJECT_URL = 'https://github.com/DreamGallery/PlayBooks_EPUB-Box'
MIN_LOG_HEIGHT = 180
COMPACT_LOG_HEIGHT = 96
MIN_LIBRARY_HEIGHT = 340
DEFAULT_FORM_HEIGHT = 454
CONTENT_SPACING = 16
PAGE_LOGS = {0: 0, 2: 1, 4: 2}
QUEUE_ROW_HEIGHT = 60
QUEUE_HEIGHT = QUEUE_ROW_HEIGHT * 4 + 8 * 3
LIBRARY_ROW_HEIGHT = 48
LIBRARY_ROW_SPACING = 4


class Workbench:
    def __init__(self, page):
        self.page = page
        self.running = False
        self.cancelled = False
        self.proc = None
        self.cancel_file = None
        self.log_buffer = None
        self.buffer_log = None
        self.jobs = []
        self.cards = []
        self.sidebar_height = 0
        self.sidebar_item_heights = {}
        self.nav_buttons = []
        self.nav_index = 0
        self.library_books = []
        self.library_selected = set()
        self.library_expanded = set()
        self.library_search_collapsed = set()
        self.library_query = ''
        self.library_rows = {}
        self.library_refresh_active = False
        self.library_fullscreen = False
        self.library_transition = False
        self.library_series_transition = False
        self.library_scroll_animating = False
        self.library_scroll_offset = 0.0
        self.library_scroll_bounds = None
        self.library_scroll_revision = self.library_render_revision = 0
        self.navigation_revision = 0
        self.library_layout_ready = None
        self.library_titles = []
        self.library_icons = {name: (SOURCE_DIR / 'assets' / filename).read_bytes()
                              for name, filename in (('book', 'library-book.svg'), ('series', 'series.svg'))}
        self.google_signed_in = False
        self.settings_file = ROOT / '.flet-settings.json'
        self.language = self.load_preferences().get('language', 'zh_CN')
        if self.language not in LANGUAGES:
            self.language = 'zh_CN'
        self.apply_locale()
        page.title = APP_NAME
        page.padding = 0
        configure_window(page)
        page.on_media_change = self.window_metrics_changed
        page.window.prevent_close = True
        page.window.on_event = self.window_event
        page.on_keyboard_event = self.keyboard_event
        page.theme = ft.Theme(color_scheme_seed='#1A73E8', use_material3=True,
            color_scheme=ft.ColorScheme(
                primary='#1A73E8', on_primary='#FFFFFF',
                primary_container='#E8F0FE', on_primary_container='#1967D2',
                secondary='#5F6368', on_secondary='#FFFFFF',
                secondary_container='#E8F0FE', on_secondary_container='#1967D2',
                tertiary='#1967D2', on_tertiary='#FFFFFF',
                tertiary_container='#E8F0FE', on_tertiary_container='#174EA6',
                surface='#FFFFFF', on_surface='#202124', on_surface_variant='#5F6368',
                surface_dim='#E8E8E8', surface_bright='#FFFFFF',
                surface_container_lowest='#FFFFFF', surface_container_low='#FCFCFC',
                surface_container='#FAFAFA', surface_container_high='#F6F6F6',
                surface_container_highest='#EEEEEE', surface_tint='#FFFFFF',
                outline='#80868B', outline_variant='#E4E4E4',
                inverse_surface='#303134', on_inverse_surface='#F1F3F4',
                inverse_primary='#8AB4F8'))
        page.dark_theme = ft.Theme(color_scheme_seed='#8AB4F8', use_material3=True,
            color_scheme=ft.ColorScheme(
                primary='#8AB4F8', on_primary='#202124',
                primary_container='#394457', on_primary_container='#D2E3FC',
                secondary='#BDC1C6', on_secondary='#202124',
                secondary_container='#394457', on_secondary_container='#D2E3FC',
                tertiary='#AECBFA', on_tertiary='#202124',
                tertiary_container='#3C4043', on_tertiary_container='#D2E3FC',
                surface='#202124', on_surface='#E8EAED', on_surface_variant='#BDC1C6',
                surface_dim='#171717', surface_bright='#3C4043',
                surface_container_lowest='#171717', surface_container_low='#292A2D',
                surface_container='#303134', surface_container_high='#35363A',
                surface_container_highest='#3C4043', surface_tint='#8AB4F8',
                outline='#9AA0A6', outline_variant='#5F6368',
                inverse_surface='#E8EAED', on_inverse_surface='#303134',
                inverse_primary='#1A73E8'))
        self.picker = ft.FilePicker()
        page.services.append(self.picker)
        self.status = ft.Text(self.tr('就绪'), size=12, text_align=ft.TextAlign.RIGHT)
        self.progress = ft.ProgressBar(value=0, height=3, color=ACCENT)
        self.logs = [LogView(visible=i == 0) for i in range(3)]
        self.count = ft.Text(self.tr('0 本待处理'), size=12)
        self.queue = ft.ListView(height=QUEUE_HEIGHT, spacing=8, padding=0)
        self.options = {
            'mode': ft.Dropdown(label=self.tr('任务类型'), value=MODES[0], options=[ft.DropdownOption(m, self.tr(m)) for m in MODES], expand=True),
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
        self.settings = self.make_settings()
        self.account = self.make_account()
        self.about = self.make_about()
        self.library = self.make_library()
        self.panels = (self.workspace, self.settings, self.account, self.about, self.library)
        self.content_height = 0
        self.main_height = 0
        self.workspace_height = DEFAULT_FORM_HEIGHT
        self.account_height = DEFAULT_FORM_HEIGHT
        self.body = ft.Column(list(self.panels), spacing=0, tight=True)
        self.settings.visible = self.account.visible = self.about.visible = self.library.visible = False
        book_icon = ft.Image(src=(SOURCE_DIR / 'assets' / 'book.svg').read_bytes(),
            width=20, height=20, color=ft.Colors.ON_SURFACE,
            fit=ft.BoxFit.CONTAIN, exclude_from_semantics=True)
        credentials_icon = ft.Image(src=(SOURCE_DIR / 'assets' / 'credentials.svg').read_bytes(),
            width=20, height=20, color=ft.Colors.ON_SURFACE,
            fit=ft.BoxFit.CONTAIN, exclude_from_semantics=True)
        for i, (label, icon) in enumerate([(self.tr('任务处理'), ft.Icon(ft.Icons.CHECKLIST_ROUNDED, size=20)),
                                          (self.tr('设置'), ft.Icon(ft.Icons.TUNE_ROUNDED, size=20)),
                                          (self.tr('ADE 授权'), credentials_icon),
                                          (self.tr('关于'), ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=20)),
                                          (self.tr('Play 书库'), book_icon)]):
            items = [icon, ft.Text(label, size=13, expand=True)]
            self.nav_buttons.append(ft.Container(content=ft.Row(items),
                padding=14, border_radius=14, on_click=lambda e, n=i: self.navigate(n)))
        self.logo_svg = (SOURCE_DIR / 'assets' / 'google-play-books.svg').read_text(encoding='utf-8')
        self.logo = ft.Image(src=self.logo_svg.encode('utf-8'), width=30, height=34,
                             fit=ft.BoxFit.CONTAIN, semantics_label='Google Play Books')
        self.sidebar = self.card(ft.Column([
            ft.Row([self.logo,
                    ft.Text('EPUB Box', size=22, weight=ft.FontWeight.BOLD)]),
            ft.Container(height=12),
            self.nav_buttons[4], self.nav_buttons[2], self.nav_buttons[0], self.nav_buttons[1], ft.Container(expand=True),
            ft.Divider(height=1), self.nav_buttons[3], ft.Divider(height=1),
            ft.Text(self.tr('外观'), size=11), self.theme_select,
            ft.Text(self.tr('语言'), size=11), self.language_select,
        ], spacing=10), width=208, on_size_change=self.resize_sidebar)
        self.sidebar.padding = 16
        for index, item in enumerate(self.sidebar.content.controls):
            if index != 6:
                item.on_size_change = partial(self.sidebar_item_resized, index)
        self.log_card = self.card(ft.Column([
            ft.Row([ft.Text(self.tr('运行日志'), size=12, weight=ft.FontWeight.BOLD), ft.Container(expand=True),
                    ft.TextButton(self.tr('清除'), icon=ft.Icons.DELETE_SWEEP_OUTLINED, on_click=self.clear_log,
                        height=28, style=ft.ButtonStyle(padding=ft.Padding.symmetric(horizontal=8),
                                                       visual_density=ft.VisualDensity.COMPACT))], height=28), *self.logs,
        ], spacing=4), expand=True)
        self.log_card.padding = ft.Padding.only(left=20, right=20, top=8, bottom=12)
        self.form_scroll = ft.Column([self.body], height=DEFAULT_FORM_HEIGHT, spacing=0,
            scroll=ft.ScrollMode.AUTO)
        self.content_area = ft.Column([self.form_scroll, self.log_card], spacing=CONTENT_SPACING,
            expand=True, on_size_change=self.resize_content)
        self.footer = self.card(ft.Column([
            ft.Row([self.start_button, self.stop_button,
                    ft.Container(self.status, expand=True, alignment=ft.Alignment.CENTER_RIGHT)], spacing=8),
            self.progress,
        ], spacing=10))
        self.main_column = ft.Column([self.content_area, self.footer], expand=True, spacing=18,
                                     on_size_change=self.resize_content)
        self.background = ft.Container(expand=True, padding=22, content=ft.Row([
            self.sidebar, self.main_column,
        ], spacing=20, vertical_alignment=ft.CrossAxisAlignment.STRETCH))
        custom_title_bar = getattr(page, 'platform', None) == ft.PagePlatform.MACOS
        page.window.title_bar_hidden = custom_title_bar
        page.window.title_bar_buttons_hidden = False
        self.title_bar = ft.Container(height=28, visible=custom_title_bar,
            content=ft.WindowDragArea(maximizable=False, content=ft.Container(
                content=ft.Text(APP_NAME, size=13, weight=ft.FontWeight.W_500),
                alignment=ft.Alignment.CENTER, padding=ft.Padding.symmetric(horizontal=80))))
        self.apply_theme()
        page.on_platform_brightness_change = self.system_theme
        page.add(ft.Column([self.title_bar, self.background], spacing=0, expand=True))
        self.render_queue()
        self.render_library()
        self.refresh_google_login()
        if self.google_signed_in:
            self.load_library_catalog(silent=True)
        self.options['profile'].on_change = self.refresh_google_login
        self.options['state'].on_change = self.refresh_google_login

    def resize_content(self, e):
        if not math.isfinite(e.height) or e.height <= 0:
            return
        if e.control is self.workspace and self.workspace.visible:
            self.workspace_height = e.height
        elif e.control is self.account and self.account.visible:
            self.account_height = e.height
        elif e.control is self.content_area:
            self.content_height = e.height
        elif e.control is self.main_column:
            self.main_height = e.height
        if self.fit_content():
            self.page.update()

    def resize_sidebar(self, e):
        if not self.sidebar.visible or not math.isfinite(e.height) or e.height <= 0:
            return
        self.sidebar_height = e.height
        compact = e.height < 680
        column = self.sidebar.content
        spacing = 6 if compact else 10
        changed = column.spacing != spacing
        if changed:
            column.spacing = spacing
            column.controls[1].height = 4 if compact else 12
            for button in self.nav_buttons:
                button.padding = 10 if compact else 14
            self.theme_select.height = self.language_select.height = 44 if compact else None
        if self.fit_sidebar() or changed:
            self.page.update()

    def sidebar_item_resized(self, index, e):
        if not math.isfinite(e.height) or e.height < 0:
            return
        self.sidebar_item_heights[index] = e.height
        if self.fit_sidebar():
            self.page.update()

    def fit_sidebar(self):
        column = self.sidebar.content
        indices = [i for i, item in enumerate(column.controls) if i != 6 and item.visible]
        if self.sidebar_height <= 0 or any(i not in self.sidebar_item_heights for i in indices):
            return False
        # Include the flexible spacer's two gaps when deciding whether it fits.
        required = sum(self.sidebar_item_heights[i] for i in indices)
        required += len(indices) * column.spacing + 2 * self.sidebar.padding
        scroll = ft.ScrollMode.AUTO if required > self.sidebar_height + .5 else None
        if column.scroll == scroll:
            return False
        column.scroll = scroll
        column.controls[6].visible = scroll is None
        return True

    def window_metrics_changed(self, e=None):
        if getattr(self.page, 'platform', None) == ft.PagePlatform.WINDOWS:
            if configure_window(self.page):
                self.page.update()

    def fit_content(self):
        changed = False
        if self.nav_index == 4 and self.library_fullscreen:
            height, expand = None, True
            full_height = self.main_height or self.content_height
            if full_height > 0 and self.library.height != full_height:
                self.library.height = full_height
                changed = True
        elif self.nav_index not in PAGE_LOGS:
            height, expand = None, True
            panel = self.panels[self.nav_index]
            if self.nav_index == 1 and self.content_height > 0 and panel.height != self.content_height:
                panel.height = self.content_height
                changed = True
        elif self.content_height <= 0:
            return False
        else:
            # Keep log pages aligned without clipping the taller form.
            log_height = min(MIN_LOG_HEIGHT, max(COMPACT_LOG_HEIGHT,
                self.content_height - MIN_LIBRARY_HEIGHT - CONTENT_SPACING))
            height = min(max(self.workspace_height, self.account_height),
                         max(0, self.content_height - log_height - CONTENT_SPACING))
            expand = False
            library_height = max(MIN_LIBRARY_HEIGHT, height)
            if self.library.height != library_height:
                self.library.height = library_height
                changed = True
        if (self.form_scroll.height, self.form_scroll.expand) == (height, expand):
            return changed
        self.form_scroll.height, self.form_scroll.expand = height, expand
        return True

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
                if key in ('profile', 'state'):
                    self.refresh_google_login()
                else:
                    self.page.update()
        return ft.Row([self.options[key], ft.IconButton(ft.Icons.FOLDER_OPEN_ROUNDED, tooltip=self.tr('选择目录'), on_click=choose)])

    def make_workspace(self):
        return ft.Column([
            self.card(ft.Column([
                ft.Row([ft.Text(self.tr('文件队列'), size=16, weight=ft.FontWeight.BOLD), ft.Container(expand=True), self.count,
                    ft.FilledTonalButton(self.tr('添加电子书'), icon=ft.Icons.ADD_ROUNDED, on_click=self.add_files)]),
                self.queue,
            ], spacing=14)),
            self.card(ft.Row([self.options['mode']])),
        ], spacing=16, on_size_change=self.resize_content)

    def make_library(self):
        self.google_login_button = ft.TextButton(self.tr('登录 Google'), icon=ft.Icons.LOGIN_ROUNDED,
            on_click=self.google_login_action)
        self.library_filter = ft.TextField(label=self.tr('搜索书名或系列'), dense=False,
            height=48, content_padding=ft.Padding.symmetric(horizontal=14, vertical=14),
            text_size=12, on_change=lambda e: self.render_library())
        self.library_count = ft.Text(self.tr('已选择 {v0} 本', v0=0), size=12)
        self.library_summary = ft.Text('', size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        self.library_list_height = self.library_visible_height = None
        self.library_ready_layout = None
        self.library_list = ft.ListView(expand=True, spacing=0, padding=0,
            opacity=0, on_scroll=self.library_scroll_ended, on_size_change=self.library_list_resized)
        self.library_viewport = ft.Container(self.library_list, expand=True, padding=0,
            on_size_change=self.resize_library_list)
        self.library_refresh_button = ft.OutlinedButton(self.tr('刷新书库'), icon=ft.Icons.REFRESH_ROUNDED, on_click=self.refresh_library)
        self.library_clear_button = ft.IconButton(ft.Icons.DELETE_OUTLINE_ROUNDED, tooltip=self.tr('清除 Google 登录凭据'),
            on_click=self.confirm_clear_google)
        self.library_add_button = ft.FilledTonalButton(self.tr('添加所选图书'), icon=ft.Icons.ADD_ROUNDED, on_click=self.add_library_tasks)
        self.library_fullscreen_button = ft.IconButton(ft.Icons.FULLSCREEN_ROUNDED,
            tooltip=self.tr('放大书库'), on_click=self.animate_library_fullscreen)
        self.library_cancel_button = ft.TextButton(self.tr('取消任务'), visible=False, on_click=self.cancel)
        self.library_filter.expand = True
        return self.card(ft.Column([
            ft.Row([ft.Text(self.tr('Play 书库'), size=20, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                self.library_cancel_button,
                self.google_login_button,
                self.library_refresh_button, self.library_clear_button, self.library_fullscreen_button]),
            ft.Row([self.library_filter, self.library_summary], spacing=16),
            self.library_viewport,
            ft.Row([self.library_count, ft.Container(expand=True),
                self.library_add_button]),
            ft.Text(self.tr('无法自动导出时，请选择本地 ACSM / EPUB。'), size=11, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=12), height=DEFAULT_FORM_HEIGHT, on_size_change=self.library_resized)

    def render_library(self, *, update=True):
        self.library_render_revision += 1
        from .volume_lookup import normalize
        query = normalize(self.library_filter.value)
        if query != self.library_query:
            self.library_search_collapsed.clear()
            self.library_query = query
        groups = {}
        self.stop_library_titles()
        for book in sorted(self.library_books, key=book_order):
            if query and query not in normalize(book['title'] + book.get('series_title', '')):
                continue
            group = book.get('series_id') or book.get('series_title') or ''
            groups.setdefault(group, []).append(book)
        previous_headers = {item.data['series']: item for item in self.library_list.controls
                            if isinstance(item.data, dict) and 'series' in item.data}
        previous_rows = dict(self.library_rows)
        self.library_list.controls.clear()
        self.library_rows.clear()
        for group in sorted(groups, key=lambda key: (not bool(key), natural_title(groups[key][0].get('series_title') or ''))):
            books = groups[group]
            title = books[0].get('series_title') or self.tr('未分系列')
            group_language = next((book['language'] for book in books if book.get('language')), '')
            if not group_language:
                group_language = title_language(books[0]['title'])
            expanded = (group not in self.library_search_collapsed) if query else group in self.library_expanded
            header = previous_headers.get(group)
            if header is None:
                header = ft.Container(ft.Row([
                    ft.Image(src=self.library_icons['series'], width=20, height=20, color=ft.Colors.ON_SURFACE_VARIANT,
                        fit=ft.BoxFit.CONTAIN, exclude_from_semantics=True),
                    self.library_title(title, ft.FontWeight.W_500, group_language),
                    ft.Text(size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Icon(ft.Icons.EXPAND_MORE_ROUNDED, size=18),
                ], spacing=10), height=LIBRARY_ROW_HEIGHT, padding=ft.Padding.symmetric(horizontal=12), border_radius=9,
                    margin=ft.Margin.only(bottom=LIBRARY_ROW_SPACING),
                    on_click=partial(self.animate_library_series, group), data={'series': group})
            else:
                self.library_titles.append(header.content.controls[1])
                header.content.controls[1].text.value = title
                header.content.controls[1].text.style = self.book_title_style(title, group_language)
            header.content.controls[2].value = self.tr('{v0} 本电子书', v0=len(books))
            header.content.controls[3].icon = ft.Icons.EXPAND_LESS_ROUNDED if expanded else ft.Icons.EXPAND_MORE_ROUNDED
            header.data['expanded'] = expanded
            self.library_list.controls.append(header)
            if not expanded:
                continue
            for book in books:
                vid = book['id']
                selected = vid in self.library_selected
                if vid in previous_rows:
                    row, checkbox = previous_rows[vid]
                    row.body.controls[1].text.value = book['title']
                    row.body.controls[1].text.style = self.book_title_style(book['title'], book.get('language', ''))
                    self.library_titles.append(row.body.controls[1])
                    checkbox.value = selected
                    checkbox.semantics_label = book['title']
                else:
                    book_icon = ft.Container(ft.Image(src=self.library_icons['book'], width=22, height=22,
                        color=ft.Colors.ON_SURFACE_VARIANT, fit=ft.BoxFit.CONTAIN, exclude_from_semantics=True),
                        width=26, height=32, alignment=ft.Alignment.CENTER)
                    checkbox = ft.Checkbox(value=selected, semantics_label=book['title'],
                        on_change=lambda e, key=vid: self.select_library_book(key, e.control.value))
                    row = LibraryBookRow(ft.Row([book_icon,
                        self.library_title(book['title'], language=book.get('language', '')), checkbox], spacing=12),
                        height=LIBRARY_ROW_HEIGHT, spacing=LIBRARY_ROW_SPACING,
                        on_click=lambda e, key=vid: self.select_library_book(key, key not in self.library_selected),
                        data={'book': vid})
                row.set_expanded(True)
                self.library_rows[vid] = (row, checkbox)
                self.library_list.controls.append(row)
        if not groups:
            self.library_list.controls.append(ft.Container(ft.Text(self.tr('请登录 Google 后刷新书库。') if not self.library_books else self.tr('没有匹配的图书。'),
                size=12, color=ft.Colors.ON_SURFACE_VARIANT), padding=24, alignment=ft.Alignment.CENTER))
        self.library_count.value = self.tr('已选择 {v0} 本', v0=len(self.library_selected))
        for item in self.library_list.controls:
            self.style_library_item(item)
        self.update_library_summary()
        if update:
            self.page.update()

    def style_library_item(self, item):
        data = item.data or {}
        dark = self.page.window.brightness == ft.Brightness.DARK
        if 'series' in data:
            item.bgcolor = '#35363A' if dark else ft.Colors.SURFACE_CONTAINER_LOW
            item.border = ft.Border.all(1, '#54575E') if dark else None
        elif 'book' in data:
            selected = data['book'] in self.library_selected
            item.bgcolor = (ft.Colors.SECONDARY_CONTAINER if selected
                            else '#242529' if dark else None)
            item.border = ft.Border.all(1, '#8AB4F8' if selected else '#484B51') if dark else None

    def resize_library_list(self, e):
        if self.nav_index != 4 or not math.isfinite(e.height) or e.height <= 0:
            return
        step = LIBRARY_ROW_HEIGHT + LIBRARY_ROW_SPACING
        rows = math.floor((e.height + 0.01) / step)
        visible = min(e.height, rows * step)
        self.library_visible_height = visible
        padding = ft.Padding.only(bottom=e.height - visible)
        opacity = 1 if self.library_list_fits() else 0
        if opacity:
            self.library_ready_layout = self.library.height
        if self.library_viewport.padding != padding or self.library_list.opacity != opacity:
            self.library_viewport.padding = padding
            self.library_list.opacity = opacity
            self.page.update()

    def library_list_fits(self):
        return (self.library_list_height is not None and self.library_visible_height is not None
                and math.isclose(self.library_list_height, self.library_visible_height, abs_tol=0.5))

    def library_list_resized(self, e):
        if self.nav_index != 4 or not math.isfinite(e.height) or e.height <= 0:
            return
        self.library_list_height = e.height
        opacity = 1 if self.library_list_fits() else 0
        if opacity:
            self.library_ready_layout = self.library.height
        if self.library_list.opacity != opacity:
            self.library_list.opacity = opacity
            self.page.update()

    async def library_scroll_ended(self, e):
        if self.nav_index != 4 or not all(math.isfinite(v) for v in (e.pixels, e.min_scroll_extent, e.max_scroll_extent)):
            return
        self.library_scroll_bounds = (e.min_scroll_extent, e.max_scroll_extent)
        self.library_scroll_offset = min(e.max_scroll_extent, max(e.min_scroll_extent, e.pixels))
        self.library_scroll_revision += 1
        idle = e.event_type == ft.ScrollType.USER and e.direction == ft.ScrollDirection.IDLE
        if e.event_type == ft.ScrollType.END or idle:
            await self.settle_library_scroll(.03)
        elif e.event_type in (ft.ScrollType.UPDATE, ft.ScrollType.OVERSCROLL):
            await self.settle_library_scroll(.12)

    async def settle_library_scroll(self, delay=.03, *, recheck=True):
        if (self.library_scroll_bounds is None or self.nav_index != 4 or self.library_transition
                or self.library_series_transition or self.library_scroll_animating):
            return
        revision = self.library_scroll_revision, self.library_render_revision
        await asyncio.sleep(delay)
        if (revision != (self.library_scroll_revision, self.library_render_revision) or self.nav_index != 4
                or self.library_transition or self.library_series_transition or self.library_scroll_animating):
            return
        step = LIBRARY_ROW_HEIGHT + LIBRARY_ROW_SPACING
        minimum, maximum = self.library_scroll_bounds
        offset = min(maximum, max(minimum, round(self.library_scroll_offset / step) * step))
        if abs(offset - self.library_scroll_offset) > 0.5:
            await self.scroll_library_to(offset, recheck=recheck)

    async def scroll_library_to(self, offset, duration=140, *, recheck=True):
        revision = self.library_scroll_revision
        self.library_scroll_animating = True
        try:
            await self.library_list.scroll_to(offset=offset, duration=duration,
                                               curve=ft.AnimationCurve.EASE_IN_OUT_CUBIC)
            # Flet acknowledges the command before the client animation finishes.
            await asyncio.sleep(duration / 1000)
        finally:
            self.library_scroll_animating = False
        if recheck and revision != self.library_scroll_revision:
            await self.settle_library_scroll(recheck=False)

    def book_title_style(self, title, language=''):
        return title_style(title, language, getattr(self.page, 'platform', None))

    def library_title(self, title, weight=None, language=''):
        control = HoverTitle(title, weight, self.book_title_style(title, language))
        self.library_titles.append(control)
        return control

    def stop_library_titles(self):
        for title in self.library_titles:
            title.stop()
        self.library_titles.clear()

    def toggle_library_fullscreen(self, e=None):
        self.library_fullscreen = not self.library_fullscreen
        self.navigate(4)

    def library_resized(self, e):
        if (self.library_layout_ready is not None and self.nav_index == 4
                and math.isclose(e.height, self.library.height, abs_tol=0.5)):
            self.library_layout_ready.set()

    async def animate_library_fullscreen(self, e=None):
        if self.library_transition or self.library_series_transition or self.nav_index != 4:
            return
        self.library_transition = True
        revision = self.navigation_revision
        self.library_fullscreen_button.disabled = True
        try:
            self.background.animate_opacity = ft.Animation(100, ft.AnimationCurve.EASE_OUT)
            self.background.animate_scale = ft.Animation(100, ft.AnimationCurve.EASE_OUT)
            self.background.opacity, self.background.scale = 0, 0.985
            self.page.update()
            await asyncio.sleep(0.1)
            if self.nav_index != 4 or revision != self.navigation_revision:
                return
            if self.nav_index == 4:
                self.library_layout_ready = asyncio.Event()
                self.toggle_library_fullscreen()
                # Reveal only after the client has laid out the final viewport.
                try:
                    await asyncio.wait_for(self.library_layout_ready.wait(), timeout=0.6)
                except asyncio.TimeoutError:
                    pass
            if revision != self.navigation_revision:
                return
            self.background.animate_opacity = ft.Animation(160, ft.AnimationCurve.EASE_OUT_CUBIC)
            self.background.animate_scale = ft.Animation(160, ft.AnimationCurve.EASE_OUT_CUBIC)
            self.background.opacity, self.background.scale = 1, 1
            self.page.update()
            await asyncio.sleep(0.16)
        finally:
            self.background.opacity, self.background.scale = 1, 1
            self.library_fullscreen_button.disabled = False
            self.library_transition = False
            self.library_layout_ready = None
            self.page.update()
            await self.settle_library_scroll()

    async def keyboard_event(self, e):
        if e.key == 'Escape' and self.library_fullscreen:
            await self.animate_library_fullscreen()

    def update_library_summary(self):
        self.library_summary.value = (self.tr('正在读取书库…')
            if self.library_refresh_active else
            self.tr('{v0} 本电子书', v0=len(self.library_books)))

    def toggle_library_series(self, group, e=None, *, update=True):
        if self.library_query:
            target = self.library_search_collapsed
        else:
            target = self.library_expanded
        if group in target:
            target.remove(group)
        else:
            target.add(group)
        self.render_library(update=update)

    async def animate_library_series(self, group, e=None):
        if self.library_series_transition or self.library_transition or self.nav_index != 4:
            return
        items = self.library_list.controls
        index = next((i for i, item in enumerate(items)
                      if isinstance(item.data, dict) and item.data.get('series') == group), None)
        if index is None:
            return
        header = items[index]
        expanding = not header.data['expanded']
        step = LIBRARY_ROW_HEIGHT + LIBRARY_ROW_SPACING
        offset = self.library_scroll_offset
        height = self.library_visible_height or 0
        bottom_row = height > step and height - step - .5 <= index * step - offset < height
        self.library_series_transition = True
        self.library_scroll_revision += 1
        self.library_fullscreen_button.disabled = True
        rows = []
        try:
            if expanding:
                self.toggle_library_series(group, update=False)
            revision = self.library_render_revision
            for item in self.library_list.controls[index + 1:]:
                if not isinstance(item, LibraryBookRow):
                    break
                rows.append(item)
            if not rows:
                return
            for row in rows:
                row.set_expanded(False, animated=not expanding)
            if not expanding:
                header.content.controls[3].icon = ft.Icons.EXPAND_MORE_ROUNDED
            self.page.update()
            await rows[0].wait_for_height(0)
            if revision != self.library_render_revision or self.nav_index != 4:
                return
            if expanding:
                for row in rows:
                    row.set_expanded(True, animated=True)
                self.page.update()
                await rows[0].wait_for_height(LIBRARY_ROW_HEIGHT)
                if (bottom_row and revision == self.library_render_revision and self.nav_index == 4
                        and abs(self.library_scroll_offset - offset) < 1):
                    maximum = max(0, len(self.library_list.controls) * step - height)
                    await self.scroll_library_to(min(maximum, offset + step), duration=220)
            else:
                self.toggle_library_series(group)
        finally:
            for row, _ in self.library_rows.values():
                if any(row is item for item in rows):
                    row.set_expanded(True)
            if any(item is header for item in self.library_list.controls):
                header.content.controls[3].icon = (ft.Icons.EXPAND_LESS_ROUNDED if header.data['expanded']
                                                   else ft.Icons.EXPAND_MORE_ROUNDED)
            self.library_series_transition = False
            self.library_fullscreen_button.disabled = False
            self.page.update()
            await self.settle_library_scroll()

    def select_library_book(self, vid, selected):
        if selected:
            self.library_selected.add(vid)
        else:
            self.library_selected.discard(vid)
        if vid in self.library_rows:
            row, checkbox = self.library_rows[vid]
            checkbox.value = selected
            self.style_library_item(row)
        self.library_count.value = self.tr('已选择 {v0} 本', v0=len(self.library_selected))
        self.page.update()

    def library_command(self, action):
        command = [cli_python(), '-u', str(ROOT / 'playbooks_app.py'), action,
            '--profile', self.options['profile'].value, '--state', self.options['state'].value]
        if self.options['show'].value:
            command.append('--show-browser')
        return command

    async def refresh_library(self, e=None):
        if self.running:
            return
        success = await self.run_jobs([(None, self.library_command('library-refresh'), None)], log_page=2, library_updates=True)
        self.refresh_google_login()
        if success:
            self.load_library_catalog()

    def load_library_catalog(self, silent=False):
        from .google_library import read_catalog
        try:
            catalog = read_catalog(Path(self.options['state'].value).expanduser(), self.options['profile'].value)
            self.library_books = catalog['books']
            self.library_selected.intersection_update(book['id'] for book in self.library_books)
            self.render_library()
        except (OSError, ValueError, KeyError):
            if not silent:
                self.note(self.tr('无法读取书库，请刷新重试。'))

    def refresh_google_login(self, e=None):
        try:
            state = Path(self.options['state'].value).expanduser() / 'library' / 'session.json'
            session = json.loads(state.read_text(encoding='utf-8'))
            self.google_signed_in = session.get('signed_in') is True and session.get('profile') == str(
                Path(self.options['profile'].value).expanduser().resolve())
        except (OSError, ValueError, TypeError, AttributeError):
            self.google_signed_in = False
        self.google_login_button.content = self.tr('退出登录') if self.google_signed_in else self.tr('登录 Google')
        self.google_login_button.icon = ft.Icons.LOGOUT_ROUNDED if self.google_signed_in else ft.Icons.LOGIN_ROUNDED
        if not self.google_signed_in and self.library_books:
            self.library_books.clear()
            self.library_selected.clear()
            self.render_library()
        self.page.update()

    async def google_login_action(self, e=None):
        if self.running:
            return
        if not self.google_signed_in:
            await self.login_google(e)
            return

        async def confirm(e):
            self.page.pop_dialog()
            command = [cli_python(), '-u', str(ROOT / 'playbooks_app.py'), 'logout-google',
                '--profile', self.options['profile'].value, '--state', self.options['state'].value]
            await self.run_jobs([(None, command, None)], log_page=2)
            self.refresh_google_login()

        self.page.show_dialog(ft.AlertDialog(title=ft.Text(self.tr('退出 Google 登录？')),
            content=ft.Text(self.tr('清除本工具独立浏览器的登录 Cookie；不会影响日常 Chrome、ADE 授权或电子书文件。')),
            actions=[ft.TextButton(self.tr('取消'), on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(self.tr('退出登录'), on_click=confirm)]))

    def protected_files(self):
        return [self.options[key].value for key in ('output', 'images', 'work')] + [
            str(ROOT / 'hires'), *[job['path'] for job in self.jobs if job['path']]]

    async def run_cleanup(self, action, log_page):
        if self.running:
            return False
        self.running = self.body.disabled = self.start_button.disabled = True
        self.progress.value = None
        self.note(self.tr('清理中…'))
        success = False
        try:
            message, success = await asyncio.to_thread(action)
        except Exception:
            message = ('Cleanup incomplete. Close this tool\'s Chrome windows and other tasks, and check the selected paths. '
                       'Any items already moved can be recovered from Trash.')
        finally:
            self.running = self.body.disabled = self.start_button.disabled = False
            self.progress.value = 0
        self.append_log(self.logs[log_page], message)
        self.note(self.tr('清理完成') if success else self.tr('清理未完成，请检查日志。'))
        return success

    async def confirm_clear_google(self, e=None):
        if self.running:
            return
        profile, state = self.options['profile'].value, self.options['state'].value
        protected = self.protected_files() + [state]

        async def confirm(e):
            self.page.pop_dialog()
            from .google_credentials import trash_credentials
            def action():
                count = trash_credentials(profile, ROOT, state, protected)
                return f'Google credential cleanup: {count} browser data items moved to Trash. ADE authorization unchanged.', True
            if await self.run_cleanup(action, 2):
                self.refresh_google_login()

        self.page.show_dialog(ft.AlertDialog(title=ft.Text(self.tr('清除 Google 登录凭据？')), scrollable=True,
            content=ft.Container(width=520, content=ft.Column([
                ft.Text(profile, selectable=True),
                ft.Text(self.tr('请先关闭本工具的 Chrome。独立配置中的 Cookie、已保存密码和网站数据将移入废纸篓，需要重新登录；不会清除 ADE 授权或电子书。')),
                ft.Text(self.tr('移入废纸篓后仍可恢复；如需彻底删除凭据，请自行清空对应项目。'), size=12),
            ], spacing=12)), actions=[ft.TextButton(self.tr('取消'), on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(self.tr('移入废纸篓'), on_click=confirm)]))

    def new_job(self, path, vid='', title='', language=''):
        return {'path': path, 'title': title, 'library_id': vid if title else '',
            'language': source_language(path) or language,
            'id': ft.TextField(label=self.tr('Google 卷 ID · 自动'), value=vid, width=200, text_size=12, dense=True),
            'status': ft.Text(self.tr('待处理') if path else self.tr('需本地文件'), size=12, width=80)}

    async def add_library_tasks(self, e=None):
        if self.running:
            return
        existing = {job.get('library_id') for job in self.jobs}
        ids = [book['id'] for book in self.library_books if book['id'] in self.library_selected and book['id'] not in existing]
        if not ids:
            self.note(self.tr('请选择尚未加入任务的图书。'))
            return
        with tempfile.TemporaryDirectory(prefix='playbooks-export-') as folder:
            result = Path(folder) / 'result.json'
            command = self.library_command('library-export') + ['--result', str(result), '--id', *ids]
            await self.run_jobs([(None, command, None)], log_page=2)
            try:
                books = json.loads(result.read_text(encoding='utf-8'))['books']
            except (OSError, ValueError, KeyError):
                return
        for book in books:
            language = book.get('language') or next((item.get('language', '') for item in self.library_books
                                                    if item['id'] == book['id']), '')
            self.jobs.append(self.new_job(book.get('path') or None, book['id'], book['title'], language))
            self.library_selected.discard(book['id'])
        self.render_library()
        self.render_queue()
        self.navigate(0)
        if any(not book.get('path') for book in books):
            self.note(self.tr('部分图书未能导出，请关联本地 ACSM / EPUB。'))

    async def attach_source(self, job, e=None):
        if self.running:
            return
        files = await self.picker.pick_files(allow_multiple=False, file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=['epub', 'acsm'])
        if files and files[0].path:
            job['path'] = files[0].path
            job['language'] = source_language(job['path']) or job.get('language', '')
            job['status'].value = self.tr('待处理')
            self.render_queue()

    def make_settings(self):
        return self.card(ft.Column([
            ft.Text(self.tr('设置'), size=20, weight=ft.FontWeight.BOLD),
            *[self.folder_field(k) for k in ('profile', 'work', 'images', 'output')],
            ft.Row([self.options[k] for k in ('gain', 'distance', 'pace')]),
            ft.Row([self.options[k] for k in ('show', 'keep', 'dry_run', 'overwrite')], wrap=True),
            ft.TextButton(self.tr('清除缓存'), icon=ft.Icons.DELETE_SWEEP_OUTLINED,
                on_click=self.confirm_clear_cache),
        ], spacing=16, scroll=ft.ScrollMode.AUTO))

    def make_account(self):
        return self.card(ft.Column([
            ft.Text(self.tr('ADE 授权'), size=20, weight=ft.FontWeight.BOLD),
            ft.Text(self.tr('导入已有授权，或使用 ByteBooks ID 登录。'), size=12),
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
        ], spacing=16), on_size_change=self.resize_content)

    async def confirm_clear_cache(self, e=None):
        if self.running:
            self.note(self.tr('请等待当前任务结束后再清除缓存。'))
            return
        from .cache_cleanup import scan_cache, scan_work_cache, trash_caches
        state = self.options['state'].value
        protected = [str(ROOT / '.chrome-profile'), self.options['profile'].value,
            str(ROOT / 'hires'), self.options['output'].value, self.options['images'].value,
            *[job['path'] for job in self.jobs if job['path']]]
        def scan():
            images = scan_cache(self.options['work'].value, ROOT,
                protected + [str(ROOT / '.playbooks-state'), state])
            work = scan_work_cache(state, ROOT, protected + [self.options['work'].value])
            return images, work
        try:
            images, work = await asyncio.to_thread(scan)
        except (OSError, ValueError):
            self.note(self.tr('无法安全识别缓存，请检查目录和权限。'))
            return
        if self.running:
            return
        entries = images[1] + work[1]
        skipped = images[2] + work[2]
        if not entries:
            self.note(self.tr('没有可清除的缓存（跳过 {v0} 个未识别或受保护项目）。', v0=skipped))
            return

        async def confirm(e):
            self.page.pop_dialog()
            def action():
                image_moved, work_moved, failed = trash_caches(images[1], work[1], state)
                return (f'Cache cleanup: {image_moved} image caches and {work_moved} working caches moved to Trash; '
                        f'{failed} not moved; {skipped} protected or unknown items skipped.', failed == 0)
            await self.run_cleanup(action, 0)

        size_mb = sum(entry.size for entry in entries) / (1024 * 1024)
        self.page.show_dialog(ft.AlertDialog(title=ft.Text(self.tr('清除缓存？')), scrollable=True,
            content=ft.Container(width=520, content=ft.Column([
                ft.Text(str(images[0]), selectable=True), ft.Text(str(work[0]), selectable=True),
                ft.Text(self.tr('图源缓存 {v0} 项，工作缓存 {v1} 项；共 {v2:.1f} MB，跳过 {v3} 项。',
                    v0=len(images[1]), v1=len(work[1]), v2=size_mb, v3=skipped)),
                ft.Text(self.tr('下载原图、匹配索引及下载／准备的电子书缓存将移入废纸篓，可恢复；再次使用可能需要重新下载。')),
                ft.Text(self.tr('保留 Chrome 登录、ADE 授权、本地原图、队列源文件和输出 EPUB。请先停止其他窗口或 CLI 中的任务。')),
            ], spacing=12)),
            actions=[ft.TextButton(self.tr('取消'), on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton(self.tr('移入废纸篓'), on_click=confirm)]))

    def make_about(self):
        return self.card(ft.Column([
            ft.Text(self.tr('关于 Play Books EPUB Box'), size=22, weight=ft.FontWeight.BOLD),
            ft.Text(self.tr('导入 ACSM 或 EPUB，用 Google Play 图书原图替换低清插图。支持图形界面与命令行。')),
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
            ft.Text(self.tr('提供 Adobe ADEPT EPUB 解密实现。'), size=13),
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
        for log in self.logs:
            for line in log.controls:
                if isinstance(line.data, dict) and 'raw_log' in line.data:
                    line.value = localize_log(line.data['raw_log'], self.language)
        self.render_library()
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
        self.page.window.brightness = ft.Brightness.DARK if dark else ft.Brightness.LIGHT
        self.title_bar.bgcolor = '#303134' if dark else '#F6F6F6'
        self.page.window.bgcolor = self.title_bar.bgcolor
        self.selection_color = '#394457' if dark else '#E8F0FE'
        self.navigation_selected_color = '#8AB4F8' if dark else '#1967D2'
        self.background.gradient = None
        self.background.bgcolor = '#202124' if dark else '#F8F8F8'
        self.page.bgcolor = self.background.bgcolor
        for card in self.cards:
            card.bgcolor = '#292A2D' if dark else '#FFFFFF'
            card.border = ft.Border.all(1, '#3C4043' if dark else '#E4E4E4')
        self.sidebar.bgcolor = '#252629' if dark else '#FCFCFC'
        for dropdown in (self.theme_select, self.language_select, self.options['mode']):
            dropdown.filled = True
            dropdown.fill_color = '#2D2E31' if dark else '#FAFAFA'
            dropdown.bgcolor = '#303134' if dark else '#FCFCFC'
            dropdown.elevation = 3
            dropdown.menu_style = ft.MenuStyle(
                shadow_color='#30000000' if dark else '#18000000',
                side=ft.BorderSide(1, '#3C4043' if dark else '#E4E4E4'))
        for item in self.library_list.controls:
            self.style_library_item(item)
        self.update_navigation()

    def update_navigation(self):
        for i, button in enumerate(self.nav_buttons):
            selected = i == self.nav_index
            button.bgcolor = self.selection_color if selected else None
            for control in button.content.controls:
                control.color = self.navigation_selected_color if selected else ft.Colors.ON_SURFACE_VARIANT

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
        if index != self.nav_index:
            self.navigation_revision += 1
            self.library_render_revision += 1
            self.library_scroll_revision += 1
            # Native ListView state is recreated when its panel becomes visible.
            self.library_scroll_offset = 0.0
            self.library_scroll_bounds = None
            if self.library_transition:
                self.background.animate_opacity = self.background.animate_scale = None
                self.background.opacity, self.background.scale = 1, 1
                if self.library_layout_ready is not None:
                    self.library_layout_ready.set()
        if index != 4:
            self.library_fullscreen = False
            for title in self.library_titles:
                title.stop()
        self.nav_index = index
        for i, control in enumerate(self.panels):
            control.visible = i == index
        self.update_navigation()
        for i, log in enumerate(self.logs):
            log.visible = i == PAGE_LOGS.get(index)
        full = index == 4 and self.library_fullscreen
        self.sidebar.visible = not full
        self.log_card.visible = index in PAGE_LOGS and not full
        self.footer.visible = index != 3 and not full
        self.library_fullscreen_button.icon = ft.Icons.FULLSCREEN_EXIT_ROUNDED if full else ft.Icons.FULLSCREEN_ROUNDED
        self.library_fullscreen_button.tooltip = self.tr('恢复布局') if full else self.tr('放大书库')
        self.library_cancel_button.visible = full and self.library_refresh_active
        self.fit_content()
        if index == 4 and self.library_ready_layout != self.library.height:
            self.library_list.opacity = 0
            self.library_list_height = self.library_visible_height = None
            self.library_ready_layout = None
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
                self.jobs.append(self.new_job(item.path))
        self.render_queue()

    def render_queue(self):
        self.queue.controls.clear()
        if not self.jobs:
            self.queue.controls.append(ft.Container(height=QUEUE_HEIGHT, padding=24, alignment=ft.Alignment.CENTER,
                content=ft.Column([ft.Icon(ft.Icons.LIBRARY_ADD_OUTLINED, size=36, color=ACCENT),
                    ft.Text(self.tr('添加 ACSM 或 EPUB 文件'), size=14)],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER)))
        for job in self.jobs:
            title = job.get('title') or Path(job['path']).name
            self.queue.controls.append(ft.Container(height=QUEUE_ROW_HEIGHT, content=ft.Row([
                ft.Icon(ft.Icons.MENU_BOOK_ROUNDED, color=ACCENT),
                ft.Column([ft.Text(title, size=13, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                    style=self.book_title_style(title, job.get('language', ''))),
                    ft.Text(str(Path(job['path']).parent) if job['path'] else self.tr('请选择对应的 ACSM / EPUB'), size=10, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)], expand=True),
                ft.IconButton(ft.Icons.ATTACH_FILE_ROUNDED, tooltip=self.tr('关联本地文件'),
                    visible=bool(job.get('library_id')), on_click=partial(self.attach_source, job)),
                job['id'], job['status'], ft.IconButton(ft.Icons.CLOSE_ROUNDED, tooltip=self.tr('移除'), on_click=lambda e, j=job: self.remove(j)),
            ], spacing=12)))
        self.count.value = self.tr('{v0} 本电子书', v0=len(self.jobs))
        self.page.update()

    def remove(self, job):
        if not self.running:
            self.jobs.remove(job)
            self.render_queue()

    def append_log(self, log, raw):
        log.controls.append(ft.Text(localize_log(raw, self.language), data={'raw_log': raw},
                                    size=TEXT_SIZE, style=ft.TextStyle(height=LINE_HEIGHT / TEXT_SIZE),
                                    selectable=True))
        if len(log.controls) > 600:
            del log.controls[:100]

    def render_log_batch(self, log, lines):
        for line in lines:
            self.append_log(log, line)
        self.page.update()

    def clear_log(self, e):
        log_index = PAGE_LOGS.get(self.nav_index)
        if log_index is None:
            return
        if self.buffer_log is self.logs[log_index] and self.log_buffer is not None:
            self.log_buffer.clear()
        self.logs[log_index].clear()
        self.page.update()

    async def start(self, e):
        if self.running:
            return
        if not self.jobs:
            self.note(self.tr('请先添加 ACSM 或 EPUB 文件。'))
            return
        try:
            if any(not job['path'] for job in self.jobs):
                raise ValueError(self.tr('请先为待处理图书关联本地文件。'))
            options = {key: control.value for key, control in self.options.items()}
            gain, distance, pace = (float(options[k]) for k in ('gain', 'distance', 'pace'))
            if not all(math.isfinite(x) for x in (gain, distance, pace)) or gain <= 0 or not 0 <= distance <= .5 or pace < 0:
                raise ValueError(self.tr('参数范围：增益 > 0；距离 0～0.5；间隔 ≥ 0'))
            tasks = []
            for job in self.jobs:
                job_options = dict(options)
                if job.get('library_id') and not job_options['output']:
                    job_options['output'] = str(ROOT / 'hires')
                tasks.append((job, build_command(job['path'], (job['id'].value or '').strip(), job_options), None))
        except ValueError as exc:
            self.note(str(exc))
            return
        self.navigate(0)
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
        command = [cli_python(), '-u', str(ROOT / 'playbooks_app.py'), 'login-google', '--profile', profile,
            '--state', self.options['state'].value]
        success = await self.run_jobs([(None, command, None)], log_page=2, library_updates=True)
        self.refresh_google_login()
        if success:
            self.load_library_catalog()

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

    def set_job_controls(self, running, library_updates=False):
        self.body.disabled = running and not library_updates
        for panel in self.panels:
            panel.disabled = running and library_updates and panel is not self.library
        for button in (self.google_login_button, self.library_refresh_button, self.library_clear_button, self.library_add_button):
            button.disabled = running
        self.start_button.disabled = running
        self.stop_button.disabled = not running
        self.library_refresh_active = running and library_updates
        self.library_cancel_button.visible = self.library_fullscreen and self.library_refresh_active
        self.update_library_summary()

    async def run_jobs(self, tasks, log_page=0, library_updates=False):
        if self.running:
            return
        if self.library_fullscreen and not library_updates:
            self.library_fullscreen = False
            self.navigate(self.nav_index)
        # Bind output to its originating function, not the currently visible page.
        log = self.logs[log_page]
        self.running, self.cancelled = True, False
        self.set_job_controls(True, library_updates)
        self.progress.value = None
        self.note(self.tr('处理中…'))
        failures = 0
        job = None
        cancel_dir = None
        self.buffer_log = log
        self.log_buffer = LogBuffer(partial(self.render_log_batch, log))
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
                    text = line.decode('utf-8', errors='replace').rstrip()
                    if library_updates:
                        from .google_library import CATALOG_EVENT
                        if text == CATALOG_EVENT:
                            self.refresh_google_login()
                            self.load_library_catalog(silent=True)
                            continue
                    self.log_buffer.append(text)
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
            self.log_buffer.append('Process error. Check the environment, paths and input files.')
        finally:
            if self.proc and self.proc.returncode is None:
                self.signal_cancel()
                await self.proc.wait()
            self.proc = None
            self.cancel_file = None
            if cancel_dir:
                cancel_dir.cleanup()
            self.log_buffer.flush()
            self.log_buffer = self.buffer_log = None
            self.running = False
            self.set_job_controls(False)
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
        if e.type == ft.WindowEventType.MOVED:
            self.window_metrics_changed()
        elif e.type == ft.WindowEventType.CLOSE:
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
