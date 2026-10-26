"""Window constraints in the calling GUI thread's logical screen coordinates."""
from dataclasses import dataclass
from functools import lru_cache
import os
import re


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self): return self.x+self.width
    @property
    def bottom(self): return self.y+self.height

    def contains(self, point):
        return self.x <= point[0] < self.right and self.y <= point[1] < self.bottom

    def overlap(self, other):
        return max(0,min(self.right,other.right)-max(self.x,other.x))*max(0,min(self.bottom,other.bottom)-max(self.y,other.y))

    def distance(self, point):
        return max(self.x-point[0],0,point[0]-self.right)**2+max(self.y-point[1],0,point[1]-self.bottom)**2

    def geometry(self):
        # A literal negative virtual-screen coordinate uses '+-N', not Tk's
        # '-N' right/bottom-edge offset syntax.
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


@dataclass(frozen=True)
class Monitor:
    screen: Rect
    work: Rect


def parse_geometry(value):
    match=re.fullmatch(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)",str(value))
    if not match: raise ValueError(f"Invalid window geometry: {value!r}")
    width,height,x,y=match.groups()
    return Rect(int(x.replace('+-','-')),int(y.replace('+-','-')),int(width),int(height))


def constrain(rect, monitors, *, pointer=None, resize=False, minimum=(320,68)):
    if not monitors: raise ValueError("No monitor work areas")
    reference=pointer if pointer is not None else (rect.x+rect.width/2,rect.y+rect.height/2)
    containing=[m for m in monitors if m.screen.contains(reference)] if pointer is not None else []
    monitor=(containing[0] if containing else
             min(monitors,key=lambda m: (-(m.screen.overlap(rect) if pointer is None else 0),m.screen.distance(reference))))
    work=monitor.work
    min_width,min_height=min(minimum[0],work.width),min(minimum[1],work.height)
    width=min(work.width,max(min_width,round(rect.width)))
    height=min(work.height,max(min_height,round(rect.height)))
    if resize:
        x=max(work.x,min(round(rect.x),work.right-min_width))
        y=max(work.y,min(round(rect.y),work.bottom-min_height))
        width=min(width,work.right-x)
        height=min(height,work.bottom-y)
    else:
        x=max(work.x,min(round(rect.x),work.right-width))
        y=max(work.y,min(round(rect.y),work.bottom-height))
    return Rect(x,y,width,height),work


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes as c
    from ctypes import wintypes as w
    class MonitorInfo(c.Structure):
        _fields_=[('size',w.DWORD),('screen',w.RECT),('work',w.RECT),('flags',w.DWORD)]
    user=c.WinDLL('user32',use_last_error=True)
    callback=c.WINFUNCTYPE(w.BOOL,w.HANDLE,w.HDC,c.POINTER(w.RECT),w.LPARAM)
    user.EnumDisplayMonitors.argtypes=[w.HDC,c.POINTER(w.RECT),callback,w.LPARAM]
    user.EnumDisplayMonitors.restype=w.BOOL
    user.GetMonitorInfoW.argtypes=[w.HANDLE,c.POINTER(MonitorInfo)]
    user.GetMonitorInfoW.restype=w.BOOL
    return c,user,MonitorInfo,callback


def monitor_areas(window):
    if os.name == 'nt':
        c,user,Info,Callback=_windows_api()
        result=[]
        def collect(handle,_dc,_rect,_data):
            info=Info();info.size=c.sizeof(info)
            if user.GetMonitorInfoW(handle,c.byref(info)):
                def rect(r):return Rect(r.left,r.top,r.right-r.left,r.bottom-r.top)
                screen,work=rect(info.screen),rect(info.work)
                if min(screen.width,screen.height,work.width,work.height)>0:
                    result.append(Monitor(screen,work))
            return True
        if user.EnumDisplayMonitors(None,None,Callback(collect),0) and result:
            return tuple(result)
    # Portable fallback if no native monitor information is available.
    rect=Rect(window.winfo_vrootx(),window.winfo_vrooty(),window.winfo_vrootwidth(),window.winfo_vrootheight())
    return (Monitor(rect,rect),)
