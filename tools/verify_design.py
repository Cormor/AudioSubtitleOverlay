"""Windows design walk: owned windows, isolated settings, real pointer/key input."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--text-scale', type=float, default=1)
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    formal = Path(os.environ['APPDATA'])/'AudioSubtitleOverlay'/'settings.json'
    before = hashlib.sha256(formal.read_bytes()).hexdigest() if formal.exists() else None
    os.environ['APPDATA'] = str(out/'isolated-appdata')
    import tkinter as tk
    from tkinter import ttk, font
    from main import Application
    from model_manager import ModelManagerWindow
    from overlay import OverlayWindow
    from verify_installed_runtime import capture_visible_window
    root = tk.Tk()
    original_scaling = float(root.tk.call('tk','scaling'))
    root.tk.call('tk','scaling',original_scaling*args.text_scale)
    with patch.object(OverlayWindow,'show'):
        app = Application(root, autostart=False, tray=False)
    app.overlay.hide(); root.geometry('980x780+80+30'); root.attributes('-topmost',True)
    errors=[]; root.report_callback_exception=lambda *a: errors.append(str(a[1]))
    user = ctypes.windll.user32
    user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT];user.GetAncestor.restype=wintypes.HWND
    user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p];user.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
    previous_pointer = wintypes.POINT();user.GetPhysicalCursorPos(ctypes.byref(previous_pointer))
    result={'text_scale':args.text_scale,'tk_scaling':float(root.tk.call('tk','scaling')),'audio_started':False,'themes':{}}
    def pump(seconds=.12):
        end=time.monotonic()+seconds
        while time.monotonic()<end: root.update();time.sleep(.01)
    def snap(window,name):
        pump();capture_visible_window({'hwnd':user.GetAncestor(window.winfo_id(),2)},out/(name+'.png'))
    def point(widget,fx=.5,fy=.5):
        previous=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            box=wintypes.RECT();user.GetWindowRect(widget.winfo_id(),ctypes.byref(box))
            return round(box.left+(box.right-box.left)*fx),round(box.top+(box.bottom-box.top)*fy)
        finally:user.SetThreadDpiAwarenessContext(previous)
    def position(x,y):
        previous=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:user.SetCursorPos(x,y)
        finally:user.SetThreadDpiAwarenessContext(previous)
    def move(widget,fx=.5,fy=.5):
        position(*point(widget,fx,fy));pump()
    def click(widget,fx=.5,fy=.5):
        move(widget,fx,fy);user.mouse_event(2,0,0,0,0);pump(.04);user.mouse_event(4,0,0,0,0);pump()
    def key(code):
        user.keybd_event(code,0,0,0);user.keybd_event(code,0,2,0);pump()
    try:
        with patch.object(ModelManagerWindow,'_refresh_remote_sizes'):
            app._show_model_manager()
        manager=app._model_manager_window;manager._window.attributes('-topmost',True);manager._window.withdraw()
        for mode,label in [('dark','深色黑金'),('light','浅色白金')]:
            app.theme_label_var.set(label);app._on_theme_selected();pump()
            for index,name in enumerate(['realtime','appearance','models','advanced','help']):
                app.notebook.select(index);pump();app._scroll_pages[index].yview_moveto(0);snap(root,mode+'-'+name)
            app.notebook.select(3);canvas=app._scroll_pages[3];canvas.yview_moveto(0);pump()
            root.lift();root.focus_force();pump()
            move(canvas);user.mouse_event(0x800,0,0,ctypes.c_ulong(-360).value,0);pump()
            wheel=canvas.yview()[0]>0
            canvas.yview_moveto(0);pump()
            bar=next(w for w in canvas.master.winfo_children() if isinstance(w,ttk.Scrollbar))
            move(bar,.5,.05);user.mouse_event(2,0,0,0,0);pump(.03)
            position(*point(bar,.5,.75));pump(.06);user.mouse_event(4,0,0,0,0);pump()
            drag=canvas.yview()[0]>0
            snap(root,mode+'-advanced-scroll')
            app.notebook.select(0);canvas=app._scroll_pages[0];canvas.yview_moveto(0);pump()
            # The root already owns foreground from the scrollbar gesture.
            app.audio_combo.focus_force();pump();key(9)
            focus=root.focus_get();focus_visible=bool(focus and focus.winfo_ismapped())
            snap(root,mode+'-keyboard-focus')
            click(app.stop_button)
            disabled_no_capture=app.pipeline is None
            # Open the shared readonly popup via pointer, then dismiss via Escape.
            click(app.source_combo,.95,.5);pump();snap(root,mode+'-dropdown');key(27)
            manager._window.deiconify();manager._window.geometry('1000x780+90+30');pump()
            manager.tree.selection_set('sensevoice-int8');manager.tree.see('sensevoice-int8');pump()
            snap(manager._window,mode+'-manager')
            manager._window.geometry('760x520+90+30');pump()
            manager.resume_gpu_button.focus_force();pump()
            bottom=(manager.resume_gpu_button.winfo_rooty()+manager.resume_gpu_button.winfo_height()<=manager._scroll_canvas.winfo_rooty()+manager._scroll_canvas.winfo_height())
            snap(manager._window,mode+'-manager-small-bottom')
            manager._scroll_canvas.yview_moveto(0);snap(manager._window,mode+'-manager-small-top')
            manager._window.withdraw();root.geometry('760x520+80+30');snap(root,mode+'-small');root.geometry('980x780+80+30')
            result['themes'][mode]={'canvas_extent':str(canvas.cget('scrollregion')),'tree_background':ttk.Style(root).lookup('Treeview','fieldbackground'),'wheel_scroll':wheel,'thumb_drag':drag,'keyboard_focus_visible':focus_visible,'disabled_no_capture':disabled_no_capture,'manager_last_control_reachable':bottom,'button_height':app.start_button.winfo_height(),'font_line_height':font.Font(root=root,font=('Microsoft YaHei UI',10)).metrics('linespace')}
        result['audio_started']=app.pipeline is not None
    finally:
        position(previous_pointer.x,previous_pointer.y)
        app.close()
    result['callback_errors']=errors
    result['formal_settings_unchanged']=(hashlib.sha256(formal.read_bytes()).hexdigest() if formal.exists() else None)==before
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=True))
    if errors or result['audio_started'] or not result['formal_settings_unchanged'] or any(not r[k] for r in result['themes'].values() for k in ['wheel_scroll','thumb_drag','keyboard_focus_visible','disabled_no_capture','manager_last_control_reachable']):
        raise SystemExit('Design interaction walk did not pass')


if __name__=='__main__': main()
