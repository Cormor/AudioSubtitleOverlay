"""Readable popups for readonly choices and opted-in numeric editors.

The combobox remains the value owner.  This module replaces only its popdown,
so textvariables, current(), postcommand and ComboboxSelected keep their meaning.
Layout helpers do not require a Tk display and can be checked independently.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import ctypes
import sys
import time
import tkinter as tk
from tkinter import font as tkfont, ttk
from typing import Callable, Sequence


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class PopupPlacement(Rect):
    above: bool


@dataclass(frozen=True)
class ChoiceRow:
    top: int
    bottom: int
    lines: tuple[str, ...]


def place_popup(anchor: Rect, width: int, height: int, work: Rect,
                gap: int = 4) -> PopupPlacement:
    """Keep the complete popup in a work area, including negative monitor origins."""
    work_width, work_height = max(1, work.width), max(1, work.height)
    width = min(max(1, width), work_width)
    height = min(max(1, height), work_height)
    below_y = anchor.y + anchor.height + gap
    below = max(0, work.y + work_height - max(work.y, below_y))
    above = max(0, min(work.y + work_height, anchor.y - gap) - work.y)
    opens_above = height > below and above > below
    available = above if opens_above else below
    if available:
        height = min(height, available)
    y = anchor.y - gap - height if opens_above else below_y
    return PopupPlacement(
        min(max(anchor.x, work.x), work.x + work_width - width),
        min(max(y, work.y), work.y + work_height - height),
        width, height, opens_above,
    )


def wrap_text(text: str, width: int, measure: Callable[[str], int]) -> tuple[str, ...]:
    """Wrap both CJK and long unbroken device names without dropping content."""
    width = max(1, width)
    lines = []
    for paragraph in str(text).split("\n"):
        if not paragraph:
            lines.append("")
        while paragraph:
            if measure(paragraph) <= width:
                lines.append(paragraph)
                break
            lo, hi = 1, len(paragraph)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if measure(paragraph[:mid]) <= width:
                    lo = mid
                else:
                    hi = mid - 1
            cut = lo
            # Prefer word boundaries when they do not waste most of the line.
            boundary = paragraph.rfind(" ", 0, cut + 1)
            if boundary >= max(1, cut // 2):
                cut = boundary + 1
            lines.append(paragraph[:cut])
            paragraph = paragraph[cut:]
    return tuple(lines)


def layout_rows(values: Sequence[str], text_width: int, linespace: int,
                measure: Callable[[str], int]) -> tuple[ChoiceRow, ...]:
    top = 0
    rows = []
    for value in values:
        lines = wrap_text(value, text_width, measure)
        height = max(36, max(1, linespace) * len(lines) + 16)
        rows.append(ChoiceRow(top, top + height, lines))
        top += height
    return tuple(rows)


def row_at(rows: Sequence[ChoiceRow], y: float) -> int | None:
    if not rows or y < 0 or y >= rows[-1].bottom:
        return None
    return bisect_right([row.bottom for row in rows], y)


def navigation_index(index: int, count: int, key: str,
                     rows: Sequence[ChoiceRow] = (), viewport: int = 0) -> int:
    if count <= 0:
        return -1
    index = min(max(index, 0), count - 1)
    if key == "Home":
        return 0
    if key == "End":
        return count - 1
    if key in ("Prior", "Next") and rows:
        direction = -1 if key == "Prior" else 1
        target = rows[index].top + direction * max(1, viewport)
        found = row_at(rows, target)
        found = (0 if target < 0 else count - 1) if found is None else found
        # A very tall row still allows PageDown/Up to reach its neighbour.
        return min(count - 1, max(0, found if found != index else index + direction))
    return min(count - 1, max(0, index + (-1 if key == "Up" else 1)))


def visible_offset(row: ChoiceRow, offset: float, viewport: int,
                   total: int) -> float:
    """Reveal an active row; oversized rows start at their first readable line."""
    if row.top < offset or row.bottom - row.top > viewport:
        offset = row.top
    elif row.bottom > offset + viewport:
        offset = row.bottom - viewport
    return max(0.0, min(offset, max(0, total - viewport)))


def _work_area(owner) -> Rect:
    """Map the native monitor work rectangle into this widget's Tk coordinates.

    On Windows all native calls use one temporary DPI context.  Their result is
    mapped using the actual HWND rectangle, rather than mixing physical pixels
    with Tk's possibly virtualized root coordinates.
    """
    anchor = Rect(owner.winfo_rootx(), owner.winfo_rooty(),
                  owner.winfo_width(), owner.winfo_height())
    if sys.platform == "win32":
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user = ctypes.windll.user32
        previous_context = None
        hwnd = owner.winfo_id()
        try:
            set_context = getattr(user, "SetThreadDpiAwarenessContext", None)
            if set_context is not None:
                set_context.argtypes = [ctypes.c_void_p]
                set_context.restype = ctypes.c_void_p
                previous_context = set_context(ctypes.c_void_p(-4))
            user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user.MonitorFromWindow.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            native = wintypes.RECT()
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            monitor = user.MonitorFromWindow(hwnd, 2)
            if (user.GetWindowRect(hwnd, ctypes.byref(native))
                    and user.GetMonitorInfoW(monitor, ctypes.byref(info))):
                sx = anchor.width / max(1, native.right - native.left)
                sy = anchor.height / max(1, native.bottom - native.top)
                return Rect(round(anchor.x + (info.rcWork.left - native.left) * sx),
                            round(anchor.y + (info.rcWork.top - native.top) * sy),
                            round((info.rcWork.right - info.rcWork.left) * sx),
                            round((info.rcWork.bottom - info.rcWork.top) * sy))
        except (AttributeError, OSError, tk.TclError):
            pass
        finally:
            if previous_context:
                set_context(previous_context)
    return Rect(owner.winfo_vrootx(), owner.winfo_vrooty(),
                owner.winfo_vrootwidth(), owner.winfo_vrootheight())


def _rounded(canvas, left, top, right, bottom, radius=8, **options):
    radius = min(radius, (right - left) / 2, (bottom - top) / 2)
    return canvas.create_polygon(
        left + radius, top, right - radius, top, right, top,
        right, top + radius, right, bottom - radius, right, bottom,
        right - radius, bottom, left + radius, bottom, left, bottom,
        left, bottom - radius, left, top + radius, left, top,
        smooth=True, splinesteps=24, **options,
    )


def _round_native_window(window, radius=8):
    """Clip the opaque Win32 window; a colour-key leaves scaling seams."""
    if sys.platform != "win32":
        return False
    from ctypes import wintypes
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    previous = None
    region = None
    try:
        user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi.CreateRoundRectRgn.restype = wintypes.HANDLE
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        hwnd = user.GetAncestor(window.winfo_id(), 2)
        box = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(box)):
            return False
        width, height = box.right - box.left, box.bottom - box.top
        rx = round(2 * radius * width / max(1, window.winfo_width()))
        ry = round(2 * radius * height / max(1, window.winfo_height()))
        region = gdi.CreateRoundRectRgn(0, 0, width + 1, height + 1, rx, ry)
        if not region or not user.SetWindowRgn(hwnd, region, True):
            return False
        region = None  # A successful SetWindowRgn transfers ownership to Windows.
        return True
    except (AttributeError, OSError, tk.TclError):
        return False
    finally:
        if region:
            gdi.DeleteObject(region)
        if previous:
            user.SetThreadDpiAwarenessContext(previous)


class ChoicePopup:
    """One ephemeral popdown. rows are ChoiceRow(top, bottom, lines), in canvas units."""

    def __init__(self, manager, owner, values, keyboard):
        self.manager, self.owner, self.values = manager, owner, tuple(values)
        self._parameter = bool(vars(owner).get("_shengmu_parameter", False))
        read_current = vars(owner).get("_shengmu_choice_value")
        current = read_current() if callable(read_current) else None
        self.selected_index = (self.values.index(current) if current in self.values else -1) if callable(read_current) else owner.current()
        read_header = vars(owner).get("_shengmu_choice_header")
        self._header_text = read_header() if callable(read_header) else ""
        self.active_index = max(0, self.selected_index) if values else -1
        self.keyboard = keyboard
        self.hover_index = None
        self.closed = False
        self._bindings = []
        self._after_ids = set()
        self._prefix, self._prefix_time = "", 0.0
        self._previous_grab = owner.grab_current()
        self._previous_grab_status = (
            self._previous_grab.grab_status() if self._previous_grab else None)
        self.window = None
        try:
            self._build()
        except Exception:
            # __init__ has not returned to manager.post yet. Own every partial
            # resource here so its caller never needs a half-created instance.
            self.close(restore_focus=False)
            raise

    def _build(self):
        manager, owner, values = self.manager, self.owner, self.values
        self._font = tkfont.Font(root=owner, font=owner.cget("font") or
                                ttk.Style(owner).lookup(owner.cget("style") or "TCombobox", "font")
                                or "TkTextFont")
        self.row_height = max(36, self._font.metrics("linespace") + 16)
        work = _work_area(owner)
        anchor = Rect(owner.winfo_rootx(), owner.winfo_rooty(),
                      owner.winfo_width(), owner.winfo_height())
        # Up to 560 Tk pixels keeps long labels readable without turning a short
        # language selector into a full-width menu. Narrow screens wrap further.
        natural = max((self._font.measure(value) for value in values), default=0) + (116 if self._parameter else 68)
        if self._header_text:
            natural = max(natural, min(self._font.measure(self._header_text) + 24, 560))
        width = min(work.width, max(anchor.width, min(natural, 560)))
        if self._header_text and self._font.measure(self._header_text) > width - 24:
            self._header_text = "当前值：自定义（完整值见输入框）"
        self._header_lines = wrap_text(self._header_text, max(1, width-24), self._font.measure) if self._header_text else ()
        self._header_height = (len(self._header_lines) * self._font.metrics("linespace") + 12) if self._header_lines else 0
        self.rows = layout_rows(values, max(1, width - (128 if self._parameter else 80)),
                                self._font.metrics("linespace"), self._font.measure)
        self.total_height = self.rows[-1].bottom if self.rows else 0
        desired_height = min(self.total_height, self.row_height * 8) + 12 + self._header_height
        self.placement = place_popup(anchor, width, desired_height, work)
        self.window = tk.Toplevel(owner, takefocus=False)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.transient(owner.winfo_toplevel())
        if sys.platform == "win32":
            # Match Tk's native ComboboxPopdown: an override-redirect window
            # otherwise stays underneath a topmost settings window.
            self.window.attributes("-topmost", True)
        self.window.configure(background=manager.palette["surfaceRaised"])
        self.window.geometry(
            f"{self.placement.width}x{self.placement.height}"
            # A leading '+' requests an absolute origin even when the value
            # itself is negative. '-120' alone means distance from screen right.
            f"+{self.placement.x}+{self.placement.y}")
        self._shell = tk.Canvas(self.window, highlightthickness=0, bd=0,
                                background=manager.palette["surfaceRaised"])
        self._shell.pack(fill="both", expand=True)
        _rounded(self._shell, 1, 1, self.placement.width - 2,
                 self.placement.height - 2, radius=8,
                 fill=manager.palette["surfaceRaised"],
                 outline=manager.palette.get("controlBorder", manager.palette["border"]), width=1)
        if self._header_lines:
            self._shell.create_text(12, 6, text="\n".join(self._header_lines), font=self._font,
                                    fill=manager.palette["accent"], anchor="nw", justify="left")
        self.viewport = max(1, self.placement.height - 12 - self._header_height)
        scrolling = self.total_height > self.viewport
        self._canvas_width = max(1, self.placement.width - 12 - (16 if scrolling else 0))
        self.canvas = tk.Canvas(self.window, highlightthickness=0, bd=0,
                                background=manager.palette["surfaceRaised"], takefocus=True,
                                yscrollincrement=1)
        self.canvas.place(x=6, y=6+self._header_height, width=self._canvas_width, height=self.viewport)
        self.canvas.configure(scrollregion=(0, 0, self._canvas_width, self.total_height))
        self.scrollbar = None
        if scrolling:
            self.scrollbar = ttk.Scrollbar(self.window, orient="vertical", command=self.canvas.yview,
                                           takefocus=False)
            self.scrollbar.place(x=self.placement.width - 20, y=6+self._header_height, width=14, height=self.viewport)
            self.canvas.configure(yscrollcommand=self.scrollbar.set)
        # Tk on Windows emits Enter without Motion on the first crossing.
        # Update the entered row immediately, including a warped pointer.
        self.canvas.bind("<Enter>", self._motion)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", self._leave)
        self.canvas.bind("<ButtonPress-1>", self._click)
        self.window.bind("<ButtonPress-1>", self._outside_click, add="+")
        self.window.bind("<KeyPress>", self._key)
        self.window.bind("<MouseWheel>", self._wheel)
        self.window.bind("<Button-4>", self._wheel)
        self.window.bind("<Button-5>", self._wheel)
        self.window.bind("<FocusOut>", self._focus_out, add="+")
        self.window.bind("<Destroy>", self._window_destroyed, add="+")
        self._bind(owner, "<Destroy>", self._owner_gone)
        self._bind(owner, "<Unmap>", self._owner_gone)
        self._bind(owner.winfo_toplevel(), "<Configure>", self._owner_configured)
        self._bind(owner.winfo_toplevel(), "<Unmap>", self._owner_unmapped)
        self._owner_geometry = (anchor.x, anchor.y, anchor.width, anchor.height)
        top = owner.winfo_toplevel()
        self._toplevel_geometry = (top.winfo_rootx(), top.winfo_rooty(),
                                   top.winfo_width(), top.winfo_height())
        self._draw()

    def _bind(self, widget, sequence, callback):
        self._bindings.append((widget, sequence, widget.bind(sequence, callback, add="+")))

    def _idle(self, callback):
        token = None

        def run():
            self._after_ids.discard(token)
            if not self.closed and self.manager.popup is self:
                callback()

        token = self.window.after_idle(run)
        self._after_ids.add(token)

    def show(self):
        self.owner.state(["pressed"])
        self.window.deiconify()
        self.window.lift()
        self.window.update_idletasks()
        if self.closed:
            return
        self.native_rounding = _round_native_window(self.window)
        self.window.grab_set()
        # Posting follows an explicit field gesture; the temporary popup must
        # receive native keyboard focus, including after a prior popup closes.
        self.canvas.focus_force()
        self._reveal()

    def _draw(self):
        if self.closed:
            return
        self.canvas.delete("all")
        p = self.manager.palette
        for index, row in enumerate(self.rows):
            selected = index == self.selected_index
            fill = p["selection"] if selected else p["hover"] if index == self.hover_index else p["surfaceRaised"]
            _rounded(self.canvas, 0, row.top, self._canvas_width, row.bottom, radius=4,
                     fill=fill, outline="")
            if selected:
                self.canvas.create_rectangle(0, row.top + 6, 3, row.bottom - 6,
                                             fill=p["accent"], outline="")
                cy = (row.top + row.bottom) / 2
                self.canvas.create_line(13, cy, 17, cy + 4, 24, cy - 4,
                                        fill=p["accent"], width=2, capstyle="round", joinstyle="round")
            self.canvas.create_text(38, (row.top + row.bottom) / 2,
                                    text="\n".join(row.lines), font=self._font,
                                    fill=p["text"], anchor="w", justify="left")
            if self._parameter:
                self.canvas.create_text(self._canvas_width - 10, (row.top + row.bottom) / 2,
                                        text="推荐", font=self._font, fill=p["textMuted"], anchor="e")
            if self.keyboard and index == self.active_index:
                _rounded(self.canvas, 2, row.top + 2, self._canvas_width - 2,
                         row.bottom - 2, radius=4, fill="", outline=p["focus"], width=2)

    def _reveal(self):
        if self.active_index < 0:
            return
        offset = visible_offset(self.rows[self.active_index], self.canvas.canvasy(0),
                                self.viewport, self.total_height)
        self.canvas.yview_moveto(offset / max(1, self.total_height))

    def _motion(self, event):
        index = row_at(self.rows, self.canvas.canvasy(event.y))
        if index != self.hover_index:
            self.hover_index = index
            self._draw()

    def _leave(self, _event):
        self.hover_index = None
        self._draw()

    def _click(self, event):
        if not (0 <= event.x < self._canvas_width and 0 <= event.y < self.viewport):
            self.manager.dismiss(restore_focus=False)
            return "break"
        index = row_at(self.rows, self.canvas.canvasy(event.y))
        if index is not None:
            self.manager.commit(self, index)
        return "break"

    def _outside_click(self, event):
        if not (self.window.winfo_rootx() <= event.x_root < self.window.winfo_rootx() + self.placement.width
                and self.window.winfo_rooty() <= event.y_root < self.window.winfo_rooty() + self.placement.height):
            self.manager.dismiss(restore_focus=False)
            return "break"

    def _wheel(self, event):
        if getattr(event, "num", None) in (4, 5):
            amount = -self.row_height if event.num == 4 else self.row_height
        else:
            amount = -int(event.delta / 120) * self.row_height
            if event.delta and not amount:
                amount = -self.row_height if event.delta > 0 else self.row_height
        self.canvas.yview_scroll(amount, "units")
        return "break"

    def _key(self, event):
        key = event.keysym
        if key in ("Escape", "F4") or (key == "Up" and event.state & 0x0008):
            self.manager.dismiss()
        elif key in ("Return", "KP_Enter", "space"):
            self.manager.commit(self, self.active_index)
        elif key in ("Tab", "ISO_Left_Tab"):
            backwards = key == "ISO_Left_Tab" or event.state & 1
            try:
                target = self.owner.tk_focusPrev() if backwards else self.owner.tk_focusNext()
            except tk.TclError:
                target = None
            self.manager.dismiss(restore_focus=False)
            if target is not None:
                try:
                    target.focus_set()
                except tk.TclError:
                    pass
        elif key in ("Up", "Down", "Home", "End", "Prior", "Next"):
            self.keyboard = True
            self.active_index = navigation_index(self.active_index, len(self.values), key,
                                                 self.rows, self.viewport)
            self._reveal()
            self._draw()
        elif event.char and event.char.isprintable() and not (event.state & 0x000C):
            now = time.monotonic()
            prefix = (self._prefix if now - self._prefix_time < 1 else "") + event.char.casefold()
            repeated = len(set(prefix)) == 1
            query = prefix[0] if repeated else prefix
            start = self.active_index + 1 if repeated else self.active_index
            for step in range(len(self.values)):
                index = (start + step) % len(self.values)
                if self.values[index].casefold().startswith(query):
                    self.active_index = index
                    self.keyboard = True
                    self._reveal()
                    self._draw()
                    break
            self._prefix, self._prefix_time = prefix, now
        return "break"

    def _focus_out(self, _event):
        self._idle(self._check_focus)

    def _check_focus(self):
        try:
            focus = self.window.focus_get()
            if focus is None or not str(focus).startswith(str(self.window) + "."):
                self.manager.dismiss(restore_focus=False)
        except (tk.TclError, KeyError):
            self.manager.dismiss(restore_focus=False)

    def _owner_gone(self, event):
        if event.widget is self.owner:
            self.manager.dismiss(restore_focus=False)

    def _owner_unmapped(self, event):
        if event.widget is self.owner.winfo_toplevel():
            self.manager.dismiss(restore_focus=False)

    def _owner_configured(self, _event):
        # Toplevel bindings also receive descendant Configure events. Checking
        # the actual anchor handles scrolling without dismissing for our popup.
        try:
            geometry = (self.owner.winfo_rootx(), self.owner.winfo_rooty(),
                        self.owner.winfo_width(), self.owner.winfo_height())
            top = self.owner.winfo_toplevel()
            top_geometry = (top.winfo_rootx(), top.winfo_rooty(),
                            top.winfo_width(), top.winfo_height())
            if geometry != self._owner_geometry or top_geometry != self._toplevel_geometry:
                self.manager.dismiss(restore_focus=False)
        except tk.TclError:
            self.manager.dismiss(restore_focus=False)

    def _window_destroyed(self, event):
        if event.widget is self.window and not self.closed:
            self.manager.dismiss(restore_focus=False)

    def close(self, restore_focus):
        if self.closed:
            return
        self.closed = True
        for token in self._after_ids:
            try:
                self.window.after_cancel(token)
            except tk.TclError:
                pass
        self._after_ids.clear()
        for widget, sequence, binding in self._bindings:
            try:
                widget.unbind(sequence, binding)
            except tk.TclError:
                pass
        self._bindings.clear()
        try:
            self.owner.state(["!pressed"])
        except tk.TclError:
            pass
        if self.window is None:
            return
        try:
            focused = self.manager.root.focus_displayof()
        except (tk.TclError, KeyError):
            focused = None
        owns_focus = focused is self.window or (focused is not None and
            str(focused).startswith(str(self.window) + "."))
        restore_focus = restore_focus and owns_focus
        unknown_grab = object()
        try:
            current = self.manager.root.grab_current()
        except (tk.TclError, KeyError):
            current = unknown_grab
        try:
            # The <Destroy> path may run after our own pathname is invalid.
            ours = current is self.window
            restore_focus = restore_focus and (
                ours or current is None or current is self._previous_grab)
            if ours or current is unknown_grab:
                # Tcl releases a grab only if this pathname owns it. An unknown
                # native dialog therefore cannot have its grab removed here.
                self.window.grab_release()
            # A callback may have opened another modal since this popup. Do not
            # replace its grab, including when our Toplevel was destroyed first.
            if (ours or current is None) and self._previous_grab is not None:
                if self._previous_grab.winfo_exists() and self._previous_grab.winfo_ismapped():
                    if self._previous_grab_status == "global":
                        self._previous_grab.grab_set_global()
                    else:
                        self._previous_grab.grab_set()
        except tk.TclError:
            pass
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        if restore_focus:
            try:
                if self.owner.winfo_exists() and self.owner.winfo_ismapped():
                    self.owner.focus_force()
            except tk.TclError:
                pass


class ChoicePopupManager:
    def __init__(self, root, palette):
        self.root = root
        self.palette = dict(palette)
        self.popup = None
        self._posting = False
        self._tag = "ShengmuReadonlyChoices"
        root.bind_class(self._tag, "<ButtonPress-1>", self._press)
        root.bind_class(self._tag, "<KeyPress>", self._key)
        # Other mapped controls do not need popup setup. Keep this on the
        # native class so page switches avoid one Python callback per widget.
        root.bind_class("TCombobox", "<Map>", self._mapped, add="+")
        self._visit(root)

    def _visit(self, widget):
        if isinstance(widget, ttk.Combobox):
            tags = widget.bindtags()
            if self._tag not in tags:
                widget.bindtags((self._tag,) + tags)
        for child in widget.winfo_children():
            self._visit(child)

    def _mapped(self, event):
        if isinstance(event.widget, ttk.Combobox):
            self._visit(event.widget)

    def update_palette(self, palette):
        self.dismiss()
        self.palette = dict(palette)

    @staticmethod
    def _readonly(combo):
        return isinstance(combo, ttk.Combobox) and combo.instate(["readonly"])

    @staticmethod
    def _editable(combo):
        return isinstance(combo, ttk.Combobox) and bool(vars(combo).get("_shengmu_parameter", False))

    def _managed(self, combo):
        return self._readonly(combo) or self._editable(combo)

    def _press(self, event):
        if not self._managed(event.widget):
            return None
        if event.widget.instate(["disabled"]):
            return "break"
        if self._editable(event.widget):
            # Existing24px arrow +6px padding +4px inset, full field height.
            arrow = event.x >= event.widget.winfo_width() - 34
            if not arrow:
                element = str(event.widget.identify(event.x, event.y))
                if element.endswith("textarea"):
                    return None
                event.widget.focus_set()
                return "break"
        if self.popup is not None and self.popup.owner is event.widget:
            self.dismiss()
        else:
            self.post(event.widget)
        return "break"

    def _key(self, event):
        if not self._managed(event.widget):
            return None
        if event.widget.instate(["disabled"]):
            return "break"
        keys = ("F4", "Down", "Up") if self._editable(event.widget) else ("F4", "Down", "Up", "space", "Return", "KP_Enter")
        if event.keysym in keys:
            self.post(event.widget, keyboard=True)
            return "break"
        return None

    def post(self, combo, keyboard=False):
        if self._posting or not self._managed(combo) or combo.instate(["disabled"]):
            return None
        self.dismiss(restore_focus=False)
        self._posting = True
        popup = None
        try:
            postcommand = combo.cget("postcommand")
            if postcommand:
                combo.tk.call("uplevel", "#0", postcommand)
            # postcommand may rebuild the view or disable the original field.
            if not combo.winfo_exists() or not combo.winfo_ismapped() or combo.instate(["disabled"]):
                return None
            raw_values = combo.cget("values")
            values = tuple(str(value) for value in (
                combo.tk.splitlist(raw_values) if isinstance(raw_values, str) else raw_values))
            if not values:
                return None
            combo.focus_set()
            popup = ChoicePopup(self, combo, values, keyboard)
            self.popup = popup
            popup.show()
            return popup
        except (tk.TclError, KeyError):
            if popup is not None:
                self.dismiss(restore_focus=False)
            else:
                # Report a genuine postcommand failure like a native binding.
                exc_type, exc, tb = sys.exc_info()
                self.root.report_callback_exception(exc_type, exc, tb)
            return None
        finally:
            self._posting = False

    def dismiss(self, restore_focus=True):
        popup, self.popup = self.popup, None
        if popup is not None:
            popup.close(restore_focus)
            closed = vars(popup.owner).get("_shengmu_choice_closed")
            if callable(closed):
                closed()

    def commit(self, popup, index):
        if self.popup is not popup or not 0 <= index < len(popup.values):
            return
        owner = popup.owner
        value = popup.values[index]
        self.dismiss()
        try:
            if not owner.winfo_exists() or owner.instate(["disabled"]):
                return
            raw_values = owner.cget("values")
            current_values = tuple(str(item) for item in (
                owner.tk.splitlist(raw_values) if isinstance(raw_values, str) else raw_values))
            # A device refresh during the popup must never select a different
            # item merely because it inherited the old index.
            if index >= len(current_values) or current_values[index] != value:
                return
            owner.current(index)
            owner.event_generate("<<ComboboxSelected>>", when="tail")
        except tk.TclError:
            pass


def install_choice_popups(root, palette) -> ChoicePopupManager:
    manager = getattr(root, "_shengmu_choices", None)
    if manager is None:
        manager = ChoicePopupManager(root, palette)
        root._shengmu_choices = manager
    else:
        manager.update_palette(palette)
    return manager
