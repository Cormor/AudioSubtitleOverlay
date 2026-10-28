"""CG-01/02/03: measurable geometry plus real Windows input, isolated APPDATA.

Visual judgments still compare the saved actual-window images with
 docs/control-review-2.0.2.md; numeric checks do not replace that review.
"""
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--text-scale',type=float,default=1)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    formal=Path(os.environ['APPDATA'])/'AudioSubtitleOverlay'/'settings.json'
    formal_hash=hashlib.sha256(formal.read_bytes()).hexdigest()
    os.environ['APPDATA']=str(out/'isolated-appdata')
    import tkinter as tk
    from tkinter import ttk
    from PIL import ImageGrab
    from main import Application
    from overlay import OverlayWindow
    from theme import palette
    from verify_installed_runtime import capture_visible_window
    r=tk.Tk();r.tk.call('tk','scaling',float(r.tk.call('tk','scaling'))*args.text_scale)
    with patch.object(OverlayWindow,'show'):
        app=Application(r,autostart=False,tray=False)
    app.overlay.hide();r.geometry('980x780+100+40');r.attributes('-topmost',True)
    errors=[];r.report_callback_exception=lambda *a:errors.append(str(a[1]))
    u=ctypes.windll.user32
    u.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT];u.GetAncestor.restype=wintypes.HWND
    u.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    u.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p];u.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
    old_pointer=wintypes.POINT();u.GetPhysicalCursorPos(ctypes.byref(old_pointer))
    def pump(t=.1):
        end=time.monotonic()+t
        while time.monotonic()<end:r.update();time.sleep(.01)
    def position(w,x=None,y=None):
        old=u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            b=wintypes.RECT();u.GetWindowRect(w.winfo_id(),ctypes.byref(b))
            x=w.winfo_width()/2 if x is None else x;y=w.winfo_height()/2 if y is None else y
            u.SetCursorPos(round(b.left+x*(b.right-b.left)/w.winfo_width()),round(b.top+y*(b.bottom-b.top)/w.winfo_height()))
        finally:u.SetThreadDpiAwarenessContext(old)
        pump()
    def key(v):u.keybd_event(v,0,0,0);u.keybd_event(v,0,2,0);pump()
    def snap(name):
        pump();capture_visible_window({'hwnd':u.GetAncestor(r.winfo_id(),2)},out/(name+'.png'))
    def crop(w):
        image=ImageGrab.grab(window=u.GetAncestor(r.winfo_id(),2))
        if image.size!=(r.winfo_width(),r.winfo_height()):raise RuntimeError('Unexpected native screenshot scale')
        x=w.winfo_rootx()-r.winfo_rootx();y=w.winfo_rooty()-r.winfo_rooty()
        return image.crop((x,y,x+w.winfo_width(),y+w.winfo_height())).convert('RGB')
    def rgb(value):return tuple(bytes.fromhex(value[1:]))
    def near(a,b,tolerance=16):return max(abs(x-y) for x,y in zip(a,b))<=tolerance
    def walk(w):
        for c in w.winfo_children():yield c;yield from walk(c)
    results={'text_scale':args.text_scale,'themes':{}}
    try:
        for mode,label in [('dark','深色黑金'),('light','浅色白金')]:
            app.theme_label_var.set(label);app._on_theme_selected();p=palette(mode);pump()
            app.notebook.select(1);pump()
            scales=[app.font_scale,app.translation_font_scale,app.background_opacity_scale,app.text_opacity_scale]
            records=[]
            for index,w in enumerate(scales):
                w.focus_force();pump();lo=float(w.cget('from'));hi=float(w.cget('to'))
                w.set((lo+hi)/2);r.focus_force();position(r,10,10);pump()
                image=crop(w);width,height=image.size
                rail_rows=[y for y in range(height) if near(image.getpixel((width//4,y)),rgb(p['controlBorder']))]
                center=w.tk.call(w._w,'coords',float(w.get()));cx=round(float(center[0]))
                # Fill bounds plus the visible 1px perimeter measure the circle;
                # excluding rail rows would underestimate its horizontal diameter.
                filled=[(x,y) for y in range(height) for x in range(max(0,cx-14),min(width,cx+14)) if near(image.getpixel((x,y)),rgb(p['accent']),12)]
                diameter=max(x for x,y in filled)-min(x for x,y in filled)+3 if filled else 0
                extent=max(y for x,y in filled)-min(y for x,y in filled)+3 if filled else 0
                geometry_ok=height==28 and 3<=len(rail_rows)<=5 and 19<=diameter<=21 and 19<=extent<=21
                snap(f'{mode}-scale-{index}-normal')
                # Native pointer drag, then keyboard; get exact endpoint values.
                w.focus_force();pump();position(w,cx,14);u.mouse_event(2,0,0,0,0);position(w,width-1,14);u.mouse_event(4,0,0,0,0);pump();maximum=abs(w.get()-hi)<1e-6
                snap(f'{mode}-scale-{index}-max-focus')
                center=w.tk.call(w._w,'coords',float(w.get()));position(w,float(center[0]),14);u.mouse_event(2,0,0,0,0);position(w,0,14);u.mouse_event(4,0,0,0,0);pump();minimum=abs(w.get()-lo)<1e-6
                snap(f'{mode}-scale-{index}-min-focus');key(0x27);keyboard=w.get()>lo
                w.set((lo+hi)/2);pump();center=w.tk.call(w._w,'coords',float(w.get()));position(w,float(center[0]),14);snap(f'{mode}-scale-{index}-hover')
                u.mouse_event(2,0,0,0,0);pump();mouse_focus=r.focus_get() is w;pressed=w.instate(['pressed']);snap(f'{mode}-scale-{index}-pressed');u.mouse_event(4,0,0,0,0);pump()
                w.state(['disabled']);before=float(w.get());image=crop(w)
                disabled_background=all(near(image.getpixel(pt),rgb(p['surface'])) for pt in [(2,2),(width-3,2),(2,height-3),(width-3,height-3)])
                position(w,width*.7,14);u.mouse_event(2,0,0,0,0);u.mouse_event(4,0,0,0,0);key(0x27);disabled_value=float(w.get())==before
                snap(f'{mode}-scale-{index}-disabled');w.state(['!disabled'])
                records.append({'instance':str(w),'hit_height':height,'rail_rows':len(rail_rows),'thumb_width':diameter,'thumb_height':extent,'geometry_ok':geometry_ok,'pointer_max':maximum,'pointer_min':minimum,'keyboard':keyboard,'disabled_background':disabled_background,'disabled_value':disabled_value,'mouse_focus':mouse_focus,'pressed_state':pressed})
            # All actual shared input instances, including off-screen advanced fields.
            inputs=[]
            for tab in range(5):
                app.notebook.select(tab);pump()
                page=app._scroll_pages[tab]
                for w in walk(page):
                    if isinstance(w,(ttk.Combobox,ttk.Entry)):
                        w.focus_force();pump();w.state(['!focus']);r.focus_force();pump();image=crop(w);ww,hh=image.size
                        corners_clear=all(near(image.getpixel(pt),rgb(p['surface'])) for pt in [(0,0),(ww-1,0),(0,hh-1),(ww-1,hh-1)])
                        inputs.append({'path':str(w),'height':hh,'corners_clear':corners_clear,'type':w.winfo_class()})
                snap(f'{mode}-page-{tab}-inputs')
            # Header theme selector: same geometry and actual arrow hit.
            combo=next(w for w in walk(r) if isinstance(w,ttk.Combobox) and w.cget('textvariable')==str(app.theme_label_var))
            r.focus_force();pump();image=crop(combo);image.save(out/f'{mode}-theme-selector-normal.png')
            ww,hh=image.size;arrow=combo.identify(ww-22,hh//2)
            position(combo,ww-22,hh//2);u.mouse_event(2,0,0,0,0);u.mouse_event(4,0,0,0,0);pump()
            popup=r._shengmu_choices.popup;posted=bool(popup and popup.owner is combo and popup.window.winfo_ismapped());snap(f'{mode}-theme-selector-posted');key(27)
            app.notebook.select(1);r.geometry('760x520+100+40');pump();app.text_opacity_scale.focus_force();pump();snap(f'{mode}-small-bottom')
            bottom=app.text_opacity_scale.winfo_rooty()+app.text_opacity_scale.winfo_height()<=app._scroll_pages[1].winfo_rooty()+app._scroll_pages[1].winfo_height()
            last=next(w for w in walk(app._scroll_pages[1]) if isinstance(w,ttk.Button) and w.cget('text')=='找回字幕窗')
            last.focus_force();pump();view=app._scroll_pages[1]
            action_visible=(last.winfo_rooty()>=view.winfo_rooty() and last.winfo_rooty()+last.winfo_height()<=view.winfo_rooty()+view.winfo_height())
            position(last);u.mouse_event(2,0,0,0,0);u.mouse_event(4,0,0,0,0);pump();app.overlay.hide();snap(f'{mode}-small-bottom-actions')
            r.geometry('980x780+100+40');pump()
            results['themes'][mode]={'scales':records,'inputs':inputs,'arrow_element':arrow,'arrow_opens':posted,'last_scale_reachable':bottom,'last_action_reachable':action_visible}
    finally:
        old=u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4));u.SetCursorPos(old_pointer.x,old_pointer.y);u.SetThreadDpiAwarenessContext(old);app.close()
    results['callback_errors']=errors;results['formal_settings_unchanged']=hashlib.sha256(formal.read_bytes()).hexdigest()==formal_hash
    (out/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    passed=not errors and results['formal_settings_unchanged'] and all(t['arrow_opens'] and t['last_scale_reachable'] and t['last_action_reachable'] and all(x['corners_clear'] for x in t['inputs']) and all(all(v for k,v in s.items() if k in ['geometry_ok','pointer_max','pointer_min','keyboard','disabled_background','disabled_value','mouse_focus','pressed_state']) for s in t['scales']) for t in results['themes'].values())
    print(json.dumps({'passed':passed,'scale':args.text_scale,'input_instances':{mode:len(t['inputs']) for mode,t in results['themes'].items()},'scales':{mode:t['scales'] for mode,t in results['themes'].items()},'errors':errors,'formal_settings_unchanged':results['formal_settings_unchanged']},ensure_ascii=True))
    if not passed:raise SystemExit('Control geometry specification failed')


if __name__=='__main__':main()
