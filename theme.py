"""Semantic desktop theme and reusable ttk components; see docs/design-system.md."""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk, font as tkfont
from PIL import Image, ImageDraw, ImageTk

THEME_LABELS = {"system": "跟随系统", "dark": "深色黑金", "light": "浅色白金"}


def system_theme() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
        except OSError:
            pass
    return "dark"


def reduced_motion() -> bool:
    if sys.platform == "win32":
        enabled = ctypes.c_int(1)
        try:
            if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
                return not bool(enabled.value)
        except (AttributeError, OSError):
            pass
    return False


def palette(preference: str = "system") -> dict[str, str]:
    selected = system_theme() if preference == "system" else preference
    if selected not in ("dark", "light"):
        selected = "dark"
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    tokens = json.loads((root / "design_tokens.json").read_text(encoding="utf-8"))
    return dict(tokens["color"][selected], theme=selected)


def theme_window(window, p):
    """Theme an owned settings/dialog window, without changing subtitle styling."""
    window.configure(background=p["canvas"])
    if sys.platform == "win32":
        try:
            window.update_idletasks()
            user = ctypes.windll.user32
            user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user.GetAncestor.restype = ctypes.c_void_p
            hwnd = user.GetAncestor(window.winfo_id(), 2)
            value = ctypes.c_int(p["theme"] == "dark")
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(value), 4)
        except (OSError, AttributeError, tk.TclError):
            pass


def _notebook_input_mode(root):
    """Track input before native class bindings; selected tabs share one marker."""
    if hasattr(root, "_shengmu_tab_input"):
        return
    import weakref
    notebooks = weakref.WeakSet()
    state = {"keyboard": False}
    tag = f"ShengmuTabInput{id(root)}"

    def mode(keyboard):
        state["keyboard"] = keyboard
        for notebook in list(notebooks):
            try:
                notebook.state(["user1" if keyboard else "!user1"])
            except tk.TclError:
                pass

    def attach(widget):
        try:
            tags = widget.bindtags()
            if tag not in tags:
                widget.bindtags((tag, *tags))
            if isinstance(widget, ttk.Notebook):
                notebooks.add(widget)
                widget.state(["user1" if state["keyboard"] else "!user1"])
            for child in widget.winfo_children():
                attach(child)
        except (tk.TclError, AttributeError):
            pass

    root.bind_class(tag, "<KeyPress>", lambda _event: mode(True))
    root.bind_class(tag, "<ButtonPress>", lambda _event: mode(False))
    root.bind_all("<Map>", lambda event: attach(event.widget), add="+")
    root._shengmu_tab_input = state
    attach(root)


