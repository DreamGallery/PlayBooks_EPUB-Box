"""A clipped desktop log viewport with hard scroll boundaries."""
import math

import flet as ft

LINE_HEIGHT = 20
TEXT_SIZE = 11


class LogView(ft.Container):
    def __init__(self, visible=True):
        self.scroll_offset = self.viewport_height = self.content_height = 0.0
        self.follow_tail = True
        self._motion_source = None
        self._motion_remainder = 0.0
        self.lines = ft.Column(spacing=0, tight=True, on_size_change=self._measure_content)
        self.layer = ft.Container(self.lines, left=0, right=16, top=0)
        self.thumb = ft.Container(left=3, right=3, top=0, height=28,
                                  bgcolor=ft.Colors.OUTLINE, border_radius=4)
        self.track = ft.GestureDetector(
            ft.Stack([self.thumb]), right=0, top=0, bottom=0, width=12,
            visible=False, mouse_cursor=ft.MouseCursor.CLICK,
            on_tap_down=self._track_click, on_vertical_drag_update=self._drag_thumb)
        self.viewport = ft.Stack([self.layer, self.track], fit=ft.StackFit.EXPAND,
                                 clip_behavior=ft.ClipBehavior.HARD_EDGE)
        # Flet's ListView uses macOS bounce physics with no per-control override.
        # Separate identities make equal-sized log pages report their own viewport.
        super().__init__(key=f'log-view-{id(self)}',
            content=ft.GestureDetector(self.viewport, on_scroll=self._wheel,
            on_vertical_drag_update=self._drag_content,
            allowed_devices=[ft.PointerDeviceType.TOUCH, ft.PointerDeviceType.TRACKPAD]),
            expand=True, visible=visible, clip_behavior=ft.ClipBehavior.HARD_EDGE,
            alignment=ft.Alignment.TOP_LEFT,
            on_size_change=self._measure_viewport)

    @property
    def controls(self):
        return self.lines.controls

    @property
    def max_offset(self):
        if self.visible_height <= 0:
            return 0.0
        rows = math.ceil(max(0, self.content_height - 0.1) / LINE_HEIGHT)
        return max(0.0, rows * LINE_HEIGHT - self.visible_height)

    @property
    def visible_height(self):
        return math.floor(self.viewport_height / LINE_HEIGHT) * LINE_HEIGHT

    def clear(self):
        self.controls.clear()
        self.scroll_offset = self.content_height = 0.0
        self.follow_tail = True
        self._motion_remainder = 0.0
        self._position()

    def _position(self):
        self.scroll_offset = min(max(0.0, self.scroll_offset), self.max_offset)
        self.layer.top = -self.scroll_offset
        if self.viewport_height > 0:
            self.viewport.height = self.visible_height
        self.track.visible = self.max_offset > 0 and self.viewport_height > 0
        self.thumb.height = min(self.visible_height,
            max(28.0, self.visible_height ** 2 / max(1.0, self.content_height)))
        travel = max(0.0, self.visible_height - self.thumb.height)
        self.thumb.top = travel * self.scroll_offset / max(1.0, self.max_offset)

    def _move(self, offset):
        if not math.isfinite(offset):
            return
        bounded = min(max(0.0, round(offset / LINE_HEIGHT) * LINE_HEIGHT), self.max_offset)
        self.follow_tail = self.max_offset - bounded < 0.5
        if bounded != self.scroll_offset:
            self.scroll_offset = bounded
            self._position()
            self.update()

    def _measure_viewport(self, event):
        if event.height <= 0 or not math.isfinite(event.height):
            return
        self.viewport_height = event.height
        self._resize()

    def _measure_content(self, event):
        if event.height < 0 or not math.isfinite(event.height):
            return
        if event.height == 0 and self.controls:
            return  # A hidden log page must retain its position.
        self.content_height = event.height
        self._resize()

    def _resize(self):
        if self.follow_tail:
            self.scroll_offset = self.max_offset
        self._position()
        if self.max_offset == 0:
            self.follow_tail = True
        self.update()

    def _wheel(self, event):
        self._scroll_by(event.scroll_delta.y, 'wheel')

    def _drag_content(self, event):
        self._scroll_by(-(event.primary_delta or 0), 'content')

    def _drag_thumb(self, event):
        travel = self.visible_height - self.thumb.height
        if travel > 0:
            self._scroll_by((event.primary_delta or 0) * self.max_offset / travel, 'thumb')

    def _scroll_by(self, delta, source):
        if not math.isfinite(delta) or delta == 0:
            return
        if source != self._motion_source or delta * self._motion_remainder < 0:
            self._motion_remainder = 0.0
        self._motion_source = source
        self._motion_remainder += delta
        steps = math.trunc(self._motion_remainder / LINE_HEIGHT)
        self._motion_remainder -= steps * LINE_HEIGHT
        if steps:
            self._move(self.scroll_offset + steps * LINE_HEIGHT)
        if (delta < 0 and self.scroll_offset == 0 or
                delta > 0 and self.scroll_offset == self.max_offset):
            self._motion_remainder = 0.0

    def _track_click(self, event):
        y = event.local_position.y
        self._motion_remainder = 0.0
        if y < self.thumb.top:
            self._move(self.scroll_offset - self.visible_height)
        elif y > self.thumb.top + self.thumb.height:
            self._move(self.scroll_offset + self.visible_height)
