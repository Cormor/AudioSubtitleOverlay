"""Windows 分层窗口逐像素透明合成。"""

import ctypes as c
from ctypes import wintypes as w

import numpy as np


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

    def __init__(self, window):
        self.window = window
        self.user = c.WinDLL("user32", use_last_error=True)
        self.gdi = c.WinDLL("gdi32", use_last_error=True)
        signatures = [
            (self.user.GetParent, [w.HWND], w.HWND),
            (self.user.GetWindowLongPtrW, [w.HWND, c.c_int], c.c_ssize_t),
            (self.user.SetWindowLongPtrW, [w.HWND, c.c_int, c.c_ssize_t], c.c_ssize_t),
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

    def present(self, image):
        hwnd = self.user.GetParent(self.window.winfo_id())
        style = self.user.GetWindowLongPtrW(hwnd, -20)
        if not style & 0x80000:
            self.user.SetWindowLongPtrW(hwnd, -20, style | 0x80000)
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
