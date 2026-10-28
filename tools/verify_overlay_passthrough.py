"""Bounded cross-process mouse routing acceptance; no audio or saved user settings.

Run on Windows, with desktop ownership assigned to this tool for the run:
    python tools/verify_overlay_passthrough.py --source <repo> --output <new-E-directory>
An optional --candidate directory overlays reviewed native/overlay source files.
Only this process's overlay and its child counter window receive synthetic input.
"""
from __future__ import annotations

import argparse
from collections import deque
import ctypes as c
from ctypes import wintypes as w
from dataclasses import asdict
import hashlib
import json
import multiprocessing as mp
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid


class GUIThreadInfo(c.Structure):
    _fields_=[('size',w.DWORD),('flags',w.DWORD),('active',w.HWND),('focus',w.HWND),
              ('capture',w.HWND),('menu_owner',w.HWND),('move_size',w.HWND),
              ('caret',w.HWND),('caret_rect',w.RECT)]


def native_api():
    user = c.WinDLL('user32', use_last_error=True)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    signatures = [
        (user.GetAncestor, [w.HWND, w.UINT], w.HWND),
        (user.GetWindowThreadProcessId, [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
        (user.IsWindow, [w.HWND], w.BOOL),
        (user.IsWindowVisible, [w.HWND], w.BOOL),
        (user.GetAsyncKeyState, [c.c_int], w.SHORT),
        (user.GetCapture, [], w.HWND),
        (user.GetMessageTime, [], w.LONG),
        (user.GetMessageExtraInfo, [], c.c_ssize_t),
        (user.GetDoubleClickTime, [], w.UINT),
        (user.EndMenu, [], w.BOOL),
        (user.GetCursorPos, [c.POINTER(w.POINT)], w.BOOL),
        (user.GetPhysicalCursorPos, [c.POINTER(w.POINT)], w.BOOL),
        (user.GetClipCursor, [c.POINTER(w.RECT)], w.BOOL),
        (user.GetWindowRect, [w.HWND, c.POINTER(w.RECT)], w.BOOL),
        (user.GetClientRect, [w.HWND, c.POINTER(w.RECT)], w.BOOL),
        (user.ClientToScreen, [w.HWND, c.POINTER(w.POINT)], w.BOOL),
        (user.GetWindowTextW, [w.HWND, w.LPWSTR, c.c_int], c.c_int),
        (user.GetClassNameW, [w.HWND, w.LPWSTR, c.c_int], c.c_int),
        (user.GetForegroundWindow, [], w.HWND),
        (user.GetGUIThreadInfo, [w.DWORD,c.POINTER(GUIThreadInfo)], w.BOOL),
        (user.GetWindowLongW, [w.HWND,c.c_int], w.LONG),
        (user.SetWindowPos, [w.HWND,w.HWND,c.c_int,c.c_int,c.c_int,c.c_int,w.UINT], w.BOOL),
        (user.SetForegroundWindow, [w.HWND], w.BOOL),
        (user.WindowFromPoint, [w.POINT], w.HWND),
        (user.SetCursorPos, [c.c_int, c.c_int], w.BOOL),
        (kernel.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        (kernel.GetProcessTimes, [w.HANDLE, c.POINTER(w.FILETIME), c.POINTER(w.FILETIME),
                                  c.POINTER(w.FILETIME), c.POINTER(w.FILETIME)], w.BOOL),
        (kernel.CloseHandle, [w.HANDLE], w.BOOL),
    ]
    for function, arguments, result in signatures:
        function.argtypes, function.restype = arguments, result
    for name,arguments,result in [
        ('GetDpiAwarenessContextForProcess',[w.HANDLE],c.c_void_p),
        ('GetThreadDpiAwarenessContext',[],c.c_void_p),
        ('GetWindowDpiAwarenessContext',[w.HWND],c.c_void_p),
        ('GetAwarenessFromDpiAwarenessContext',[c.c_void_p],c.c_int),
        ('AreDpiAwarenessContextsEqual',[c.c_void_p,c.c_void_p],w.BOOL),
        ('GetDpiForWindow',[w.HWND],w.UINT),
    ]:
        if hasattr(user,name):
            function=getattr(user,name);function.argtypes,function.restype=arguments,result
    if hasattr(user, 'SetThreadDpiAwarenessContext'):
        user.SetThreadDpiAwarenessContext.argtypes = [c.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = c.c_void_p
        user.SetThreadDpiAwarenessContext(c.c_void_p(-4))
    return user, kernel


def process_created(kernel, pid):
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError(c.get_last_error(), 'OpenProcess failed')
    try:
        values = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(c.byref(value) for value in values)):
            raise OSError(c.get_last_error(), 'GetProcessTimes failed')
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def window_identity(user, kernel, hwnd, token, expected_pid=None):
    pid = w.DWORD()
    if not user.IsWindow(hwnd) or not user.GetWindowThreadProcessId(hwnd, c.byref(pid)):
        raise RuntimeError('Owned window is not alive')
    if expected_pid is not None and pid.value != expected_pid:
        raise RuntimeError('Owned HWND belongs to another PID')
    title = c.create_unicode_buffer(512)
    user.GetWindowTextW(hwnd, title, len(title))
    if token not in title.value:
        raise RuntimeError('Owned HWND run token mismatch')
    return {'pid': pid.value, 'hwnd': int(hwnd), 'created_filetime': process_created(kernel, pid.value),
            'run_token': token}


def assert_identity(user, kernel, identity):
    actual = window_identity(user, kernel, identity['hwnd'], identity['run_token'], identity['pid'])
    if actual != identity:
        raise RuntimeError('Owned window/process identity changed')


class UnexpectedInput(RuntimeError):
    """The run contains input outside its declared plan; origin is unproven."""


class NativeMessageObserver:
    """Forward every message; observe at most three verified own HWNDs per thread."""
    MESSAGES={0x0200:'WM_MOUSEMOVE',0x0201:'WM_LBUTTONDOWN',0x0202:'WM_LBUTTONUP',
              0x0203:'WM_LBUTTONDBLCLK',0x0204:'WM_RBUTTONDOWN',0x0205:'WM_RBUTTONUP',
              0x0206:'WM_RBUTTONDBLCLK',0x020A:'WM_MOUSEWHEEL',0x020E:'WM_MOUSEHWHEEL',
              0x0207:'WM_MBUTTONDOWN',0x0208:'WM_MBUTTONUP',0x0209:'WM_MBUTTONDBLCLK',
              0x020B:'WM_XBUTTONDOWN',0x020C:'WM_XBUTTONUP',0x020D:'WM_XBUTTONDBLCLK',
              0x00A0:'WM_NCMOUSEMOVE',0x00A1:'WM_NCLBUTTONDOWN',0x00A2:'WM_NCLBUTTONUP',
              0x00A3:'WM_NCLBUTTONDBLCLK',0x00A4:'WM_NCRBUTTONDOWN',0x00A5:'WM_NCRBUTTONUP',
              0x00A6:'WM_NCRBUTTONDBLCLK',0x02A3:'WM_MOUSELEAVE',
              0x00A7:'WM_NCMBUTTONDOWN',0x00A8:'WM_NCMBUTTONUP',0x00A9:'WM_NCMBUTTONDBLCLK',
              0x00AB:'WM_NCXBUTTONDOWN',0x00AC:'WM_NCXBUTTONUP',0x00AD:'WM_NCXBUTTONDBLCLK',
              0x0215:'WM_CAPTURECHANGED',0x001F:'WM_CANCELMODE',0x0082:'WM_NCDESTROY'}
    _active=set()  # Keep C callbacks alive even if partial-install cleanup fails.

    def __init__(self,user,kernel,identity,hwnds,context=None,*,comctl=None,callback_type=None,limit=4096):
        if (identity['pid']!=os.getpid() or set(hwnds)!={'wrapper','client','canvas'}
                or not 1<=len(set(hwnds.values()))<=3 or type(limit) is not int or limit<1):
            raise ValueError('Observer requires the fixed owned wrapper/client/Canvas HWNDs')
        assert_identity(user,kernel,identity)
        self.user=user;self.kernel=kernel;self.identity=identity;self.context=context
        self.thread=threading.get_native_id();self.events=deque(maxlen=limit)
        self.limit=limit;self.total=0;self.overflow=0;self.errors=[];self.bindings={};self.uncovered_capture_count=0
        self.native_buttons=set();self.buttons_at_phase_start=[]
        self.subclass_id=(uuid.uuid4().int & ((1<<(c.sizeof(c.c_void_p)*8))-1)) or 1
        self.comctl=comctl if comctl is not None else c.WinDLL('comctl32',use_last_error=True)
        callback_type=callback_type or getattr(c,'WINFUNCTYPE',c.CFUNCTYPE)(
            c.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM,c.c_size_t,c.c_size_t)
        self.callback=callback_type(self._dispatch)  # Strong reference until removal/destruction.
        for name,args,result in [
            ('SetWindowSubclass',[w.HWND,callback_type,c.c_size_t,c.c_size_t],w.BOOL),
            ('GetWindowSubclass',[w.HWND,callback_type,c.c_size_t,c.POINTER(c.c_size_t)],w.BOOL),
            ('RemoveWindowSubclass',[w.HWND,callback_type,c.c_size_t],w.BOOL),
            ('DefSubclassProc',[w.HWND,w.UINT,w.WPARAM,w.LPARAM],c.c_ssize_t),
        ]:
            fn=getattr(self.comctl,name);fn.argtypes=args;fn.restype=result
        # Validate the complete fixed set before installing the first callback.
        descriptions={}
        for role,hwnd in hwnds.items():
            pid=w.DWORD();thread=user.GetWindowThreadProcessId(hwnd,c.byref(pid))
            if (not user.IsWindow(hwnd) or pid.value!=identity['pid'] or thread!=self.thread
                    or int(user.GetAncestor(hwnd,2) or 0)!=identity['hwnd']):
                raise RuntimeError('Observer HWND is outside the verified own thread/window')
            chain=[];cursor=hwnd
            for _ in range(16):
                parent_pid=w.DWORD();parent_thread=user.GetWindowThreadProcessId(cursor,c.byref(parent_pid))
                if parent_pid.value!=identity['pid'] or parent_thread!=self.thread:
                    raise RuntimeError('Observer parent chain left the verified own thread')
                class_name=c.create_unicode_buffer(256)
                if not user.GetClassNameW(cursor,class_name,len(class_name)):
                    raise RuntimeError('Own observation HWND class read failed')
                chain.append(dict(hwnd=int(cursor),window_class=class_name.value))
                if cursor==identity['hwnd']:break
                cursor=int(user.GetAncestor(cursor,1) or 0)
                if not cursor:raise RuntimeError('Observer parent chain missed the owned wrapper')
            else:raise RuntimeError('Own observation parent chain exceeded its bound')
            descriptions[hwnd]=chain
        self._active.add(self)
        try:
            for role,hwnd in hwnds.items():
                if hwnd in self.bindings:
                    self.bindings[hwnd]['role']+='+'+role
                    continue
                if not self.comctl.SetWindowSubclass(hwnd,self.callback,self.subclass_id,self.subclass_id):
                    raise RuntimeError('Own HWND observation install failed')
                self.bindings[hwnd]={'role':role,'installed':True,'parent_chain':descriptions[hwnd]}
                if not self._present(hwnd):
                    raise RuntimeError('Own HWND observation install readback failed')
        except Exception:
            self.close()
            raise

    def _error(self,error):
        if len(self.errors)<16:self.errors.append(str(error))

    def _present(self,hwnd):
        reference=c.c_size_t()
        found=bool(self.comctl.GetWindowSubclass(hwnd,self.callback,self.subclass_id,c.byref(reference)))
        if found and reference.value!=self.subclass_id:
            raise RuntimeError('Own observation reference changed')
        return found

    def _remove(self,hwnd):
        if threading.get_native_id()!=self.thread:
            raise RuntimeError('Observation cleanup must remain on its owner thread')
        if self.user.IsWindow(hwnd):
            pid=w.DWORD();thread=self.user.GetWindowThreadProcessId(hwnd,c.byref(pid))
            if (pid.value!=self.identity['pid'] or thread!=self.thread
                    or int(self.user.GetAncestor(hwnd,2) or 0)!=self.identity['hwnd']):
                raise RuntimeError('Observation HWND ownership changed before cleanup')
            if self._present(hwnd):
                if not self.comctl.RemoveWindowSubclass(hwnd,self.callback,self.subclass_id) or self._present(hwnd):
                    raise RuntimeError('Own HWND observation removal/readback failed')
        self.bindings[hwnd]['installed']=False
        if not any(binding['installed'] for binding in self.bindings.values()):self._active.discard(self)

    def _dispatch(self,hwnd,message,wparam,lparam,subclass_id,reference):
        entry=None
        try:
            if message in self.MESSAGES:
                if len(self.events)>=self.limit:self.overflow+=1
                self.total+=1
                entry=dict(number=self.total,perf=time.perf_counter(),hwnd=int(hwnd),
                           role=self.bindings.get(hwnd,{}).get('role','installing'),message=int(message),
                           name=self.MESSAGES[message],wparam=int(wparam),lparam=int(lparam),
                           message_time=int(self.user.GetMessageTime()),
                           message_extra_info=int(self.user.GetMessageExtraInfo()),
                           capture_current_thread=int(self.user.GetCapture() or 0))
                entry['mk_lbutton']=bool(wparam & 1) if 0x0200<=message<=0x020E else None
                if 0x0200<=message<=0x020E or 0x00A0<=message<=0x00AD:
                    x,y=c.c_short(lparam & 0xFFFF).value,c.c_short((lparam>>16)&0xFFFF).value
                    if message in (0x020A,0x020E) or 0x00A0<=message<=0x00AD:
                        entry['screen_point']=[x,y]
                    else:
                        pos=w.POINT(x,y)
                        if not self.user.ClientToScreen(hwnd,c.byref(pos)):
                            raise RuntimeError('Native input coordinate conversion failed')
                        entry['screen_point']=[pos.x,pos.y]
                button={0x0201:'left',0x0202:'left',0x0203:'left',0x0204:'right',0x0205:'right',0x0206:'right',
                        0x0207:'middle',0x0208:'middle',0x0209:'middle',
                        0x00A1:'left',0x00A2:'left',0x00A3:'left',0x00A4:'right',0x00A5:'right',0x00A6:'right',
                        0x00A7:'middle',0x00A8:'middle',0x00A9:'middle'}.get(message)
                if message in (0x020B,0x020C,0x020D,0x00AB,0x00AC,0x00AD):
                    identifier=(wparam>>16)&0xFFFF
                    if identifier not in (1,2):raise RuntimeError('Unknown native X button identifier')
                    button='x'+str(identifier)
                if button:
                    if message in (0x0202,0x0205,0x0208,0x020C,0x00A2,0x00A5,0x00A8,0x00AC):self.native_buttons.discard(button)
                    else:self.native_buttons.add(button)
                entry['capture_covered']=not entry['capture_current_thread'] or entry['capture_current_thread'] in self.bindings
                if not entry['capture_covered']:self.uncovered_capture_count+=1
                if self.context is not None:entry['context']=dict(self.context)
                self.events.append(entry)
            if message==0x0082 and hwnd in self.bindings:self._remove(hwnd)
        except BaseException as error:
            self._error(error)
        # No Tk calls, capture changes, filtering or replacement messages here.
        try:
            return self.comctl.DefSubclassProc(hwnd,message,wparam,lparam)
        except BaseException as error:
            self._error(error)
            return 0
        finally:
            if entry is not None:
                try:
                    entry['forward_return_perf']=time.perf_counter()
                    entry['capture_after_forward']=int(self.user.GetCapture() or 0)
                    entry['capture_after_covered']=not entry['capture_after_forward'] or entry['capture_after_forward'] in self.bindings
                    if not entry['capture_after_covered']:self.uncovered_capture_count+=1
                except BaseException as error:self._error(error)

    def snapshot(self):
        return dict(events=list(self.events),total=self.total,overflow=self.overflow,errors=list(self.errors),
                    uncovered_capture_count=self.uncovered_capture_count,
                    buttons_at_phase_start=list(self.buttons_at_phase_start),native_buttons=sorted(self.native_buttons),
                    bindings={str(k):dict(v) for k,v in self.bindings.items()},
                    meaning='Own HWND window-procedure delivery; not hardware-input provenance')

    def clear(self):
        self.buttons_at_phase_start=sorted(self.native_buttons)
        self.events.clear();self.total=0  # Never clear an observation error/overflow.

    def close(self):
        for hwnd,binding in self.bindings.items():
            if binding['installed']:
                try:self._remove(hwnd)
                except Exception as error:self._error(error)
        if not any(binding['installed'] for binding in self.bindings.values()):self._active.discard(self)
        if self.errors:raise RuntimeError('Own HWND observation failure: '+str(self.errors))


def require_complete_native_observation(observation):
    if observation['overflow'] or observation['errors']:
        raise RuntimeError('Native message observation is incomplete')


def _native_rows(observation):
    require_complete_native_observation(observation)
    bindings=observation['bindings']
    roles={role for binding in bindings.values() for role in binding['role'].split('+')}
    if roles!={'wrapper','client','canvas'} or not all(x['installed'] is True for x in bindings.values()):
        raise RuntimeError('Native receiver HWND coverage is incomplete')
    rows=observation['events']
    if [x['number'] for x in rows]!=list(range(1,len(rows)+1)) or observation['total']!=len(rows):
        raise RuntimeError('Native receiver history is incomplete')
    for row in rows:
        if (str(row['hwnd']) not in bindings or row['capture_covered'] is not True or row['capture_after_covered'] is not True
                or not math.isfinite(row['perf']) or not math.isfinite(row['forward_return_perf'])
                or row['forward_return_perf']<row['perf']):
            raise RuntimeError('Native input cannot be inferred from incomplete coverage')
    return rows


def _require_passive_native_phase(observation):
    rows=_native_rows(observation)
    for row in rows:
        passive=(row['name']=='WM_MOUSEMOVE' and row['wparam']==0 and row['mk_lbutton'] is False
                 or row['name']=='WM_MOUSELEAVE' and row['wparam']==row['lparam']==0)
        if not passive or row['capture_current_thread'] or row['capture_after_forward']:
            raise UnexpectedInput('Blocked receiver got an actual or unknown native input message')
    return rows


def require_blocked_overlay(tk_events,observation):
    """Keep strict zero Tk callbacks; no exception for its earlier own menu."""
    if tk_events:raise UnexpectedInput('Passthrough overlay got a Tk input callback')
    rows=_require_passive_native_phase(observation)
    return dict(receiver='overlay',criterion='zero_Tk_callbacks_and_zero_phase_native_operations',
                native_operations=0,passive_native_messages=len(rows),
                previous_menu_button_history='Outside fixed observation; no diagnostic exception applied')


def classify_blocked_receiver(tk_events,observation):
    """Lower counter only: retain an evidenced Tk state release as diagnostic."""
    rows=_require_passive_native_phase(observation)
    if observation['buttons_at_phase_start'] or observation['native_buttons']:
        raise UnexpectedInput('Blocked counter native button prehistory/end state is unbalanced')
    diagnostics=[]
    for event in tk_events:
        number=event.get('latest_native_message_number')
        if event.get('kind')!='release' or type(number) is not int or not 1<=number<=len(rows):
            raise UnexpectedInput('Blocked receiver got an unexplained Tk input callback')
        row=rows[number-1];next_perf=rows[number]['perf'] if number<len(rows) else math.inf
        if (row['name']!='WM_MOUSEMOVE' or row['screen_point']!=[event['x'],event['y']]
                or not math.isfinite(event['perf']) or not row['perf']<=event['perf']<next_perf):
            raise UnexpectedInput('Tk release does not match the observed no-button MOVE window')
        diagnostics.append(dict(kind='tk_release_without_local_native_button_message',
                                callback=dict(event),native_message_number=number,
                                meaning='No native button message at this receiver; exact internal Tk trigger unobserved'))
    return dict(receiver='lower_counter',criterion='zero_native_operations_with_exact_Tk_state_diagnostic',
                native_operations=0,diagnostics=diagnostics,passive_native_messages=len(rows))


def require_native_delivery(tk_events,observation,actions,*,full_matrix=True):
    """Independently prove every planned lower-process native input packet."""
    rows=_native_rows(observation)
    if observation['buttons_at_phase_start'] or observation['native_buttons']:
        raise RuntimeError('Native receiver button prehistory/end state is not balanced')
    names={0x2:'WM_LBUTTONDOWN',0x4:'WM_LBUTTONUP',0x8:'WM_RBUTTONDOWN',
           0x10:'WM_RBUTTONUP',0x800:'WM_MOUSEWHEEL'}
    packets=[x for x in actions if 'flags' in x]
    if any(type(x['flags']) is not int and not (type(x['flags']) is list and all(type(v) is int for v in x['flags'])) for x in packets):
        raise RuntimeError('Input flags must be exact integers or the fixed integer pair')
    fixed=([('left',2),('left',4),('right',[8,16]),('double',2),('double',4),('double',2),
            ('double',4),('drag',2),('drag',4),('wheel',0x800)] if full_matrix else
           [('left',2),('left',4),('wheel',0x800)])
    if [(x.get('gesture'),x['flags']) for x in packets]!=fixed:
        raise RuntimeError('Input actions do not contain the fixed complete gesture/packet plan')
    expected=[]
    for index,action in enumerate(packets):
        flags=action['flags'] if isinstance(action['flags'],list) else [action['flags']]
        if action['sendinput_inserted']!=len(flags):raise RuntimeError('Planned input packet was not fully inserted')
        end=packets[index+1]['inject_perf'] if index+1<len(packets) else math.inf
        for flag in flags:
            if flag not in names:raise UnexpectedInput('Unknown planned native input packet')
            expected.append((names[flag],action['inject_perf'],end,list(action['point'])))
    operations=[]
    button_state=0
    for row in rows:
        if row['name'] in names.values():operations.append(row)
        elif row['name'] not in {'WM_MOUSEMOVE','WM_MOUSELEAVE','WM_CAPTURECHANGED'}:
            raise UnexpectedInput('Unexpected native operation at receiving process')
        if row['name']=='WM_LBUTTONDOWN':button_state|=1
        elif row['name']=='WM_LBUTTONUP':button_state&=~1
        elif row['name']=='WM_RBUTTONDOWN':button_state|=2
        elif row['name']=='WM_RBUTTONUP':button_state&=~2
        if row['name']=='WM_MOUSEMOVE' or row['name'] in names.values():
            if row['wparam']&0x73!=button_state:
                raise UnexpectedInput('Native mouse flags include an unplanned button state')
            if row['name']=='WM_MOUSEMOVE' and button_state:
                assert_planned_events([dict(x=row['screen_point'][0],y=row['screen_point'][1])],packets[0]['point'])
    if len(operations)!=len(expected):raise RuntimeError('Native input packet count mismatch')
    for row,(name,start,end,point) in zip(operations,expected):
        if row['name']!=name or not start<=row['perf']<end or row['screen_point']!=point:
            raise RuntimeError('Native input order/time/position mismatch')
        if name=='WM_MOUSEWHEEL' and c.c_short((row['wparam']>>16)&0xFFFF).value!=120:
            raise RuntimeError('Native wheel delta mismatch')
    kinds={event['kind'] for event in tk_events}
    if not kinds<={'left','release','right','double','drag','wheel'}:
        raise UnexpectedInput('Receiving process got an unknown Tk callback')
    if full_matrix and not {'left','release','right','double','drag','wheel'}<=kinds:
        raise RuntimeError('Receiving process Tk operation set incomplete')
    drag=[x for x in packets if x['gesture']=='drag']
    if full_matrix and (len(drag)!=2 or drag[0]['flags']!=2 or drag[1]['flags']!=4
            or not any(row['name']=='WM_MOUSEMOVE' and row['mk_lbutton'] is True
                       and drag[0]['inject_perf']<=row['perf']<drag[1]['inject_perf']
                       and row['screen_point']==list(drag[1]['point']) for row in rows)):
        raise RuntimeError('No actual native held-button move along the planned drag')
    return dict(matched_native_packets=len(operations),full_Tk_matrix=full_matrix,native_drag=full_matrix)


def independent_gesture_gap(milliseconds):
    """Separate a deliberate double click from the next independent drag."""
    if type(milliseconds) is not int or not 1<=milliseconds<=5000:
        raise ValueError('Unexpected system double-click interval')
    # Tk 8.6.15 tkBind.c also groups nearby button events for 500 ms,
    # independently of the Windows interval. Do not let a faster OS setting
    # turn this deliberately independent gesture into a Tk multi-click.
    return (max(milliseconds,500)+50)/1000


def tk_input_target_snapshot(root, overlay, point, event=None):
    """Observe Tk geometry/target only; this does not replace native hit guards."""
    canvas=overlay.canvas
    left,top=canvas.winfo_rootx(),canvas.winfo_rooty()
    width,height=canvas.winfo_width(),canvas.winfo_height()
    containing=root.winfo_containing(*point)
    event_widget=getattr(event,'widget',None)
    def widget_data(widget):
        return None if widget is None else dict(path=str(widget),widget_class=widget.winfo_class(),
                                                is_canvas=widget is canvas,is_overlay=widget is overlay.window)
    return dict(coordinate_space='Tk screen coordinates; not native hit-test attribution',
                canvas=dict(path=str(canvas),root_rect=[left,top,left+width,top+height],
                            actual_size=[width,height],viewable=bool(canvas.winfo_viewable())),
                containing=widget_data(containing),event_widget=widget_data(event_widget),
                product_drag_mode=getattr(overlay,'_drag_mode',None))


def cursor_snapshot(user,hwnd=None):
    """Read shared cursor/clip/DPI state; no owner attribution or clip mutation."""
    result={'perf':time.perf_counter()}
    for name,key,structure in [('GetCursorPos','logical',w.POINT),
                                ('GetPhysicalCursorPos','physical',w.POINT),
                                ('GetClipCursor','clip',w.RECT)]:
        if not hasattr(user,name):
            result[key]={'available':False};continue
        data=structure();ok=bool(getattr(user,name)(c.byref(data)))
        observation={'available':True,'success':ok,'perf':time.perf_counter()}
        if ok:
            observation['rect' if key=='clip' else 'point']=(
                [data.left,data.top,data.right,data.bottom] if key=='clip' else [data.x,data.y])
        else: observation['last_error']=c.get_last_error() if hasattr(c,'get_last_error') else None
        result[key]=observation
    required=('GetDpiAwarenessContextForProcess','GetThreadDpiAwarenessContext',
              'GetAwarenessFromDpiAwarenessContext','AreDpiAwarenessContextsEqual')
    if all(hasattr(user,name) for name in required):
        def context(handle):
            if not handle:return {'success':False,'opaque_context':0}
            matches=[name for value,name in [(-1,'unaware'),(-2,'system'),(-3,'per-monitor'),
                (-4,'per-monitor-v2'),(-5,'unaware-gdi-scaled')]
                if user.AreDpiAwarenessContextsEqual(handle,c.c_void_p(value))]
            return dict(success=True,opaque_context=int(handle),
                awareness=int(user.GetAwarenessFromDpiAwarenessContext(handle)),
                name=matches[0] if len(matches)==1 else 'other-context')
        result['dpi']={'process':context(user.GetDpiAwarenessContextForProcess(None)),
            'thread':context(user.GetThreadDpiAwarenessContext())}
        if hwnd and hasattr(user,'GetWindowDpiAwarenessContext') and hasattr(user,'GetDpiForWindow'):
            result['dpi']['window']=context(user.GetWindowDpiAwarenessContext(hwnd))
            result['dpi']['window_dpi']=int(user.GetDpiForWindow(hwnd))
    else: result['dpi']={'available':False}
    result['clip_owner']='GetClipCursor does not identify the process which last confined the cursor'
    return result


def checked_move(user,point,record,hwnd=None):
    """Existing planned move with exact observed results before any injection."""
    record['cursor_before_set']=cursor_snapshot(user,hwnd)
    moved=bool(user.SetCursorPos(*point));error=c.get_last_error() if not moved and hasattr(c,'get_last_error') else 0
    record['set_cursor_result']=dict(point=list(point),success=moved,last_error=error,perf=time.perf_counter())
    record['cursor_after_set']=cursor_snapshot(user,hwnd)
    cursor=w.POINT();ok=bool(user.GetCursorPos(c.byref(cursor)))
    record['checked_cursor']=dict(success=ok,point=[cursor.x,cursor.y] if ok else None,perf=time.perf_counter())
    if not moved:raise OSError(error,'SetCursorPos failed')
    if not ok or (cursor.x,cursor.y)!=point:
        raise UnexpectedInput('Cursor changed before planned injection')
    return cursor


def assert_planned_events(events, point):
    # SetCursorPos may yield intermediate drag motion within this small rectangle.
    x,y=point
    for event in events:
        if not (x<=event['x']<=x+8 and y-4<=event['y']<=y):
            raise UnexpectedInput('Input outside planned coordinates: '+str(event))


def assert_activation_idle(user, thread_ids):
    """Reject held buttons, capture or modal menu/move state; never release it."""
    if any(user.GetAsyncKeyState(key)&0x8000 for key in (1,2,4,5,6)):
        raise UnexpectedInput('Mouse button held before owned setup activation')
    states=[]
    for thread_id in thread_ids:
        info=GUIThreadInfo();info.size=c.sizeof(GUIThreadInfo)
        if not user.GetGUIThreadInfo(thread_id,c.byref(info)):
            raise RuntimeError('Cannot confirm GUI thread is idle; activation refused')
        state={'thread_id':thread_id,'flags':info.flags,'capture':int(info.capture or 0),
               'menu_owner':int(info.menu_owner or 0),'move_size':int(info.move_size or 0)}
        states.append(state)
        if info.capture or info.menu_owner or info.move_size or info.flags&0x1E:
            raise UnexpectedInput('Capture/menu/move state active; owned setup activation refused')
    return states


def assert_movement_state(user,thread_ids,allowed_pids,held,record):
    """Apply the existing input-safety conditions before every planned move."""
    allowed_keys=({1} if 0x2 in held else set())|({2} if 0x8 in held else set())
    pressed=[key for key in (1,2,4,5,6) if user.GetAsyncKeyState(key)&0x8000]
    record.update(pressed_mouse_keys=pressed,own_held=sorted(held),gui_threads=[])
    if any(key not in allowed_keys for key in pressed):
        raise UnexpectedInput('Unplanned held mouse button before owned movement')
    for thread_id in thread_ids:
        info=GUIThreadInfo();info.size=c.sizeof(info)
        ok=bool(user.GetGUIThreadInfo(thread_id,c.byref(info)))
        state=dict(thread_id=thread_id,success=ok,flags=info.flags,capture=int(info.capture or 0),
                   menu_owner=int(info.menu_owner or 0),move_size=int(info.move_size or 0))
        record['gui_threads'].append(state)
        if not ok:raise RuntimeError('Cannot confirm GUI state before owned movement')
        if info.menu_owner or info.move_size or info.flags&0x1E:
            raise UnexpectedInput('Menu/move state active before owned movement')
        if info.capture:
            pid=w.DWORD();owner_thread=user.GetWindowThreadProcessId(info.capture,c.byref(pid))
            state.update(capture_pid=pid.value,capture_thread=int(owner_thread))
            if not owner_thread or pid.value not in allowed_pids:
                raise UnexpectedInput('Foreign/unknown capture before owned movement')


def owned_client_center(user,kernel,identity):
    assert_identity(user,kernel,identity)
    if not user.IsWindowVisible(identity['hwnd']):
        raise RuntimeError('Owned setup counter is not visible')
    rect=w.RECT();origin=w.POINT(0,0)
    if not (user.GetClientRect(identity['hwnd'],c.byref(rect))
            and user.ClientToScreen(identity['hwnd'],c.byref(origin))):
        raise RuntimeError('Owned setup client rectangle unavailable')
    if rect.right-rect.left<64 or rect.bottom-rect.top<64:
        raise RuntimeError('Owned setup client rectangle too small')
    point=(origin.x+(rect.left+rect.right)//2,origin.y+(rect.top+rect.bottom)//2)
    hit=user.GetAncestor(user.WindowFromPoint(w.POINT(*point)),2)
    if hit!=identity['hwnd']:
        raise RuntimeError('Owned setup point does not hit the counter; input refused')
    return point


def foreground_snapshot(user,kernel):
    """Observe actual foreground identity; zero/own Tk hosts are not other apps."""
    hwnd=int(user.GetForegroundWindow() or 0)
    result={'hwnd':hwnd,'valid':bool(hwnd and user.IsWindow(hwnd)),'pid':0,'thread_id':0,
            'title':'','class':'','visible':False,'gui_threads':[]}
    if result['valid']:
        pid=w.DWORD()
        result['thread_id']=int(user.GetWindowThreadProcessId(hwnd,c.byref(pid)))
        result['pid']=pid.value
        title=c.create_unicode_buffer(512);name=c.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd,title,len(title));user.GetClassNameW(hwnd,name,len(name))
        result.update(title=title.value,**{'class':name.value},visible=bool(user.IsWindowVisible(hwnd)))
        try:
            result['created_filetime']=process_created(kernel,pid.value)
        except OSError as error:
            result['creation_query_error']=str(error)
    for thread_id in (0,threading.get_native_id()):
        info=GUIThreadInfo();info.size=c.sizeof(GUIThreadInfo)
        ok=bool(user.GetGUIThreadInfo(thread_id,c.byref(info)))
        observation={'thread_id':thread_id,'success':ok,'flags':info.flags,
            'active':int(info.active or 0),'focus':int(info.focus or 0),'capture':int(info.capture or 0),
            'menu_owner':int(info.menu_owner or 0),'move_size':int(info.move_size or 0)}
        if info.capture:
            owner_pid=w.DWORD();owner_thread=user.GetWindowThreadProcessId(info.capture,c.byref(owner_pid))
            observation['capture_identity']={'hwnd':int(info.capture),'pid':owner_pid.value,
                'thread_id':int(owner_thread),'observed_only':True}
        result['gui_threads'].append(observation)
    result['cursor']=cursor_snapshot(user,hwnd or None)
    return result


def fixed_pixel_alpha(image, local, kind):
    """Verify the originally selected pixel; never pick a new glyph median."""
    x,y=local
    if not (0<=x<image.size[0] and 0<=y<image.size[1]):
        raise RuntimeError('Fixed alpha point is outside the current image')
    alpha=image.getpixel((x,y))[3]
    if (kind=='opaque' and alpha!=255) or (kind=='glyph' and alpha<128) or (kind=='resize' and alpha<=0):
        raise RuntimeError('Originally selected alpha point no longer matches '+kind)
    return alpha


def restore_reading_fixture(overlay, fixture):
    """Reset only this tool's synthetic-content reading state after a gesture."""
    overlay._follow_tail=fixture['follow_tail']
    overlay._scroll_target=fixture['source_target']
    overlay._trans_scroll_target=fixture['translation_target']
    overlay._scroll_motion.snap(fixture['source_target'])
    overlay._trans_scroll_motion.snap(fixture['translation_target'])
    overlay._scrollbar_show_until=0.0


def counter_process(connection, token, geometry, seconds):
    import tkinter as tk
    user, kernel = native_api()
    root = tk.Tk()
    root.title('Mouse counter ' + token)
    root.geometry(geometry)
    canvas = tk.Canvas(root, background='#244658', highlightthickness=0)
    canvas.pack(fill='both', expand=True)
    canvas.create_text(25, 25, anchor='nw', fill='white', text='Owned cross-process input counter')
    events = deque(maxlen=1024)
    count = [0]
    def record(kind, event):
        count[0] += 1
        events.append({'kind': kind, 'perf': time.perf_counter(), 'x': event.x_root, 'y': event.y_root,
                       'delta': getattr(event, 'delta', 0), 'number': count[0],
                       'latest_native_message_number':observation.total if observation is not None else None,
                       'cursor_at_callback':cursor_snapshot(user,user.GetAncestor(root.winfo_id(),2)),
                       'cursor_meaning':'Current shared state at Tk callback, not input-origin attribution'})
    for sequence, kind in [('<ButtonPress-1>', 'left'), ('<ButtonRelease-1>', 'release'),
                           ('<Double-Button-1>', 'double'), ('<ButtonPress-3>', 'right'),
                           ('<B1-Motion>', 'drag'), ('<MouseWheel>', 'wheel')]:
        root.bind_all(sequence, lambda event, kind=kind: record(kind, event))
    deadline = time.monotonic() + seconds
    observation=None
    try:
        root.update()
        hwnd = user.GetAncestor(root.winfo_id(), 2)
        identity=window_identity(user,kernel,hwnd,token,os.getpid())
        observation=NativeMessageObserver(user,kernel,identity,
            {'wrapper':int(hwnd),'client':int(root.winfo_id()),'canvas':int(canvas.winfo_id())})
        connection.send({'kind': 'ready', 'identity': identity,
                         'native_observation':observation.snapshot(),
                         'environment':cursor_snapshot(user,hwnd)})
        while time.monotonic() < deadline:
            root.update()
            while connection.poll():
                command = connection.recv()
                if command == 'stop':
                    return
                if command == 'clear':
                    events.clear(); count[0] = 0
                    observation.clear()
                elif command != 'snapshot':
                    raise RuntimeError('Unknown counter command')
                connection.send({'kind': command, 'events': list(events), 'total': count[0],
                                 'native_observation':observation.snapshot()})
            time.sleep(.005)
    finally:
        cleanup=[]
        if observation is not None:
            try:observation.close()
            except Exception as error:cleanup.append(str(error))
        try:root.destroy()
        except Exception as error:cleanup.append(str(error))
        status=observation.snapshot() if observation is not None else None
        if status is not None:status.pop('events')  # Small ack: parent joins before reading it.
        try:connection.send({'kind':'stopped','native_cleanup_errors':cleanup,'native_observation':status})
        except (BrokenPipeError,EOFError,OSError):pass
        connection.close()


def run(args):
    if os.name != 'nt':
        raise RuntimeError('Native mouse routing acceptance requires Windows')
    source = args.source.resolve(strict=True)
    if not source.is_dir() or not (source / 'overlay.py').is_file():
        raise ValueError('--source must be the source repository')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    # No Settings.load/save and no Application/ASR/translation imports.
    sys.path.insert(0, str(source))
    if args.candidate:
        candidate = args.candidate.resolve(strict=True)
        for name in ('overlay.py', 'layered_window.py'):
            if not (candidate / name).is_file():
                raise ValueError('--candidate requires both reviewed native files')
        sys.path.insert(0, str(candidate))
    import tkinter as tk
    from overlay import OverlayWindow
    from settings import Settings
    import overlay as loaded_overlay
    import layered_window as loaded_native
    user, kernel = native_api()
    token = uuid.uuid4().hex
    report = {'success': False, 'run_token': token, 'started_epoch': time.time(), 'steps': [], 'states': [], 'actions': [], 'phases': [], 'menu_cancellations': [], 'focus_attempts': [], 'setup_activation': None, 'focus_records': [],
              'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source_files': {str(Path(module.__file__).resolve()): hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                               for module in (loaded_overlay, loaded_native)},
              'scope': 'owned overlay + other-process owned counter, synthetic text, no audio or saved settings'}
    root = None
    overlay = None
    process = None
    parent = None
    child = None
    tk_thread_id=threading.get_native_id()
    menu_timer=None
    setup_activation_used=False
    overlay_events = []
    native_observer=None
    context={"kind":"setup","mode":"setup","gesture":"setup"}
    deadline = time.monotonic() + args.seconds
    def save_progress():
        temporary=output/"progress.tmp"
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        os.replace(temporary,output/"progress.json")
    def record_focus(stage):
        record=dict(stage=stage,perf=time.perf_counter(),context=dict(context),
                    actual=foreground_snapshot(user,kernel))
        report['focus_records'].append(record)
        return record
    def pump(seconds=.10):
        end = min(deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            root.update()
            time.sleep(.005)
        if time.monotonic() >= deadline:
            raise TimeoutError('Native acceptance time budget exhausted')
    def receive(expected):
        while not parent.poll(.02):
            if not process.is_alive():
                raise RuntimeError('Counter process exited')
            pump(.02)
        message = parent.recv()
        if message.get('kind') != expected:
            raise RuntimeError('Counter response mismatch')
        return message
    def query(command):
        parent.send(command)
        result=receive(command)
        if 'native_observation' in result:
            require_complete_native_observation(result['native_observation'])
        if native_observer is not None:
            require_complete_native_observation(native_observer.snapshot())
        return result
    def own_focus(identity, *, initial_setup=False):
        nonlocal setup_activation_used
        assert_identity(user,kernel,identity)
        attempt={'perf':time.perf_counter(),'hwnd':identity['hwnd'],
                 'before':int(user.GetForegroundWindow() or 0),'initial_setup':initial_setup}
        record_focus('before_SetForegroundWindow')
        user.SetForegroundWindow(identity['hwnd'])
        pump()
        record_focus('after_SetForegroundWindow')
        attempt['after_api']=int(user.GetForegroundWindow() or 0)
        report['focus_attempts'].append(attempt)
        if user.GetForegroundWindow()!=identity['hwnd'] and initial_setup and not setup_activation_used:
            setup_activation_used=True
            pid=w.DWORD()
            counter_thread=user.GetWindowThreadProcessId(identity['hwnd'],c.byref(pid))
            states=assert_activation_idle(user,(0,tk_thread_id,counter_thread))
            assert_identity(user,kernel,identity)
            if not user.IsWindowVisible(identity['hwnd']):
                raise RuntimeError('Owned setup counter not visible')
            c.set_last_error(0)
            style=user.GetWindowLongW(identity['hwnd'],-20)
            if not style and c.get_last_error():
                raise OSError(c.get_last_error(),'Owned setup style read failed')
            was_topmost=bool(style&8)
            activation={'attempt':1,'identity':identity,'states':states,'was_topmost':was_topmost,
                        'planned':True,'phase':'prepared'}
            report['setup_activation']=activation;save_progress()
            raised=False
            try:
                if not user.SetWindowPos(identity['hwnd'],-1,0,0,0,0,0x213):
                    raise OSError(c.get_last_error(),'Owned setup counter raise failed')
                raised=True
                assert_activation_idle(user,(0,tk_thread_id,counter_thread))
                point=owned_client_center(user,kernel,identity)
                cursor=checked_move(user,point,activation,identity['hwnd'])
                if owned_client_center(user,kernel,identity)!=point:
                    raise UnexpectedInput('Owned setup counter moved before click')
                activation['final_states']=assert_activation_idle(user,(0,tk_thread_id,counter_thread))
                activation.update(point=point,phase='injecting',inject_perf=time.perf_counter())
                save_progress()
                pair=(Input*2)(Input(type=0,value=Union(mouse=Mouse(0,0,0,0x2,0,0))),
                               Input(type=0,value=Union(mouse=Mouse(0,0,0,0x4,0,0))))
                inserted=user.SendInput(2,pair,c.sizeof(Input))
                activation['inserted']=inserted
                if inserted!=2:
                    if inserted==1: held.add(0x2)
                    raise OSError(c.get_last_error(),'Owned setup activation click failed')
                pump(.15)
                activation.update(phase='completed',foreground=int(user.GetForegroundWindow() or 0))
            finally:
                if raised:
                    assert_identity(user,kernel,identity)
                    if not user.SetWindowPos(identity['hwnd'],-1 if was_topmost else -2,0,0,0,0,0x213):
                        raise OSError(c.get_last_error(),'Owned setup topmost restore failed')
                    c.set_last_error(0)
                    restored=user.GetWindowLongW(identity['hwnd'],-20)
                    if (not restored and c.get_last_error()) or bool(restored&8)!=was_topmost:
                        raise RuntimeError('Owned setup topmost restore readback failed')
                    activation['restored_topmost']=bool(restored&8)
            save_progress()
            if user.GetForegroundWindow()!=identity['hwnd']:
                raise RuntimeError('Single owned setup click did not activate counter')
            activation['events']=query('snapshot')
            assert_planned_events(activation['events']['events'],point)
            query('clear');save_progress()
        if user.GetForegroundWindow()!=identity['hwnd']:
            raise RuntimeError('Could not foreground the owned counter; no further input sent')
    def verify_target(point):
        record_focus("before_verify_target")
        assert_identity(user, kernel, overlay_identity)
        assert_identity(user, kernel, sink_identity)
        hit = user.GetAncestor(user.WindowFromPoint(w.POINT(*point)), 2)
        owned = (overlay_identity['hwnd'], sink_identity['hwnd'])
        if user.GetForegroundWindow() not in owned:
            record_focus('verify_target_foreground_rejected')
            raise RuntimeError('Foreground HWND is outside verified input targets; input refused')
        if not user.IsWindowVisible(overlay_identity['hwnd']) or not user.IsWindowVisible(sink_identity['hwnd']):
            raise RuntimeError('Owned test windows must both be visible')
        if hit not in owned:
            raise RuntimeError('Another window covers the owned test point; input refused')
        guard=dict(context,perf=time.perf_counter(),planned_point=list(point))
        report.setdefault('movement_guards',[]).append(guard)
        counter_pid=w.DWORD();counter_thread=user.GetWindowThreadProcessId(sink_identity['hwnd'],c.byref(counter_pid))
        if not counter_thread or counter_pid.value!=sink_identity['pid']:
            raise RuntimeError('Owned counter GUI thread query failed before movement')
        assert_movement_state(user,(0,tk_thread_id,int(counter_thread)),
                              {overlay_identity['pid'],sink_identity['pid']},held,guard)
    class Mouse(c.Structure):
        _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('data', w.DWORD), ('flags', w.DWORD),
                    ('time', w.DWORD), ('extra', c.c_size_t)]
    class Union(c.Union):
        _fields_ = [('mouse', Mouse)]
    class Input(c.Structure):
        _anonymous_ = ('value',)
        _fields_ = [('type', w.DWORD), ('value', Union)]
    user.SendInput.argtypes, user.SendInput.restype = [w.UINT, c.POINTER(Input), c.c_int], w.UINT
    held = set()
    def action_snapshot(point):
        box=w.RECT();client=w.RECT();origin=w.POINT(0,0)
        if not (user.GetWindowRect(overlay_identity['hwnd'],c.byref(box))
                and user.GetClientRect(overlay_identity['hwnd'],c.byref(client))
                and user.ClientToScreen(overlay_identity['hwnd'],c.byref(origin))):
            raise RuntimeError('Owned input geometry query failed')
        local=(point[0]-overlay.window.winfo_rootx(),point[1]-overlay.window.winfo_rooty())
        image=overlay._last_image
        alpha=image.getpixel(local)[3] if 0<=local[0]<image.width and 0<=local[1]<image.height else None
        result=dict(context,perf=time.perf_counter(),point=point,local=local,alpha=alpha,
                    window_rect=(box.left,box.top,box.right,box.bottom),
                    client_rect=(origin.x,origin.y,origin.x+client.right,origin.y+client.bottom),
                    tk_geometry=overlay.window.geometry(),image_size=image.size,
                    source_value=overlay._scroll_motion.value,source_target=overlay._scroll_target,
                    source_velocity=overlay._scroll_motion.velocity,follow_tail=overlay._follow_tail,
                    product_drag_mode=getattr(overlay,'_drag_mode',None),
                    tk_target=tk_input_target_snapshot(root,overlay,point))
        report['actions'].append(result)
        return result
    def send(point, flags, data=0):
        action=action_snapshot(point)
        verify_target(point)
        cursor=checked_move(user,point,action,overlay_identity['hwnd'])
        report['actions'][-1].update(flags=flags,injected_point=(cursor.x,cursor.y),inject_perf=time.perf_counter())
        value = Input(type=0, value=Union(mouse=Mouse(0, 0, data, flags, 0, 0)))
        inserted=user.SendInput(1, c.byref(value), c.sizeof(Input))
        action.update(sendinput_requested=1,sendinput_inserted=int(inserted),sendinput_return_perf=time.perf_counter())
        if inserted != 1:
            raise OSError(c.get_last_error(), 'SendInput failed')
        if flags in (0x2, 0x8):
            held.add(flags)
        if flags in (0x4, 0x10):
            held.discard(0x2 if flags == 0x4 else 0x8)
        pump(.035)
    def click(point):
        send(point, 0x2); send(point, 0x4)
    def exercise(point, reset=None):
        context["gesture"]="left"
        click(point)
        if reset: reset()
        context["gesture"]="right"
        nonlocal menu_timer
        cancellation={'scheduled_perf':time.perf_counter(),'mode':context['mode'],'fired':False}
        if context['mode']!='on':
            def cancel_menu():
                nonlocal menu_timer
                menu_timer=None
                if threading.get_native_id()!=tk_thread_id:
                    raise RuntimeError('Menu cancellation must stay on the Tk thread')
                assert_identity(user,kernel,overlay_identity)
                # Both calls run on this tool's Tk thread; no wrapper message or key.
                overlay._menu.unpost()
                cancellation.update(fired=True,perf=time.perf_counter(),end_menu=bool(user.EndMenu()),thread_id=tk_thread_id)
            menu_timer=root.after(90,cancel_menu)
            report['menu_cancellations'].append(cancellation)
        # Batch right down/up before pumping a native modal menu, so no button
        # remains held if popup processing blocks the Tk event loop.
        action=action_snapshot(point);verify_target(point)
        cursor=checked_move(user,point,action,overlay_identity['hwnd'])
        pair=(Input*2)(Input(type=0,value=Union(mouse=Mouse(0,0,0,0x8,0,0))),
                       Input(type=0,value=Union(mouse=Mouse(0,0,0,0x10,0,0))))
        report['actions'][-1].update(flags=[0x8,0x10],injected_point=point,inject_perf=time.perf_counter())
        inserted=user.SendInput(2,pair,c.sizeof(Input))
        action.update(sendinput_requested=2,sendinput_inserted=int(inserted),sendinput_return_perf=time.perf_counter())
        if inserted!=2:
            if inserted==1: held.add(0x8)
            raise OSError(c.get_last_error(),'SendInput right pair failed')
        pump(.18)
        if context['mode']!='on' and (not cancellation['fired'] or cancellation['perf']-cancellation['scheduled_perf']>.5):
            raise RuntimeError('Owned menu cancellation did not complete within 500 ms')
        if reset: reset()
        pump(.08)
        context["gesture"]="double"
        click(point); click(point)
        if reset: reset()
        interval=int(user.GetDoubleClickTime())
        wait=independent_gesture_gap(interval)
        gap=dict(context,perf=time.perf_counter(),system_double_click_ms=interval,wait_seconds=wait,
                 tk_8_6_nearby_ms=500,
                 reason='next drag is an independent gesture, not another double-click continuation')
        report.setdefault('independent_gesture_gaps',[]).append(gap)
        pump(wait)
        gap['finished_perf']=time.perf_counter()
        context["gesture"]="drag"
        send(point, 0x2)
        shifted = (point[0] + 8, point[1] - 4)
        verify_target(shifted)
        movement=action_snapshot(shifted);movement['operation']='planned drag SetCursorPos only; no SendInput'
        checked_move(user,shifted,movement,overlay_identity['hwnd'])
        pump(.08)
        send(shifted, 0x4)
        if reset: reset()
        context["gesture"]="wheel"
        send(point, 0x800, 120)
        if reset: reset()
        pump(.12)
    def alpha_point(kind):
        overlay._redraw()
        image = overlay._last_image
        width, height = image.size
        if kind == 'glyph':
            area = (20, 35, width - 70, height - 35)
            pixels = [(x, y) for y in range(area[1], area[3]) for x in range(area[0], area[2])
                      if image.getpixel((x, y))[3] >= 128]
            if not pixels:
                raise RuntimeError('No opaque text pixel drawn')
            x, y = pixels[len(pixels)//2]
        elif kind == 'resize':
            pixels = [(x, y) for y in range(height-24, height-8) for x in range(width-24, width-8)
                      if image.getpixel((x, y))[3] > 0]
            if not pixels:
                raise RuntimeError('No nonzero-alpha resize pixel drawn')
            x, y = pixels[len(pixels)//2]
        else:
            x, y = 40, height-40
            if image.getpixel((x, y))[3] != 255:
                raise RuntimeError('Background acceptance point is not opaque')
        return (overlay.window.winfo_rootx()+x, overlay.window.winfo_rooty()+y), image.getpixel((x,y))[3]
    try:
        if any(user.GetAsyncKeyState(key) & 0x8000 for key in (1,2,4)):
            raise RuntimeError('A mouse button is already held; no input sent')
        root = tk.Tk(); root.withdraw()
        report['tk_version']=str(root.tk.call('package','present','Tk'))
        report['tcl_version']=str(root.tk.call('info','patchlevel'))
        report['input_struct_size']=c.sizeof(Input)
        if root.winfo_screenwidth() < 850 or root.winfo_screenheight() < 650:
            raise RuntimeError('Desktop too small for bounded owned test windows')
        ctx = mp.get_context('spawn')
        parent, child = ctx.Pipe()
        process = ctx.Process(target=counter_process, args=(child, token, '820x560+20+40', args.seconds+5))
        process.start(); child.close()
        ready=receive('ready');sink_identity=ready['identity']
        report['counter_environment']=ready.get('environment')
        report['counter_native_observation_initial']=ready['native_observation']
        require_complete_native_observation(ready['native_observation'])
        if sink_identity['pid'] == os.getpid():
            raise RuntimeError('Counter must be a different process')
        assert_identity(user, kernel, sink_identity)
        report['counter']=sink_identity;record_focus('counter_ready');save_progress()
        own_focus(sink_identity,initial_setup=True)
        settings = Settings(overlay_geometry='680x240+60+100', overlay_auto_height=False,
                            overlay_background_opacity=0.0, overlay_text_opacity=1.0,
                            overlay_font_size=28, overlay_translation_font_size=21)
        record_focus('before_overlay_constructor')
        overlay = OverlayWindow(root, settings, lambda: None, lambda: None)
        record_focus('after_overlay_constructor')
        overlay.window.title('Overlay passthrough ' + token)
        overlay.on_mouse_passthrough_changed = lambda result: report['states'].append(asdict(result))
        overlay.set_content('Synthetic caption for mouse routing.', 'Synthetic translated caption.', 'Source', 'Translation')
        initial = overlay.set_mouse_passthrough(False)
        if not initial.success or initial.effective is not False:
            raise RuntimeError('Initial normal mode failed')
        record_focus('before_overlay_show')
        overlay.show();record_focus('after_overlay_show')
        pump(.2);record_focus('after_overlay_show_pump')
        overlay_identity = window_identity(user, kernel, overlay._layer._hwnd, token, os.getpid())
        native_observer=NativeMessageObserver(user,kernel,overlay_identity,
            {'wrapper':overlay_identity['hwnd'],'client':int(overlay.window.winfo_id()),
             'canvas':int(overlay.canvas.winfo_id())},context)
        report.update(overlay=overlay_identity, counter=sink_identity)
        report['native_observation_initial']=native_observer.snapshot()
        report['overlay_environment']=cursor_snapshot(user,overlay_identity['hwnd'])
        for sequence,event_kind in [('<ButtonPress-1>','left'),('<ButtonRelease-1>','release'),
                                    ('<Double-Button-1>','double'),('<ButtonPress-3>','right'),
                                    ('<B1-Motion>','drag'),('<MouseWheel>','wheel')]:
            root.bind_all(sequence,lambda event,kind=event_kind:overlay_events.append(
                {'kind':kind,'perf':time.perf_counter(),'x':event.x_root,'y':event.y_root,
                 'latest_native_message_number':native_observer.total,
                 'cursor_at_callback':cursor_snapshot(user,overlay_identity['hwnd']),
                 'tk_target':tk_input_target_snapshot(root,overlay,(event.x_root,event.y_root),event),
                 'cursor_meaning':'Current shared state at Tk callback, not input-origin attribution'}))
        from screen_bounds import parse_geometry
        fixed_rect=overlay._current_rect()
        fixed_cap_height=overlay._cap_height
        reading_fixture={'follow_tail':overlay._follow_tail,'source_target':overlay._scroll_target,
                         'translation_target':overlay._trans_scroll_target}
        restore_reading_fixture(overlay,reading_fixture)
        report['fixed_fixture']={'rect':asdict(fixed_rect),'cap_height':fixed_cap_height,
                                 'reading':reading_fixture}
        required={'left','release','right','double','drag','wheel'}
        for kind in ('glyph', 'resize', 'opaque'):
            if kind == 'opaque':
                settings.overlay_background_opacity = 1.0
                overlay.apply_settings(); pump()
            point, alpha = alpha_point(kind)
            local=(point[0]-overlay.window.winfo_rootx(),point[1]-overlay.window.winfo_rooty())
            context.update(kind=kind,mode='off',gesture='setup')
            reference=action_snapshot(point)
            off = overlay.set_mouse_passthrough(False)
            if not off.success or off.effective is not False:
                raise RuntimeError('Normal mode not confirmed')
            def reset_geometry():
                overlay._menu.unpost()
                overlay._release_owned_pointer()
                overlay._drag_mode=''
                overlay._cap_height=fixed_cap_height
                restore_reading_fixture(overlay,reading_fixture)
                overlay._set_bounded_geometry(fixed_rect)
                overlay.window.update_idletasks()
                overlay._redraw(); pump(.05)
                current=action_snapshot(point)
                if tuple(current['local'])!=local or current['window_rect']!=reference['window_rect']:
                    raise RuntimeError('Owned fixed test geometry was not restored')
                fixed_pixel_alpha(overlay._last_image,local,kind)
            query('clear'); overlay_events.clear(); native_observer.clear(); action_start=len(report['actions'])
            exercise(point,reset_geometry)
            blocked = query('snapshot')
            blocked_overlay=list(overlay_events)
            phase=dict(kind=kind,mode='off',point=point,alpha=alpha,counter=blocked,overlay=blocked_overlay,
                       overlay_native=native_observer.snapshot())
            report['phases'].append(phase)
            save_progress()
            assert_planned_events(blocked_overlay,point);assert_planned_events(blocked['events'],point)
            if not required <= {event['kind'] for event in blocked_overlay}:
                raise RuntimeError(kind + ': normal overlay input set incomplete')
            phase['blocked_native_validation']=classify_blocked_receiver(blocked['events'],blocked['native_observation'])
            own_focus(sink_identity)
            foreground = user.GetForegroundWindow()
            enabled = overlay.set_mouse_passthrough(True)
            pump()
            if not enabled.success or enabled.effective is not True or user.GetForegroundWindow() != foreground:
                raise RuntimeError('Enabling passthrough failed or stole focus')
            context.update(mode='on',gesture='setup')
            fixed_pixel_alpha(overlay._last_image,local,kind)
            query('clear'); overlay_events.clear(); native_observer.clear(); action_start=len(report['actions'])
            exercise(point)
            passed = query('snapshot')
            phase=dict(kind=kind,mode='on',point=point,alpha=alpha,counter=passed,overlay=list(overlay_events),
                       overlay_native=native_observer.snapshot())
            report['phases'].append(phase)
            save_progress()
            assert_planned_events(overlay_events,point);assert_planned_events(passed['events'],point)
            kinds = {event['kind'] for event in passed['events']}
            if not required <= kinds:
                raise RuntimeError(kind + ': lower-process input set incomplete: ' + str(sorted(kinds)))
            phase['receiving_native_validation']=require_native_delivery(passed['events'],passed['native_observation'],report['actions'][action_start:])
            phase['blocked_native_validation']=require_blocked_overlay(overlay_events,phase['overlay_native'])
            if user.GetForegroundWindow() != sink_identity['hwnd']:
                raise RuntimeError('Passthrough input changed focus away from lower process')
            disabled = overlay.set_mouse_passthrough(False)
            if not disabled.success or disabled.effective is not False:
                raise RuntimeError('External restore to normal mode failed')
            context.update(mode='restored',gesture='setup')
            query('clear'); overlay_events.clear(); native_observer.clear(); action_start=len(report['actions'])
            exercise(point,reset_geometry)
            restored = query('snapshot')
            restored_overlay=list(overlay_events)
            phase=dict(kind=kind,mode='restored',point=point,alpha=alpha,counter=restored,overlay=restored_overlay,
                       overlay_native=native_observer.snapshot())
            report['phases'].append(phase)
            save_progress()
            assert_planned_events(restored_overlay,point);assert_planned_events(restored['events'],point)
            if not required <= {event['kind'] for event in restored_overlay}:
                raise RuntimeError(kind + ': restored overlay input set incomplete')
            phase['blocked_native_validation']=classify_blocked_receiver(restored['events'],restored['native_observation'])
            report['steps'].append({'kind':kind, 'point':point, 'alpha':alpha, 'off':blocked,
                                    'on':passed, 'restored':restored, 'enabled':asdict(enabled),
                                    'disabled':asdict(disabled),'off_overlay':blocked_overlay,
                                    'restored_overlay':restored_overlay})
        context.update(kind='lifecycle',mode='on',gesture='lifecycle')
        own_focus(sink_identity)
        overlay.set_mouse_passthrough(True)
        foreground = user.GetForegroundWindow()
        overlay.hide(); pump(); overlay.show(); pump(.15)
        for topmost in (False, True):
            settings.overlay_topmost = topmost
            settings.overlay_background_opacity = .45
            overlay.apply_settings(); pump()
            for _ in range(5):
                overlay._redraw(); pump(.02)
            if overlay.mouse_passthrough is not True or user.GetForegroundWindow() != foreground:
                raise RuntimeError('Lifecycle/present lost passthrough or foreground')
        point, _ = alpha_point('opaque') if settings.overlay_background_opacity == 1 else alpha_point('resize')
        query('clear'); native_observer.clear(); action_start=len(report['actions'])
        context['gesture']='left';click(point)
        context['gesture']='wheel';send(point,0x800,120);pump(.10)
        lifecycle = query('snapshot')
        if not {'left','wheel'} <= {event['kind'] for event in lifecycle['events']}:
            raise RuntimeError('Lifecycle input did not reach lower process')
        lifecycle['receiving_native_validation']=require_native_delivery(lifecycle['events'],lifecycle['native_observation'],report['actions'][action_start:],full_matrix=False)
        report['lifecycle'] = {'foreground_unchanged':True, 'events':lifecycle,
                               'passthrough':overlay.mouse_passthrough}
        # Validate the capture rectangle inside the owned counter's client area.
        def owned_capture_rect():
            assert_identity(user,kernel,overlay_identity)
            assert_identity(user,kernel,sink_identity)
            outer=w.RECT(); client=w.RECT(); origin=w.POINT(0,0)
            if not (user.GetWindowRect(overlay_identity['hwnd'],c.byref(outer))
                    and user.GetClientRect(sink_identity['hwnd'],c.byref(client))
                    and user.ClientToScreen(sink_identity['hwnd'],c.byref(origin))):
                raise RuntimeError('Owned capture rectangle query failed')
            box=(outer.left,outer.top,outer.right,outer.bottom)
            inside=(origin.x+client.left,origin.y+client.top,origin.x+client.right,origin.y+client.bottom)
            if not (inside[0]<=box[0]<box[2]<=inside[2] and inside[1]<=box[1]<box[3]<=inside[3]):
                raise RuntimeError('Capture would include unrelated desktop pixels')
            return box
        capture_rect=owned_capture_rect()
        sys.path.insert(0,str(source/'tools'))
        from verify_installed_runtime import capture_visible_window
        capture_visible_window({'hwnd':overlay_identity['hwnd']},output/'owned-passthrough.png')
        if owned_capture_rect()!=capture_rect:
            raise RuntimeError('Owned window moved during capture')
        report['capture_rect']=capture_rect
        report['held_transitions']=[]
        for operation in ('enable','hide','close'):
            context.update(kind='held_transition',mode='off',gesture=operation)
            normal=overlay.set_mouse_passthrough(False)
            if not normal.success or normal.effective is not False:
                raise RuntimeError('Could not prepare normal mode for held transition')
            overlay.show();pump(.10);own_focus(sink_identity)
            point,_=alpha_point('glyph')
            query('clear');overlay_events.clear();native_observer.clear()
            send(point,0x2)
            captured=int(user.GetCapture() or 0)
            if not overlay._drag_mode or captured not in native_observer.bindings:
                raise RuntimeError('Held transition did not start with owned button capture')
            check=dict(operation=operation,point=point,before_capture=captured,
                       before_drag_mode=overlay._drag_mode,started_perf=time.perf_counter())
            report['held_transitions'].append(check)
            if operation=='enable':
                result=overlay.set_mouse_passthrough(True);check['result']=asdict(result)
                if not result.success or result.effective is not True:
                    raise RuntimeError('Enabling while held failed')
            elif operation=='hide':overlay.hide()
            else:overlay.close()
            check.update(after_perf=time.perf_counter(),after_capture=int(user.GetCapture() or 0),
                         after_drag_mode=overlay._drag_mode,window_exists=bool(overlay.window.winfo_exists()))
            context['mode']='after_'+operation
            if check['after_capture'] or check['after_drag_mode']:
                raise RuntimeError('Held transition retained pointer capture/drag state')
            if operation=='hide':
                if overlay.window.winfo_viewable():raise RuntimeError('Held hide left overlay visible')
                check['hidden_confirmed']=True
                overlay.show();pump(.10)  # Restore visibility before the unchanged send guard.
            elif operation=='close':
                if check['window_exists']:raise RuntimeError('Held close left overlay alive')
                check['release_plan']='Existing finally releases only this tool-owned held button'
                report['intentional_held_close']=True
                break
            send(point,0x4)
            if user.GetCapture():raise RuntimeError('Held transition release retained native capture')
            check['counter_after_release']=query('snapshot')
            check['overlay_native']=native_observer.snapshot()
            check['overlay_callbacks']=list(overlay_events)
        report['success'] = True
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        record_focus('exception_before_root_probe')
        if root is not None:
            try:
                client=root.winfo_id()
                report['failure_tk_root']={'client_hwnd':int(client),'ancestor_hwnd':int(user.GetAncestor(client,2) or 0),'logical_title':root.title(),'pid':os.getpid(),'resolved_after_failure':True}
                record_focus('exception_after_root_probe')
            except Exception as probe_error:
                report['failure_tk_root_error']=str(probe_error)
        report['contaminated']=isinstance(error,UnexpectedInput)
        report['failure_context']=dict(context)
        report['failure_overlay_events']=list(overlay_events)
        if parent is not None and process is not None and process.is_alive():
            try:
                parent.send('snapshot')
                if parent.poll(.3):
                    report['failure_counter_snapshot']=parent.recv()
            except (BrokenPipeError,EOFError,OSError):
                pass
    finally:
        cleanup_errors = []
        if menu_timer is not None and root is not None:
            try:
                root.after_cancel(menu_timer)
            except Exception as error:
                cleanup_errors.append(str(error))
        # Release only buttons this tool injected; do not leave one held after abort.
        report['held_before_cleanup']=sorted(held);report['held_cleanup']=[]
        for down in held:
            try:
                value = Input(type=0, value=Union(mouse=Mouse(0,0,0,0x4 if down == 0x2 else 0x10,0,0)))
                cleanup=dict(down=down,release_flag=0x4 if down==0x2 else 0x10,
                             cursor=cursor_snapshot(user),perf=time.perf_counter())
                report['held_cleanup'].append(cleanup)
                inserted=user.SendInput(1,c.byref(value),c.sizeof(Input))
                cleanup.update(sendinput_inserted=int(inserted),return_perf=time.perf_counter())
                if inserted != 1:
                    cleanup_errors.append('held mouse button release failed')
            except Exception as error:
                cleanup_errors.append(str(error))
        if native_observer is not None:
            report['native_observation']=native_observer.snapshot()
            try:native_observer.close()
            except Exception as error:cleanup_errors.append(str(error))
            report['native_observation_cleanup']=native_observer.snapshot()
            if native_observer.overflow:cleanup_errors.append('Overlay native observation overflow')
        for target in (overlay, root):
            if target is not None:
                try:
                    target.close() if target is overlay else target.destroy()
                except Exception as error:
                    cleanup_errors.append(str(error))
        if parent is not None and process is not None and process.is_alive():
            try:
                parent.send('stop')
            except (BrokenPipeError, EOFError):
                pass
            process.join(3)
            if process.is_alive():
                process.terminate(); process.join(3)
        report['counter_exited'] = process is None or not process.is_alive()
        if parent is not None and report['counter_exited']:
            try:
                while parent.poll(.05):
                    final=parent.recv()
                    if final.get('kind')=='stopped':
                        report['counter_native_cleanup']=final
                        cleanup_errors.extend(final['native_cleanup_errors'])
                        observation=final.get('native_observation')
                        if observation and (observation['overflow'] or observation['errors']):
                            cleanup_errors.append('Counter native observation incomplete')
            except (EOFError,OSError):pass
            if process is not None and 'counter_native_cleanup' not in report:
                cleanup_errors.append('Counter native observation cleanup acknowledgement missing')
        for connection in (parent, child):
            if connection is not None:
                connection.close()
        if process is not None and report['counter_exited']:
            process.close()
        report['injected_held_keys_after_cleanup']={str(down):bool(user.GetAsyncKeyState(1 if down==0x2 else 2)&0x8000)
                                                     for down in held}
        if any(report['injected_held_keys_after_cleanup'].values()):
            cleanup_errors.append('Previously injected held button is still pressed; origin unproven')
        report['finished_epoch'] = time.time()
        report['cleanup_errors'] = cleanup_errors
        report['success'] = bool(report['success'] and report['counter_exited'] and not cleanup_errors)
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if report['success'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--candidate',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=45)
    args = parser.parse_args()
    if not 15 <= args.seconds <= 120:
        parser.error('--seconds must be between 15 and 120')
    return run(args)


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
