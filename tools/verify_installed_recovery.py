"""Terminate only identified worker DLL owners in an isolated installed app."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from verify_installed_runtime import process_snapshot, windows_for_pid


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    exe=args.exe.resolve()
    real=Path(os.environ['APPDATA'])/'AudioSubtitleOverlay'
    formal=(real/'settings.json').read_bytes()
    data=output/'isolated-appdata'/'AudioSubtitleOverlay';data.mkdir(parents=True,exist_ok=True)
    subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(data/'translation_models'),str(real/'translation_models')],check=True,capture_output=True)
    import soundcard
    settings=json.loads(formal)
    settings.update(auto_start=True,start_minimized=True,source_language='中文',target_language='英语',
        recognition_model='sensevoice-int8',recognition_device='CPU（int8）',model_path=str(real/'models'/'sensevoice-int8'),
        loopback_device='系统播放：'+str(soundcard.default_speaker().name),translation_enabled=True,
        translation_backend='仅使用本地翻译',overlay_geometry='720x300+80+120',overlay_topmost=False)
    (data/'settings.json').write_text(json.dumps(settings,ensure_ascii=False),encoding='utf-8')
    env=os.environ.copy();env['APPDATA']=str(data.parent)
    log=data/'logs'/'app.log';events=[];known=set()
    with (output/'console.txt').open('wb') as console:
        app=subprocess.Popen([str(exe),'--quit-after','240'],cwd=exe.parent,env=env,stdout=console,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        (output/'process.json').write_text(json.dumps({'pid':app.pid,'exe':str(exe)},indent=2))
        known.add(app.pid)
        def observe():
            rows=process_snapshot(exe)
            while True:
                old=len(known);known.update(x['pid'] for x in rows if x['parent'] in known)
                if len(known)==old:break
            return [x for x in rows if x['pid'] in known]
        def read():return log.read_text(encoding='utf-8',errors='replace') if log.exists() else ''
        def wait_for(predicate,label,timeout=60):
            start=time.monotonic()
            while time.monotonic()-start<timeout:
                observe()
                if predicate():
                    events.append({'step':label,'seconds':round(time.monotonic()-start,3)})
                    return
                if app.poll() is not None:raise RuntimeError('App exited during '+label)
                time.sleep(.5)
            raise TimeoutError(label)
        def kill_worker(kind):
            matches=[];identities=[]
            for row in observe():
                if row['parent']!=app.pid:continue
                raw=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',f'(Get-Process -Id {row["pid"]}).Modules | ForEach-Object {{$_.FileName}} | ConvertTo-Json -Compress'],encoding='utf-8',errors='replace').strip()
                maps=[str(x).lower() for x in json.loads(raw)]
                is_asr=any('sherpa' in name and name.endswith(('.dll','.pyd')) for name in maps)
                is_translator=any('ctranslate2' in name and name.endswith(('.dll','.pyd')) for name in maps)
                identities.append({'pid':row['pid'],'asr':is_asr,'translator':is_translator,'libraries':[name for name in maps if any(t in name for t in ['sherpa','ctranslate','onnx'])]})
                if (kind=='asr' and is_asr) or (kind=='translator' and is_translator and not is_asr):matches.append(row['pid'])
            (output/('identities-'+kind+'.json')).write_text(json.dumps(identities,indent=2))
            if len(matches)!=1:raise RuntimeError(f'Worker identity ambiguous: {kind}: {matches}')
            pid=matches[0]
            kernel=ctypes.windll.kernel32
            kernel.OpenProcess.restype=ctypes.c_void_p
            kernel.TerminateProcess.argtypes=[ctypes.c_void_p,ctypes.c_uint]
            kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint]
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            handle=kernel.OpenProcess(0x100001,False,pid)
            if not handle:raise ctypes.WinError()
            try:
                if not kernel.TerminateProcess(handle,17):raise ctypes.WinError()
                if kernel.WaitForSingleObject(handle,5000)!=0:raise RuntimeError('Worker did not terminate')
            finally:kernel.CloseHandle(handle)
            events.append({'step':'terminate_'+kind,'pid':pid})
        try:
            wait_for(lambda:'译文已接收' in read(),'initial_translation')
            before=read().count('译文已接收');kill_worker('translator')
            wait_for(lambda:read().count('译文已接收')>before,'translator_resumed')
            before=read().count('字幕窗口已提交');kill_worker('asr')
            # A new identified native ASR process and further submitted text prove recovery.
            wait_for(lambda:any(x['parent']==app.pid and x['pid'] not in [e.get('pid') for e in events if e['step']=='terminate_asr'] and x['working_set']>250_000_000 for x in observe()) and read().count('字幕窗口已提交')>before,'asr_resumed')
            kill_worker('asr')
            wait_for(lambda:len(observe())==1,'repeated_asr_stopped')
            stopped_count=read().count('字幕窗口已提交')
            time.sleep(5)
            if len(observe())!=1 or read().count('字幕窗口已提交')!=stopped_count:raise RuntimeError('Stopped ASR unexpectedly resumed')
            events.append({'step':'no_unbounded_restart_or_late_source'})
        finally:
            for window in windows_for_pid(app.pid):ctypes.windll.user32.PostMessageW(window['hwnd'],0x10,0,0)
            forced_cleanup=False
            try:app.wait(20)
            except subprocess.TimeoutExpired:
                forced_cleanup=True
                for row in sorted(observe(),key=lambda x:x['pid']==app.pid):
                    subprocess.run(['taskkill.exe','/PID',str(row['pid']),'/F'],capture_output=True,check=True)
                app.wait(5)
            remaining=observe()
            result={'completed':any(e['step']=='no_unbounded_restart_or_late_source' for e in events),'forced_cleanup':forced_cleanup,'events':events,'exit_code':app.returncode,'remaining_processes':remaining,'formal_settings_unchanged':(real/'settings.json').read_bytes()==formal}
            (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=True))
            if remaining or forced_cleanup or not result['formal_settings_unchanged']:raise RuntimeError('Cleanup or isolation failed')


if __name__=='__main__':main()
