"""Compact library rows and hover-only title scrolling."""
import asyncio
import math

import flet as ft


class LibraryBookRow(ft.Container):
    def __init__(self, body, height, spacing, **kwargs):
        self.body = body
        self._row_height, self._spacing = height, spacing
        self._measured_height = self._target_height = None
        self._layout_ready = None
        inner = ft.Container(body, top=0, left=0, right=0, height=height,
                             padding=ft.Padding.symmetric(horizontal=14, vertical=6))
        super().__init__(ft.Stack([inner], clip_behavior=ft.ClipBehavior.HARD_EDGE),
            height=height, margin=ft.Margin.only(left=12, bottom=spacing), border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE, on_size_change=self._measure, **kwargs)

    def set_expanded(self, expanded, *, animated=False):
        self.animate = ft.Animation(200 if animated else 0, ft.AnimationCurve.EASE_IN_OUT_CUBIC)
        self.animate_opacity = ft.Animation(160 if animated else 0, ft.AnimationCurve.EASE_OUT)
        self.height = self._row_height if expanded else 0
        self.margin = ft.Margin.only(left=12, bottom=self._spacing if expanded else 0)
        self.opacity = 1 if expanded else 0

    def _measure(self, e):
        if not math.isfinite(e.height) or e.height < 0:
            return
        self._measured_height = e.height
        if self._layout_ready is not None and math.isclose(e.height, self._target_height, abs_tol=.5):
            self._layout_ready.set()

    async def wait_for_height(self, height):
        height += self._spacing if height else 0
        if self._measured_height is not None and math.isclose(self._measured_height, height, abs_tol=.5):
            return
        self._target_height, self._layout_ready = height, asyncio.Event()
        try:
            await asyncio.wait_for(self._layout_ready.wait(), timeout=.4)
        except asyncio.TimeoutError:
            pass
        finally:
            self._layout_ready = None


class HoverTitle(ft.Container):
    def __init__(self, title, weight=None, style=None):
        self._text_width = self._viewport_width = 0
        self._task = None
        self.text = ft.Text(title, size=12, weight=weight, style=style, no_wrap=True,
                            on_size_change=self._measure_text)
        self.scroller = ft.Row([self.text], scroll=ft.ScrollMode.HIDDEN, spacing=0,
                              on_size_change=self._measure_viewport)
        super().__init__(content=self.scroller, expand=True, height=22,
                         clip_behavior=ft.ClipBehavior.HARD_EDGE, on_hover=self._hover)

    def _measure_text(self, event):
        self._text_width = event.width

    def _measure_viewport(self, event):
        self._viewport_width = event.width

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    def will_unmount(self):
        self.stop()

    async def _hover(self, event):
        self.stop()
        if event.data is True or event.data == 'true':
            distance = self._text_width - self._viewport_width
            if distance > 2:
                self._task = asyncio.create_task(self._scroll(distance))
        else:
            try:
                await self.scroller.scroll_to(offset=0)
            except (RuntimeError, TimeoutError):
                pass

    async def _scroll(self, distance):
        duration = max(1600, round(distance / 32 * 1000))
        try:
            while True:
                await asyncio.sleep(.65)
                await self.scroller.scroll_to(offset=distance, duration=duration, curve=ft.AnimationCurve.LINEAR)
                await asyncio.sleep(duration / 1000 + 1)
                await self.scroller.scroll_to(offset=0, duration=250)
                await asyncio.sleep(.5)
        except (asyncio.CancelledError, RuntimeError, TimeoutError):
            pass
