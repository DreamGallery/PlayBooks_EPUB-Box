"""Fit the fixed desktop window to the current Windows monitor's usable area."""
import math

import flet as ft

DEFAULT_SIZE = (1200, 900)
SCREEN_MARGIN = 12


def windows_work_area(point):
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD)]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    set_context = user32.SetThreadDpiAwarenessContext
    set_context.argtypes, set_context.restype = [ctypes.c_void_p], ctypes.c_void_p
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    previous = set_context(ctypes.c_void_p(-4))
    if not previous:
        raise OSError('Cannot query the monitor in physical pixels')
    try:
        monitor = user32.MonitorFromPoint(wintypes.POINT(*point), 2)
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise OSError('Cannot query the monitor work area')
        return info.work.left, info.work.top, info.work.right, info.work.bottom
    finally:
        set_context(previous)


def fitted_bounds(area, ratio):
    left, top, right, bottom = (value / ratio for value in area)
    width = min(DEFAULT_SIZE[0], math.floor(right - left - 2 * SCREEN_MARGIN))
    height = min(DEFAULT_SIZE[1], math.floor(bottom - top - 2 * SCREEN_MARGIN))
    if width <= 0 or height <= 0:
        raise ValueError('Invalid monitor work area')
    return left, top, right, bottom, width, height


def configure_window(page):
    window = page.window
    width, height = DEFAULT_SIZE
    area = None
    fixed = True
    windows = getattr(page, 'platform', None) == ft.PagePlatform.WINDOWS
    if windows:
        ratio = getattr(getattr(page, 'media', None), 'device_pixel_ratio', 1)
        try:
            if not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
                raise ValueError('Invalid display scale')
            point = (round(((window.left or 0) + (window.width or width) / 2) * ratio),
                     round(((window.top or 0) + (window.height or height) / 2) * ratio))
            area = fitted_bounds(windows_work_area(point), ratio)
            left, top, right, bottom, width, height = area
        except (OSError, AttributeError, ValueError):
            # Keep the window recoverable if display information is unavailable.
            fixed = False
            width, height = window.width or width, window.height or height
    # Flet applies size before min/max bounds. On Windows, lock user resizing
    # without locking programmatic resizing when the monitor/DPI changes.
    constrained = fixed and not windows
    values = dict(width=width, height=height, min_width=width if constrained else 0 if fixed else 640,
                  max_width=width if constrained else None, min_height=height if constrained else 0 if fixed else 480,
                  max_height=height if constrained else None, resizable=not fixed, maximizable=not fixed)
    if area:
        values['left'] = min(max(window.left if window.left is not None else left + SCREEN_MARGIN,
                                 left + SCREEN_MARGIN), right - width - SCREEN_MARGIN)
        values['top'] = min(max(window.top if window.top is not None else top + SCREEN_MARGIN,
                                top + SCREEN_MARGIN), bottom - height - SCREEN_MARGIN)
    changed = False
    for key, value in values.items():
        if getattr(window, key) != value:
            setattr(window, key, value)
            changed = True
    return changed
