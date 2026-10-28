"""Owned Windows state walk, isolated settings; final package evidence is separate."""
from pathlib import Path
import os,sys,json,hashlib,time,ctypes,argparse
from ctypes import wintypes
from unittest.mock import patch
parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--text-scale',type=float,default=1);args=parser.parse_args()
repo=Path(__file__).resolve().parents[1];sys.path.insert(0,str(repo));sys.path.insert(0,str(repo/'tools'))
out=args.output;out.mkdir(parents=True,exist_ok=True)
formal=Path(os.environ['APPDATA'])/'AudioSubtitleOverlay/settings.json';before=hashlib.sha256(formal.read_bytes()).hexdigest()
os.environ['APPDATA']=str(out/'isolated-appdata')
import tkinter as tk
from tkinter import ttk,font
from main import Application
from overlay import OverlayWindow
from model_manager import ModelManagerWindow
from verify_installed_runtime import capture_visible_window
root=tk.Tk();scaling=float(root.tk.call('tk','scaling'));root.tk.call('tk','scaling',scaling*args.text_scale)
with patch.object(OverlayWindow,'show'):
 app=Application(root,autostart=False,tray=False)
app.overlay.hide();root.geometry('1000x820+80+30');root.attributes('-topmost',True)
errors=[];root.report_callback_exception=lambda *x:errors.append(str(x[1]))
user=ctypes.windll.user32;user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT];user.GetAncestor.restype=wintypes.HWND
user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)];user.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p];user.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
old_pointer=wintypes.POINT();pointer_context=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4));user.GetPhysicalCursorPos(ctypes.byref(old_pointer));user.SetThreadDpiAwarenessContext(pointer_context)
def pump(sec=.10):
 root.after(max(1,round(sec*1000)),root.quit)
 root.mainloop()
def position(x,y):
 prior=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
 try:user.SetCursorPos(round(x),round(y))
 finally:user.SetThreadDpiAwarenessContext(prior)
def rect(w):
 prior=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
 try:
  b=wintypes.RECT();user.GetWindowRect(w.winfo_id(),ctypes.byref(b));return b
 finally:user.SetThreadDpiAwarenessContext(prior)
def click(w,fx=.5,fy=.5):
 b=rect(w);position(b.left+(b.right-b.left)*fx,b.top+(b.bottom-b.top)*fy);pump(.03)
 user.mouse_event(2,0,0,0,0);pump(.02);user.mouse_event(4,0,0,0,0);pump()
def key(k):
 user.keybd_event(k,0,0,0);user.keybd_event(k,0,2,0);pump()
def snap(name):
 pump();capture_visible_window({'hwnd':user.GetAncestor(root.winfo_id(),2)},out/(name+'.png'))
 popup=getattr(root,'_shengmu_choices',None).popup
 if popup is not None:
  capture_visible_window({'hwnd':user.GetAncestor(popup.window.winfo_id(),2)},out/(name+'-full-popup.png'))
  from choice_popup import _work_area
  work=_work_area(popup.owner)
  (out/(name+'-placement.json')).write_text(json.dumps({'placement':vars(popup.placement),'work_area':vars(work),'anchor':[popup.owner.winfo_rootx(),popup.owner.winfo_rooty(),popup.owner.winfo_width(),popup.owner.winfo_height()],'native_rounding':popup.native_rounding,'selected':popup.selected_index,'active':popup.active_index,'row_height':popup.row_height,'rows':len(popup.rows)},ensure_ascii=False,indent=2),encoding='utf8')
def descendants(w):
 result=[]
 for c in w.winfo_children():result.append(c);result+=descendants(c)
 return result
result={'themes':{}}
try:
 pump();root.lift();root.focus_force();pump()
 for mode,label in [('dark','深色黑金'),('light','浅色白金')]:
  app.theme_label_var.set(label);app._on_theme_selected();app.notebook.select(0);pump()
  def tab_click(index):
   # Tab centres in the notebook's own logical coordinate system.
   xs=[]
   for x in range(0,800,2):
    try:
     if app.notebook.index('@%s,20'%x)==index:xs.append(x)
    except tk.TclError:pass
   b=rect(app.notebook);position(b.left+(min(xs)+max(xs))/2/app.notebook.winfo_width()*(b.right-b.left),b.top+20/app.notebook.winfo_height()*(b.bottom-b.top));pump(.05)
   user.mouse_event(2,0,0,0,0);pump(.03);user.mouse_event(4,0,0,0,0);pump(.2)
  tab_click(0)
  assert not root._shengmu_tab_input['keyboard'] and app.notebook.instate(['!user1'])
  snap(mode+'-tab-mouse')
  key(39)
  assert root._shengmu_tab_input['keyboard'] and app.notebook.instate(['user1']) and root.focus_get() is app.notebook
  assert app.notebook.index(app.notebook.select())==1
  snap(mode+'-tab-keyboard')
  key(9);assert root.focus_get() is not app.notebook
  snap(mode+'-tab-content-focus')
  tab_click(1)
  fixture=tk.Toplevel(root);fixture.title('声幕：窗口焦点验证');fixture.geometry('220x80+40+40');ttk.Button(fixture,text='验证窗口').pack();fixture.focus_force();pump(.2)
  fixture.destroy();root.lift();root.focus_force();pump(.2)
  assert not root._shengmu_tab_input['keyboard'] and app.notebook.instate(['!user1'])
  snap(mode+'-tab-window-reactivated')
  result['themes'][mode]={'mouse_mode_cleared':True,'real_keyboard_selects_next':True,'keyboard_mode_visible_state':True,'tab_focus_enters_content':True,'window_reactivation_does_not_enable_keyboard_mode':True}
finally:
 position(old_pointer.x,old_pointer.y);app.close()
result['formal_settings_unchanged']=hashlib.sha256(formal.read_bytes()).hexdigest()==before;result['callback_errors']=errors
(out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result));assert not errors and result['formal_settings_unchanged']
