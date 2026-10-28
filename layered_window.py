"""Windows 分层窗口逐像素透明合成。"""

import ctypes as c
from dataclasses import dataclass
import logging
import os
import threading
from ctypes import wintypes as w

import numpy as np


@dataclass(frozen=True)
class MousePassthroughResult:
    success: bool
    requested: bool
    effective: bool | None
    error: str = ""
    hwnd: int = 0


class SIZE(c.Structure):
    _fields_ = [("cx", w.LONG), ("cy", w.LONG)]


class BLEND(c.Structure):
    _fields_ = [("operation", w.BYTE), ("flags", w.BYTE), ("alpha", w.BYTE), ("format", w.BYTE)]


class BITMAPINFOHEADER(c.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                ("image_size", w.DWORD), ("x", w.LONG), ("y", w.LONG),
                ("used", w.DWORD), ("important", w.DWORD)]


class LayeredWindow:
    """将预乘 Alpha 的 BGRA 位图提交给桌面合成器。"""

    _LAYERED = 0x00080000
    _TRANSPARENT = 0x00000020
    _NOACTIVATE = 0x08000000
    _INPUT_MASK = _LAYERED | _TRANSPARENT | _NOACTIVATE

    def __init__(self, window):
        self.window = window
        self._thread_id = threading.get_ident()
        self._native_thread_id = threading.get_native_id()
        self._closed = False
        self._hwnd = 0
        self._baseline_noactivate = 0
        self._requested_passthrough = False
        self._effective = None
        self._input_busy = False
        self._suspended_hwnd = 0
        self._last_input_result = None
        self.on_mouse_passthrough_changed = None
        self.user = c.WinDLL("user32", use_last_error=True)
        self.gdi = c.WinDLL("gdi32", use_last_error=True)
        pointer = c.sizeof(c.c_void_p) == 8
        self._get_window_long = self.user.GetWindowLongPtrW if pointer else self.user.GetWindowLongW
        self._set_window_long = self.user.SetWindowLongPtrW if pointer else self.user.SetWindowLongW
        signatures = [
            (self.user.GetAncestor, [w.HWND, w.UINT], w.HWND),
            (self.user.IsWindow, [w.HWND], w.BOOL),
            (self.user.GetWindowThreadProcessId, [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            (self._get_window_long, [w.HWND, c.c_int], c.c_ssize_t if pointer else w.LONG),
            (self._set_window_long, [w.HWND, c.c_int, c.c_ssize_t if pointer else w.LONG], c.c_ssize_t if pointer else w.LONG),
            (self.user.SetWindowPos, [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT], w.BOOL),
            (self.user.GetCapture, [], w.HWND),
            (self.user.IsChild, [w.HWND, w.HWND], w.BOOL),
            (self.user.ReleaseCapture, [], w.BOOL),
            (self.user.GetDC, [w.HWND], w.HDC),
            (self.user.ReleaseDC, [w.HWND, w.HDC], c.c_int),
            (self.gdi.CreateCompatibleDC, [w.HDC], w.HDC),
            (self.gdi.CreateDIBSection, [w.HDC, c.c_void_p, w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD], w.HANDLE),
            (self.gdi.SelectObject, [w.HDC, w.HANDLE], w.HANDLE),
            (self.gdi.DeleteObject, [w.HANDLE], w.BOOL),
            (self.gdi.DeleteDC, [w.HDC], w.BOOL),
            (self.user.UpdateLayeredWindow, [w.HWND, w.HDC, c.POINTER(w.POINT), c.POINTER(SIZE), w.HDC,
                                            c.POINTER(w.POINT), w.DWORD, c.POINTER(BLEND), w.DWORD], w.BOOL),
        ]
        for function, arguments, result in signatures:
            function.argtypes = arguments
            function.restype = result

    @property
    def mouse_passthrough(self):
        """Last verified effective state; None means it cannot be confirmed."""
        return self._effective

    def _check_thread(self):
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("mouse_passthrough_wrong_thread")
        if self._closed:
            raise RuntimeError("mouse_passthrough_closed")

    def _get_style(self, hwnd):
        c.set_last_error(0)
        style = self._get_window_long(hwnd, -20)
        error = c.get_last_error()
        if not style and error:
            raise OSError(error, "GetWindowLongPtrW failed")
        return int(style)

    def _set_style(self, hwnd, style):
        c.set_last_error(0)
        previous = self._set_window_long(hwnd, -20, style)
        error = c.get_last_error()
        if not previous and error:
            raise OSError(error, "SetWindowLongPtrW failed")
        if not self.user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037):
            raise OSError(c.get_last_error(), "SetWindowPos failed")

    def _resolve_hwnd(self):
        self._check_thread()
        if not self.window.winfo_exists():
            raise RuntimeError("mouse_passthrough_window_destroyed")
        hwnd = int(self.user.GetAncestor(self.window.winfo_id(), 2) or 0)
        pid = w.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, c.byref(pid))
        if (not hwnd or not self.user.IsWindow(hwnd) or pid.value != os.getpid()
                or thread != self._native_thread_id):
            raise RuntimeError("mouse_passthrough_window_not_owned")
        if hwnd != self._hwnd:
            style = self._get_style(hwnd)
            self._baseline_noactivate = style & self._NOACTIVATE
            self._hwnd = hwnd
            self._effective = None
            self._suspended_hwnd = 0
        return hwnd

    @staticmethod
    def _effective_style(style):
        return bool(style & LayeredWindow._LAYERED and style & LayeredWindow._TRANSPARENT)

    def _publish_input_state(self, result, *, explicit=False):
        changed = result != self._last_input_result
        self._last_input_result = result
        self._effective = result.effective
        callback = self.on_mouse_passthrough_changed
        if callback is not None and not self._closed and (explicit or changed):
            try:
                callback(result)
            except Exception:
                logging.getLogger(__name__).exception("Mouse passthrough callback failed")
        return result

    def _apply_input_style(self, enabled, *, explicit=False):
        # No Tk/Win32 access from tray/background threads or after close.
        if threading.get_ident() != self._thread_id or self._closed:
            return MousePassthroughResult(False, enabled, self._effective,
                                          "mouse_passthrough_unavailable", 0)
        if type(enabled) is not bool:
            return MousePassthroughResult(False, False, self._effective,
                                          "mouse_passthrough_requires_bool", self._hwnd)
        if self._input_busy:
            return MousePassthroughResult(False, enabled, self._effective,
                                          "mouse_passthrough_busy", self._hwnd)
        hwnd = 0
        before = None
        self._input_busy = True
        try:
            hwnd = self._resolve_hwnd()
            if not explicit and hwnd == self._suspended_hwnd:
                return self._last_input_result
            before = self._get_style(hwnd)
            desired = before | self._LAYERED
            if enabled:
                desired |= self._TRANSPARENT | self._NOACTIVATE
            else:
                desired = (desired & ~(self._TRANSPARENT | self._NOACTIVATE)) | self._baseline_noactivate
            if desired != before:
                self._set_style(hwnd, desired)
            actual = self._get_style(hwnd)
            if (actual & self._INPUT_MASK) != (desired & self._INPUT_MASK):
                raise RuntimeError("mouse_passthrough_style_readback_mismatch")
            self._requested_passthrough = enabled
            self._suspended_hwnd = 0
            result = MousePassthroughResult(True, enabled, self._effective_style(actual), "", hwnd)
        except Exception as error:
            effective = None
            # Restore only bits this transaction changed; preserve unrelated updates.
            if hwnd and before is not None:
                try:
                    current = self._get_style(hwnd)
                    restored = (current & ~self._INPUT_MASK) | (before & self._INPUT_MASK)
                    if restored != current:
                        self._set_style(hwnd, restored)
                    verified = self._get_style(hwnd)
                    if (verified & self._INPUT_MASK) != (before & self._INPUT_MASK):
                        raise RuntimeError("mouse_passthrough_restore_mismatch")
                    effective = self._effective_style(verified)
                except Exception:
                    effective = None
            self._suspended_hwnd = hwnd
            result = MousePassthroughResult(False, enabled, effective, str(error), hwnd)
        finally:
            self._input_busy = False
        return self._publish_input_state(result, explicit=explicit)

    def set_mouse_passthrough(self, enabled):
        """Apply and synchronously report the verified state on the Tk thread."""
        return self._apply_input_style(enabled, explicit=True)

    def refresh_mouse_passthrough(self):
        """Reapply to a new HWND; suspend same-HWND retries after a failure."""
        return self._apply_input_style(self._requested_passthrough)

    def release_owned_capture(self):
        """Release only capture belonging to this overlay or its child HWND."""
        hwnd = self._resolve_hwnd()
        captured = self.user.GetCapture()
        if captured and (captured == hwnd or self.user.IsChild(hwnd, captured)):
            if not self.user.ReleaseCapture() or self.user.GetCapture() == captured:
                raise OSError(c.get_last_error(), "ReleaseCapture failed")

    def close(self):
        if self._closed:
            return
        self._check_thread()
        self._closed = True
        self.on_mouse_passthrough_changed = None
        self._hwnd = 0
        self._effective = None

    def present(self, image):
        state = self.refresh_mouse_passthrough()
        hwnd = state.hwnd
        if self._closed or not hwnd:
            raise RuntimeError(state.error or "layered_window_unavailable")
        if self._resolve_hwnd() != hwnd:
            raise RuntimeError("layered_window_changed_during_input_callback")
        width, height = image.size
        rgba = np.asarray(image, dtype=np.uint16)
        # Win32 要求 RGB 先乘 Alpha，避免半透明字形出现黑边或颜色键杂色。
        rgba[:, :, :3] = (rgba[:, :, :3] * rgba[:, :, 3:4] + 127) // 255
        pixels = rgba[:, :, [2, 1, 0, 3]].astype(np.uint8).tobytes()
        screen = self.user.GetDC(None)
        dc = self.gdi.CreateCompatibleDC(screen)
        bits = c.c_void_p()
        header = BITMAPINFOHEADER(c.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        bitmap = self.gdi.CreateDIBSection(screen, c.byref(header), 0, c.byref(bits), None, 0)
        if not bitmap:
            self.gdi.DeleteDC(dc)
            self.user.ReleaseDC(None, screen)
            raise c.WinError(c.get_last_error())
        old = self.gdi.SelectObject(dc, bitmap)
        try:
            c.memmove(bits, pixels, len(pixels))
            destination = w.POINT(self.window.winfo_x(), self.window.winfo_y())
            size = SIZE(width, height)
            origin = w.POINT(0, 0)
            blend = BLEND(0, 0, 255, 1)
            if not self.user.UpdateLayeredWindow(hwnd, screen, c.byref(destination), c.byref(size), dc,
                                                  c.byref(origin), 0, c.byref(blend), 2):
                raise c.WinError(c.get_last_error())
        finally:
            self.gdi.SelectObject(dc, old)
            self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(dc)
            self.user.ReleaseDC(None, screen)
