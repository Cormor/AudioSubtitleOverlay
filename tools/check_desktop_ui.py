"""Isolated Windows desktop check; no audio capture or user configuration writes."""

import ctypes
import argparse
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import tkinter as tk
    from PIL import ImageGrab
    from main import Application
    from overlay import OverlayWindow

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "verification_desktop")
    evidence = parser.parse_args().output
    evidence.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shengmu-ui-", dir=evidence) as temporary:
        os.environ["APPDATA"] = temporary
        root = tk.Tk()
        root.withdraw()
        with patch.object(OverlayWindow, "show"):
            app = Application(root, autostart=False)
        results = {}
        try:
            app._show_settings()
            root.geometry("+80+80")

            def pump(seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)

            pump(.5)
            results["no_capture"] = app.pipeline is None
            results["tray_ready"] = app._tray.available
            class IconId(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                            ("uID", wintypes.UINT), ("guid", ctypes.c_byte * 16)]
            item = IconId(ctypes.sizeof(IconId), app._tray._icon._hwnd, 0)
            rectangle = wintypes.RECT()
            get_rect = ctypes.windll.shell32.Shell_NotifyIconGetRect
            get_rect.argtypes = [ctypes.POINTER(IconId), ctypes.POINTER(wintypes.RECT)]
            get_rect.restype = ctypes.c_long
            results["shell_icon_registered"] = get_rect(ctypes.byref(item), ctypes.byref(rectangle)) == 0
            # DWM reports physical pixels even on a DPI-virtualized desktop.
            user = ctypes.windll.user32
            user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user.GetAncestor.restype = wintypes.HWND
            frame = user.GetAncestor(root.winfo_id(), 2)
            bounds = wintypes.RECT()
            hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(frame, 9, ctypes.byref(bounds), ctypes.sizeof(bounds))
            if hr != 0:
                raise RuntimeError("Could not read physical window bounds")
            box = (bounds.left,bounds.top,bounds.right,bounds.bottom)
            ImageGrab.grab(window=frame).save(evidence / "recognition.png")
            notebook = next(w for w in root.winfo_children()[0].winfo_children() if w.winfo_class()=="TNotebook")
            results["five_task_pages"] = len(notebook.tabs()) == 5
            for index, title in [(1,"appearance"),(2,"models"),(3,"advanced"),(4,"help")]:
                notebook.select(index)
                pump(.15)
                ImageGrab.grab(window=frame).save(evidence / f"{title}.png")
            notebook.select(0)
            app.theme_label_var.set("深色黑金")
            app._on_theme_selected()
            pump(.2)
            results["dark_theme_applied"] = app._palette["theme"] == "dark"
            ImageGrab.grab(window=frame).save(evidence / "dark.png")
            app.theme_label_var.set("浅色白金")
            app._on_theme_selected()
            pump(.2)
            results["light_theme_applied"] = app._palette["theme"] == "light"
            ImageGrab.grab(window=frame).save(evidence / "light.png")
            root.iconify()
            pump(.3)
            results["minimize_to_tray"] = root.state() == "withdrawn"
            # Exercise the actual default menu action and Tk message drain.
            app._tray._icon.menu.items[0](app._tray._icon)
            pump(.3)
            results["restore_from_tray_menu"] = root.state() == "normal"
            app._tray.close()
            app._hide_settings()
            pump(.1)
            results["missing_tray_keeps_taskbar"] = root.state() == "iconic"
        finally:
            app.close()
        results["closed"] = not app._tray.available
        (evidence / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(results))
        if not all(results.values()):
            raise SystemExit("Desktop check failed")


if __name__ == "__main__":
    main()
