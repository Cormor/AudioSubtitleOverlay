"""Observe the actual installed executable with isolated settings and local evidence."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time



def link_shared_model_caches(real_data, isolated_data, *, create_link=None):
    """Expose managed caches without converting them into a custom model path."""
    real_data = Path(real_data).resolve(strict=True)
    isolated_data = Path(isolated_data).resolve(strict=True)
    if real_data == isolated_data:
        raise ValueError('Acceptance data must differ from the formal data directory')
    if create_link is None:
        def create_link(destination, source):
            subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(destination),str(source)],
                           check=True,capture_output=True)
    pairs = [(isolated_data / name, real_data / name) for name in ('models', 'translation_models')]
    for destination, source in pairs:
        if not source.is_dir():
            raise ValueError(f'Shared model cache is not available: {source}')
        occupied = (destination.exists() or destination.is_symlink()
                    or getattr(destination, 'is_junction', lambda: False)())
        if occupied and (not destination.is_dir() or not destination.samefile(source)):
            raise ValueError(f'Isolated cache already has a different target: {destination}')
    for destination, source in pairs:
        if not destination.exists():
            create_link(destination, source)
        if not destination.is_dir() or not destination.samefile(source):
            raise RuntimeError(f'Shared cache link did not resolve to its source: {destination}')


def windows_for_pid(pid):
    user=ctypes.windll.user32
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    result=[]
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    @callback_type
    def collect(hwnd,_):
        found=wintypes.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(found))
        if found.value==pid and user.IsWindowVisible(hwnd):
            title=ctypes.create_unicode_buffer(512);user.GetWindowTextW(hwnd,title,512)
            box=wintypes.RECT()
            if not user.GetWindowRect(hwnd,ctypes.byref(box)):return True
            if box.right-box.left>=300 and box.bottom-box.top>=60:
                result.append({'hwnd':int(hwnd),'title':title.value,'width':box.right-box.left,'height':box.bottom-box.top})
        return True
    user.EnumWindows(collect,0)
    return result


def capture_visible_window(window, output):
    """Capture actual compositor pixels; PrintWindow is black for layered HWNDs."""
    from PIL import ImageGrab
    user=ctypes.windll.user32
    user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype=wintypes.BOOL
    user.GetWindowLongW.argtypes=[wintypes.HWND,ctypes.c_int]
    user.GetWindowLongW.restype=wintypes.LONG
    user.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
    user.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
    previous_dpi=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    hwnd=window['hwnd'];was_topmost=bool(user.GetWindowLongW(hwnd,-20)&8)
    try:
        if not was_topmost:
            if not user.SetWindowPos(hwnd,-1,0,0,0,0,0x13):raise ctypes.WinError()
        time.sleep(.15)
        box=wintypes.RECT()
        if not user.GetWindowRect(hwnd,ctypes.byref(box)):raise ctypes.WinError()
        ImageGrab.grab(bbox=(box.left,box.top,box.right,box.bottom),include_layered_windows=True,all_screens=True).save(output)
    finally:
        if not was_topmost:user.SetWindowPos(hwnd,-2,0,0,0,0,0x13)
        if previous_dpi:user.SetThreadDpiAwarenessContext(previous_dpi)


def process_snapshot(executable):
    # Exact executable path excludes the older installed app still serving the user.
    quoted=str(executable).replace("'","''")
    code=f"Get-CimInstance Win32_Process -Filter \"Name='AudioSubtitleOverlay.exe'\" | Where-Object {{$_.ExecutablePath -eq '{quoted}'}} | ForEach-Object {{$p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; [pscustomobject]@{{pid=$_.ProcessId;parent=$_.ParentProcessId;working_set=$p.WorkingSet64;handle_count=$p.HandleCount}}}} | ConvertTo-Json -Compress"
    raw=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',code],encoding='utf-8',errors='replace').strip()
    value=json.loads(raw) if raw else []
    return [value] if isinstance(value,dict) else value


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=3600)
    parser.add_argument('--show-main',action='store_true',help='Keep the isolated acceptance main window visible for real settings interaction')
    parser.add_argument('--model',choices=['sensevoice-int8','tiny.en','zipformer-zh-en-medium-int8'],default='sensevoice-int8')
    parser.add_argument('--source-language',choices=['自动检测','中文','英语'],default='自动检测')
    parser.add_argument('--target-language',choices=['中文','英语'],default='英语')
    parser.add_argument('--keep-source-selection',action='store_true',help='验证正式配置原有音源策略，仍使用独立APPDATA')
    args=parser.parse_args()
    exe=args.exe.resolve();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    if not exe.is_file() or exe.name!='AudioSubtitleOverlay.exe':raise ValueError('Expected installed app executable')
    if args.keep_source_selection:
        # APPDATA isolation alone does not isolate Windows audio-session meters.
        existing=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            "@(Get-CimInstance Win32_Process -Filter \"Name='AudioSubtitleOverlay.exe'\").Count"],encoding='utf-8',errors='replace').strip()
        if existing and int(existing):
            raise RuntimeError('Beta source verification requires serial capture: close the existing owned app before running this isolated check')
    real=Path(os.environ['APPDATA'])/'AudioSubtitleOverlay'
    formal=(real/'settings.json').read_bytes();formal_hash=hashlib.sha256(formal).hexdigest()
    data=output/'isolated-appdata'/'AudioSubtitleOverlay';data.mkdir(parents=True,exist_ok=True)
    link_shared_model_caches(real, data)
    import soundcard
    source=str(soundcard.default_speaker().name)
    settings=json.loads(formal)
    settings.update(auto_start=True,start_minimized=not args.show_main,source_language=args.source_language,target_language=args.target_language,
                    recognition_model=args.model,recognition_device='CPU（int8）',model_path='',
                    loopback_device='系统播放：'+source,translation_enabled=True,translation_backend='仅使用本地翻译',
                    overlay_geometry='720x300+80+120',overlay_topmost=False,ui_theme='dark')
    if args.keep_source_selection:settings['loopback_device']=json.loads(formal)['loopback_device']
    (data/'settings.json').write_text(json.dumps(settings,ensure_ascii=False),encoding='utf-8')
    env=os.environ.copy();env['APPDATA']=str(data.parent)
    env['PYTHONIOENCODING']='utf-8'
    from verify_streaming_translation import sha256
    executable_hash = sha256(exe)
    trace_root = (output/'trace').resolve()
    with (output/'console.txt').open('wb') as console:
        run_started_epoch = time.time()
        process=subprocess.Popen([str(exe),'--trace-dir',str(trace_root),'--trace-seconds','600','--trace-text',
                                  '--quit-after',str(args.seconds)],cwd=exe.parent,env=env,stdout=console,stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        run_record = dict(pid=process.pid, exe=str(exe), target_seconds=args.seconds,
                          started_epoch=run_started_epoch, executable_sha256=executable_hash,
                          trace_root=str(trace_root))
        (output/'process.json').write_text(json.dumps(run_record,indent=2),encoding='utf-8')
        counts={'source_submissions':0,'translation_received':0,'translation_submissions':0,'health':0,'errors':0}
        offset=0;started=time.monotonic();next_status=0;next_capture=45;captures=0;snapshots=[]
        owned_pids={process.pid}
        def owned_snapshot():
            rows=process_snapshot(exe)
            while True:
                previous=len(owned_pids)
                owned_pids.update(row['pid'] for row in rows if row['parent'] in owned_pids)
                if len(owned_pids)==previous:break
            return [row for row in rows if row['pid'] in owned_pids]
        log=data/'logs'/'app.log'
        while process.poll() is None:
            elapsed=time.monotonic()-started
            if log.exists():
                with log.open('r',encoding='utf-8',errors='replace') as stream:
                    if log.stat().st_size<offset:offset=0
                    stream.seek(offset);lines=stream.readlines();offset=stream.tell()
                for line in lines:
                    for token,key in [('字幕窗口已提交','source_submissions'),('译文已接收','translation_received'),('译文窗口已提交','translation_submissions'),('运行状态：','health'),(' ERROR ','errors')]:
                        if token in line:counts[key]+=1
            if elapsed>=next_capture:
                from PIL import ImageGrab
                for index,window in enumerate(windows_for_pid(process.pid)):
                    try:
                        capture_visible_window(window,output/f'window-{round(elapsed):04}-{index}.png')
                        captures+=1
                    except OSError:pass
                next_capture+=300
            if elapsed>=next_status:
                row={'elapsed_seconds':round(elapsed,2),'pid':process.pid,**counts,'processes':owned_snapshot(),'captures':captures}
                snapshots.append(row)
                with (output/'monitor.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                print(json.dumps(row,ensure_ascii=True),flush=True)
                next_status+=60
            if elapsed>args.seconds+90:
                process.terminate();process.wait(10);raise RuntimeError('Timed exit did not finish')
            time.sleep(2)
        duration=time.monotonic()-started
        final={'exe':str(exe),'pid':process.pid,'exit_code':process.returncode,'duration_seconds':duration,**counts,
               'captures':captures,'remaining_processes':owned_snapshot(),
               'formal_settings_unchanged':hashlib.sha256((real/'settings.json').read_bytes()).hexdigest()==formal_hash}
        run_record.update(ended_epoch=time.time(), owned_pids=sorted(owned_pids),
                          executable_unchanged=sha256(exe)==executable_hash)
        (output/'process.json').write_text(json.dumps(run_record,indent=2),encoding='utf-8')
        final['executable_unchanged'] = run_record['executable_unchanged']
        (output/'result.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(final,ensure_ascii=True),flush=True)
        if not(final['executable_unchanged'] and final['exit_code']==0 and duration>=args.seconds-2 and counts['source_submissions'] and counts['translation_submissions'] and counts['errors']==0 and not final['remaining_processes'] and final['formal_settings_unchanged']):raise SystemExit('Installed acceptance incomplete')


if __name__=='__main__':main()