def _elements(root, style, p):
    """Antialiased nine-slice elements retain native ttk keyboard/hit behaviour."""
    # Names are stable per palette. Retain PhotoImages for the interpreter's life.
    cache = getattr(root, "_shengmu_theme_images", {})
    key = (p["theme"], round(float(root.tk.call("tk", "scaling")), 2))
    if key in cache:
        return cache[key][0]
    prefix = f"Shengmu{len(cache)}"
    images = []

    def photo(draw_fn, width=72, height=36):
        image = Image.new("RGBA", (width * 4, height * 4))
        draw_fn(ImageDraw.Draw(image), 4, width, height)
        result = ImageTk.PhotoImage(image.resize((width, height), Image.Resampling.LANCZOS), master=root)
        images.append(result)
        return result

    def rounded(fill, border, focus=False, radius=8, tab=False, tab_focus=False):
        def draw(d, s, w, h):
            if focus:
                d.rounded_rectangle((0, 0, (w-1)*s, (h-1)*s), radius=radius*s,
                                    outline=p["focus"], width=2*s)
            d.rounded_rectangle((3*s, 3*s, (w-4)*s, (h-4)*s), radius=max(1,radius-2)*s,
                                fill=fill, outline=border, width=s)
            if tab:
                d.rounded_rectangle((3*s, (h-(8 if tab_focus else 6))*s, (w-4)*s, (h-4)*s), radius=s, fill=p["accent"])
        return photo(draw)

    def element(name, fill, border, selected=False, tab=False, inset=0):
        base = rounded(fill, border, tab=tab)
        disabled = rounded(p["disabledSurface"], p["disabledBorder"])
        hover = rounded(p["hover"] if not selected else p["accentHover"], p["controlBorder"], tab=tab)
        pressed = rounded(p["pressed"] if not selected else p["accentPressed"], p["accent"], tab=tab)
        focus = rounded(fill, border, True, tab=tab)
        focus_hover = rounded(p["hover"] if not selected else p["accentHover"], p["controlBorder"], True, tab=tab)
        style.element_create(prefix+name, "image", base, ("disabled", disabled),
                             ("focus", "pressed", rounded(p["pressed"] if not selected else p["accentPressed"], p["accent"], True)),
                             ("pressed", "!disabled", pressed), ("focus", "active", focus_hover),
                             ("focus", focus), ("active", hover), border=12, padding=inset, width=24, height=24, sticky="nsew")
        return prefix+name

    for name, fill, border, selected in (
        ("Button", p["surfaceRaised"], p["controlBorder"], False),
        ("Primary", p.get("accentFill",p["accent"]), p.get("accentFill",p["accent"]), True),
        ("Selected", p["selection"], p["accent"], False),
        ("Entry", p["surfaceRaised"], p["controlBorder"], False),
    ):
        element(name, fill, border, selected, inset=4 if name == "Entry" else 0)
    normal = rounded(p["canvas"], p["canvas"])
    selected = rounded(p["surfaceRaised"], p["surfaceRaised"], tab=True)
    tab_focus = rounded(p["surfaceRaised"], p["surfaceRaised"], tab=True, tab_focus=True)
    style.element_create(prefix+"Tab", "image", normal, ("selected", "focus", "user1", tab_focus),
                         ("selected", selected), ("focus", "user1", rounded(p["canvas"],p["canvas"],tab=True,tab_focus=True)),
                         ("active",rounded(p["hover"],p["hover"])), border=12, padding=0, width=24, height=24, sticky="nsew")

    def indicator(selected=False, disabled=False, focus=False, hover=False):
        def draw(d, s, w, h):
            if focus:
                d.rounded_rectangle((0,0,23*s,23*s),radius=6*s,outline=p["focus"],width=2*s)
            fill = (p["disabledSurface"] if disabled else
                    p.get("accentFill",p["accent"]) if selected else p["hover"] if hover else p["surfaceRaised"])
            outline = p["disabledBorder"] if disabled else p["accent"] if selected else p["controlBorder"]
            d.rounded_rectangle((4*s,4*s,19*s,19*s),radius=3*s,fill=fill,outline=outline,width=s)
            if selected:
                d.line([(7*s,11*s),(10*s,14*s),(16*s,8*s)],
                       fill=p["textDisabled"] if disabled else p.get("onAccentFill",p["onAccent"]),width=2*s,joint="curve")
        return photo(draw,24,24)
    style.element_create(prefix+"Check", "image", indicator(),
                         ("disabled","selected",indicator(True,True)), ("disabled",indicator(False,True)),
                         ("focus","selected",indicator(True,focus=True)), ("focus",indicator(focus=True)),
                         ("selected",indicator(True)), ("active",indicator(hover=True)), sticky="w", width=28)
    def chevron(color):
        return photo(lambda d,s,w,h: d.line([(5*s,5*s),(9*s,9*s),(13*s,5*s)], fill=color,width=2*s,joint="curve"),20,16)
    style.element_create(prefix+".downarrow", "image", chevron(p["text"]), ("disabled",chevron(p["textDisabled"])), sticky="e", width=24)
    def thumb(color):
        return photo(lambda d,s,w,h: d.rounded_rectangle((3*s,2*s,(w-4)*s,(h-3)*s),radius=4*s,fill=color),16,32)
    style.element_create(prefix+".thumb", "image",thumb(p["controlBorder"]),
                         ("pressed",thumb(p["accent"])),("active",thumb(p["textMuted"])),
                         border=6,padding=0,width=14,height=28,sticky="nsew")
    def horizontal_thumb(color):
        return photo(lambda d,s,w,h: d.rounded_rectangle((2*s,3*s,(w-3)*s,(h-4)*s),radius=4*s,fill=color),32,16)
    style.element_create(prefix+"H.thumb", "image",horizontal_thumb(p["controlBorder"]),
                         ("pressed",horizontal_thumb(p["accent"])),("active",horizontal_thumb(p["textMuted"])),
                         border=6,padding=0,width=28,height=14,sticky="nsew")
    # A 28px interaction parcel contains a separate 4px visual rail.
    # Do not reuse the clam trough: it paints the whole slider-height parcel.
    def rail(color):
        return photo(lambda d,s,w,h: d.rounded_rectangle(
            (12*s,12*s,(w-13)*s,16*s-1), radius=2*s, fill=color),40,28)
    style.element_create(prefix+".trough", "image",rail(p["controlBorder"]),
                         ("disabled",rail(p["disabledBorder"])),
                         border=(14,0,14,0),padding=0,width=40,height=28,sticky="we")
    def slider(color,focus=False,disabled=False):
        def draw(d,s,w,h):
            if focus:
                d.ellipse((s,s,26*s,26*s),outline=p["focus"],width=2*s)
            d.ellipse((4*s,4*s,24*s-1,24*s-1),fill=color,
                      outline=p["disabledBorder"] if disabled else p["controlBorder"],width=s)
        return photo(draw,28,28)
    style.element_create(prefix+".slider","image",slider(p["accent"]),
                         ("disabled",slider(p["textDisabled"],disabled=True)),
                         ("focus","pressed",slider(p["accentPressed"],True)),
                         ("pressed",slider(p["accentPressed"])),
                         ("focus","hover",slider(p["accentHover"],True)),
                         ("focus",slider(p["accent"],True)),
                         ("hover",slider(p["accentHover"])),
                         ("active",slider(p["accentHover"])),width=28,height=28,sticky="")
    bar=photo(lambda d,s,w,h:d.rounded_rectangle((0,0,(w-1)*s,(h-1)*s),radius=3*s,fill=p["accent"]),12,6)
    style.element_create(prefix+".pbar","image",bar,border=3,padding=0,width=6,height=6,sticky="nswe")
    cache[key] = (prefix, images)
    root._shengmu_theme_images = cache
    return prefix


