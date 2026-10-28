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
values=['系统播放：默认扬声器','麦克风：默认输入（示例设备）','系统播放：耳机（示例设备）','系统播放：显示器音频（示例设备）','麦克风：USB音频设备（示例设备）','系统播放：虚拟输出（示例设备）','麦克风：桌面输入（示例设备）','系统播放：第二输出设备（示例设备）','系统播放：第三输出设备（示例设备）','麦克风：备用输入（示例设备）','系统播放：备用输出（示例设备）','Beta：自动跟踪有声音的应用']
result={'text_scale':args.text_scale,'tk_scaling':scaling*args.text_scale,'audio_started':False,'themes':{}}
try:
 pump();root.lift();root.focus_force();pump()
 manager=root._shengmu_choices
 for mode,label in [('dark','深色黑金'),('light','浅色白金')]:
  app.theme_label_var.set(label);app._on_theme_selected();app.notebook.select(0);app._scroll_pages[0].yview_moveto(0);pump()
  app.audio_combo.configure(values=values);app.audio_combo.current(0)
  selected_events=[];binding=app.audio_combo.bind('<<ComboboxSelected>>',lambda e:selected_events.append(e.widget.current()),add='+')
  click(app.audio_combo,.50,.5);assert manager.popup is not None,'Pointer text area failed to open'
  popup=manager.popup
  assert popup.owner is app.audio_combo and len(popup.values)==len(values)
  snap(mode+'-audio-open')
  key(40);key(40);assert app.audio_combo.current()==0,'Navigation changed committed value'
  snap(mode+'-audio-focus')
  key(27);assert manager.popup is None and app.audio_combo.current()==0,'Escape changed committed value'
  assert root.grab_current() is None,'Grab remained after cancel'
  click(app.audio_combo,.95,.5);assert manager.popup is not None,'Arrow failed to open'
  key(35);snap(mode+'-audio-last');key(13);assert app.audio_combo.current()==len(values)-1,'End/Enter did not commit last'
  assert len(selected_events)==1 and manager.popup is None,'Selection event count or dismissal wrong'
  keyboard_last=True
  # A long value must wrap and remain readable; the font remains the field font.
  long_value='系统播放：'+('一个带有中文与 VeryLongDeviceName 标识的多通道设备说明；'*8)
  app.audio_combo.configure(values=[long_value]+values);app.audio_combo.current(0)
  click(app.audio_combo,.95,.5);assert manager.popup is not None
  snap(mode+'-audio-long-text');key(27)
  app.audio_combo.configure(values=values);app.audio_combo.current(0)
  # All main pages use the same popup presentation; do not select a different setting.
  checked=[];disabled=[]
  for page in range(5):
   app.notebook.select(page);app._scroll_pages[page].yview_moveto(0);pump()
   combos=[w for w in descendants(app._workspace) if isinstance(w,ttk.Combobox)]
   for combo in combos:
    if not combo.winfo_ismapped():continue
    if combo.instate(['disabled']):
     manager.post(combo,keyboard=True);assert manager.popup is None;disabled.append(str(combo));continue
    combo.focus_force();pump();manager.post(combo,keyboard=True);pump()
    if combo.cget('values'):
     assert manager.popup is not None and manager.popup.owner is combo,str(combo)
     checked.append(str(combo));manager.dismiss();pump()
   snap(mode+'-page-'+str(page))
  app.notebook.select(0);pump()
  app.audio_combo.focus_force();pump(.18);key(115) # F4
  assert manager.popup is not None,'F4 failed to open'
  key(9);assert manager.popup is None,'Tab failed to dismiss'
  tab_focus=root.focus_get() is not app.audio_combo
  assert tab_focus,'Tab did not move focus'
  app.audio_combo.configure(state='disabled');manager.post(app.audio_combo);assert manager.popup is None
  app.audio_combo.configure(state='readonly')
  # Opening survives a theme refresh safely; no stale grab/window may remain.
  manager.post(app.audio_combo);assert manager.popup is not None
  from theme import apply_theme
  apply_theme(root,mode);pump();assert root._shengmu_choices is manager and manager.popup is None
  assert root.grab_current() is None
  app.audio_combo.unbind('<<ComboboxSelected>>',binding)
  result['themes'][mode]={'long_audio_values':len(values),'keyboard_last_commit':keyboard_last,'selection_event_once':True,'escape_preserves_value':True,'tab_moves_focus':tab_focus,'disabled_does_not_open':True,'theme_refresh_closes_popup':True,'checked_comboboxes':checked,'disabled_comboboxes':disabled,'field_font':str(app.audio_combo.cget('font'))}
  print(json.dumps({'theme':mode,'checked_comboboxes':len(checked),'passed':True}),flush=True)
 result['audio_started']=app.pipeline is not None
finally:
 position(old_pointer.x,old_pointer.y);app.close()
result['callback_errors']=errors;result['formal_settings_unchanged']=hashlib.sha256(formal.read_bytes()).hexdigest()==before
(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(result,ensure_ascii=True))
assert not errors and not result['audio_started'] and result['formal_settings_unchanged']