def apply_theme(root, preference="system") -> dict[str, str]:
    _notebook_input_mode(root)
    p = palette(preference)
    root._shengmu_theme_preference = preference
    style = ttk.Style(root)
    if style.theme_use() != "clam":
        style.theme_use("clam")
    style.configure(".", font=("Microsoft YaHei UI", 10), background=p["surface"], foreground=p["text"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], troughcolor=p["canvas"])
    prefix = _elements(root, style, p)
    style.configure("TFrame", background=p["surface"])
    style.configure("Surface.TFrame", background=p["canvas"])
    for name, color in (("TLabel",p["text"]),("Muted.TLabel",p["textMuted"]),("Accent.TLabel",p["accent"]),
                        ("Info.TLabel",p["info"]),("Danger.TLabel",p["danger"]),("Success.TLabel",p["success"])):
        style.configure(name, background=p["surface"], foreground=color)
    style.configure("Title.TLabel", font=("Microsoft YaHei UI",18,"bold"))
    style.configure("State.TLabel", font=("Microsoft YaHei UI",12,"bold"), foreground=p["accent"])
    style.configure("TLabelframe", background=p["surface"], bordercolor=p["separator"], relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=p["surface"], foreground=p["text"], font=("Microsoft YaHei UI",10,"bold"))
    for name, element, fg in (("TButton","Button",p["text"]),
                              ("Primary.TButton","Primary",p.get("onAccentFill",p["onAccent"])),
                              ("SelectedProfile.TButton","Selected",p["text"]),
                              ("Danger.TButton","Button",p["danger"])):
        style.layout(name, [(prefix+element,{"sticky":"nsew","children":[("Button.padding",{"sticky":"nsew","children":[("Button.label",{"sticky":"nsew"})]})]})])
        style.configure(name, padding=(14,9), anchor="center", font=("Microsoft YaHei UI",10), foreground=fg, background=p["surface"])
        style.map(name, foreground=[("disabled",p["textDisabled"]),("!disabled",fg)], background=[("disabled",p["surface"]),("pressed",p["surface"]),("active",p["surface"]),("!disabled",p["surface"])])
    style.layout("TCheckbutton", [("Checkbutton.padding",{"sticky":"nsew","children":[(prefix+"Check",{"side":"left","sticky":"w"}),("Checkbutton.label",{"side":"left","sticky":"nsew"})]})])
    style.configure("TCheckbutton", padding=(0,4), foreground=p["text"])
    style.map("TCheckbutton", foreground=[("disabled",p["textDisabled"])],background=[("active",p["surface"])])
    style.configure("TNotebook", background=p["canvas"], bordercolor=p["canvas"], lightcolor=p["canvas"], darkcolor=p["canvas"], borderwidth=0, tabmargins=(0,4,0,8))
    style.layout("TNotebook.Tab", [(prefix+"Tab",{"sticky":"nsew","children":[("Notebook.padding",{"sticky":"nsew","children":[("Notebook.label",{"sticky":"nsew"})]})]})])
    style.configure("TNotebook.Tab", padding=(16,12), foreground=p["textMuted"], expand=(0,0,0,0))
    style.map("TNotebook.Tab", foreground=[("disabled",p["textDisabled"]),("selected",p["text"]),("!selected",p["textMuted"])],expand=[("selected",(0,0,0,0)),("!selected",(0,0,0,0))], padding=[("selected",(16,12)),("!selected",(16,12))], background=[("selected",p["canvas"]),("!selected",p["canvas"])])
    for name in ("TEntry","TCombobox","TSpinbox"):
        arrow = [(prefix+".downarrow",{"side":"right","sticky":""})] if name=="TCombobox" else []
        if name != "TSpinbox":
            content = arrow + [("Combobox.textarea" if arrow else "Entry.textarea",{"sticky":"nsew"})]
            # Both text and arrow stay inside the shared inset: neither can paint
            # across the rounded border or its focus ring.
            padding = "Combobox.padding" if arrow else "Entry.padding"
            style.layout(name,[(prefix+"Entry",{"sticky":"nsew","children":[
                (padding,{"sticky":"nsew","children":content})]})])
        style.configure(name, padding=(6,5), fieldbackground=p["surfaceRaised"], background=p["surface"],
                        foreground=p["text"], insertcolor=p["text"], arrowcolor=p["text"], arrowsize=14,
                        selectbackground=p["selection"],selectforeground=p["text"])
        style.map(name, fieldbackground=[("disabled",p["disabledSurface"]),("readonly",p["surfaceRaised"])],
                  foreground=[("disabled",p["textDisabled"]),("!disabled",p["text"])],
                  arrowcolor=[("disabled",p["textDisabled"]),("!disabled",p["text"])],background=[("active",p["surface"]),("!disabled",p["surface"])])
    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                    rowheight=max(34, tkfont.Font(root=root, font=("Microsoft YaHei UI",10)).metrics("linespace")+14), borderwidth=2, lightcolor=p["surface"], darkcolor=p["surface"])
    style.map("Treeview",background=[("selected",p["selection"])],foreground=[("selected",p["text"])],
              bordercolor=[("focus",p["focus"]),("!focus",p["separator"])],
              lightcolor=[("focus",p["focus"]),("!focus",p["separator"])],
              darkcolor=[("focus",p["focus"]),("!focus",p["separator"])])
    style.configure("Treeview.Heading",background=p["surfaceRaised"],foreground=p["textMuted"],
                    relief="flat",borderwidth=0,padding=(8,10),font=("Microsoft YaHei UI",10,"bold"))
    style.map("Treeview.Heading",background=[("active",p["hover"])])
    style.configure("TSeparator", background=p["separator"])
    style.configure("Horizontal.TProgressbar",background=p["accent"],troughcolor=p["canvas"],borderwidth=0)
    style.layout("Input.Horizontal.TProgressbar", [("Horizontal.Progressbar.trough", {"sticky":"nswe", "children":[(prefix+".pbar",{"side":"left","sticky":"ns"})]})])
    style.configure("Input.Horizontal.TProgressbar",background=p["accent"],troughcolor=p["canvas"],borderwidth=0,thickness=6,lightcolor=p["canvas"],darkcolor=p["canvas"],bordercolor=p["canvas"])
    style.layout("Horizontal.TScale", [(prefix+".trough", {"sticky":"we", "children":[(prefix+".slider",{"side":"left","sticky":""})]})])
    if not getattr(root, "_shengmu_scale_bindings", False):
        # ttk's stock scale drag leaves keyboard focus on the previous widget.
        # Keep mouse/keyboard ownership and the visible pressed state aligned.
        def scale_press(event):
            widget = event.widget
            if isinstance(widget, ttk.Scale) and not widget.instate(["disabled"]):
                widget.focus_set()
                widget.state(["pressed"])
        def scale_release(event):
            if isinstance(event.widget, ttk.Scale):
                event.widget.state(["!pressed"])
        root.bind_class("TScale", "<ButtonPress-1>", scale_press, add="+")
        root.bind_class("TScale", "<ButtonRelease-1>", scale_release, add="+")
        root.bind_class("TScale", "<FocusOut>", scale_release, add="+")
        root.bind_class("TScale", "<Unmap>", scale_release, add="+")
        root._shengmu_scale_bindings = True
    style.configure("TScale",background=p["surface"],troughcolor=p["controlBorder"])
    style.map("TScale",background=[("disabled",p["surface"]),("active",p["surface"]),("!disabled",p["surface"])])
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky":"ns", "children":[(prefix+".thumb",{"sticky":"nsew"})]})])
    style.layout("Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {"sticky":"we", "children":[(prefix+"H.thumb",{"sticky":"nsew"})]})])
    style.configure("TScrollbar",background=p["controlBorder"],troughcolor=p["canvas"],arrowcolor=p["text"],borderwidth=0,arrowsize=13,lightcolor=p["canvas"],darkcolor=p["canvas"],bordercolor=p["canvas"])
    style.map("TScrollbar", background=[("pressed",p["accent"]),("active",p["textMuted"])])
    root.option_add("*TCombobox.font", "{Microsoft YaHei UI} 10")
    root.option_add("*TCombobox*Listbox.font", "{Microsoft YaHei UI} 10")
    theme_window(root,p)
    for role,value in (("background",p["surfaceRaised"]),("foreground",p["text"]),("selectBackground",p["selection"]),("selectForeground",p["text"])):
        root.option_add("*TCombobox*Listbox."+role,value)
    # Readonly fields keep their native value/event contract, while the popup
    # has an explicit layout and selection/focus design of its own.
    from choice_popup import install_choice_popups
    install_choice_popups(root, p)

    def visit(widget):
        for child in widget.winfo_children():
            if isinstance(child, tk.Toplevel) and not getattr(child, "_shengmu_subtitle", False):
                if not child.overrideredirect():
                    theme_window(child, p)
            visit(child)
    visit(root)
    return p
