"""Replay a fixed local WAV through real ASR, MT and the real Windows overlay.

The source checkout can point at the reconstructed pre-refactor baseline.
No speaker playback is started; captured input is injected at the capture boundary.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--wav', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--audio-seconds', type=float, default=12)
    parser.add_argument('--source-language', choices=['zh','en'], default='zh')
    parser.add_argument('--target-language', choices=['zh','en'], default='en')
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    from verify_streaming_translation import source_identity, sha256
    source_hashes = source_identity(source)
    run_started_epoch = time.time()
    real_data = Path(os.environ['APPDATA'])/'AudioSubtitleOverlay'
    formal_hash = hashlib.sha256((real_data/'settings.json').read_bytes()).hexdigest()
    appdata = output/'isolated-appdata'
    folder = appdata/'AudioSubtitleOverlay'
    folder.mkdir(parents=True, exist_ok=True)
    link = folder/'translation_models'
    if not link.exists():
        subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(real_data/'translation_models')],check=True,capture_output=True)
    settings = json.loads((real_data/'settings.json').read_text(encoding='utf-8'))
    labels={'zh':'中文','en':'英语'}
    settings.update(auto_start=False, source_language=labels[args.source_language], target_language=labels[args.target_language], recognition_model='sensevoice-int8',
                    recognition_device='CPU（int8）', model_path=str(real_data/'models'/'sensevoice-int8'),
                    translation_backend='仅使用本地翻译', translation_enabled=True, overlay_geometry='720x300+80+120',
                    overlay_topmost=False, overlay_animation=False)
    (folder/'settings.json').write_text(json.dumps(settings,ensure_ascii=False),encoding='utf-8')
    os.environ['APPDATA'] = str(appdata)
    sys.path.insert(0,str(source))
    import numpy as np
    import tkinter as tk
    from main import Application
    import pipeline
    import audio_capture
    import atexit
    with wave.open(str(args.wav),'rb') as audio:
        if (audio.getframerate(),audio.getnchannels(),audio.getsampwidth()) != (16000,1,2):
            raise ValueError('Expected 16kHz mono PCM16')
        samples = np.frombuffer(audio.readframes(int(args.audio_seconds*16000)),dtype='<i2').astype(np.float32)/32768
    samples = np.concatenate((samples,np.zeros(16000,dtype=np.float32)))

    class ReplayCapture:
        sample_rate=16000
        def __init__(self, name=''):
            self._stop=threading.Event()
            self.finished=threading.Event()
        def start(self,on_audio,on_error,on_ready=None):
            def feed():
                if on_ready:on_ready('固定音频回放（不播放到扬声器）')
                started=time.perf_counter()
                for offset in range(0,len(samples),320):
                    if self._stop.is_set():break
                    on_audio(samples[offset:offset+320].copy())
                    if self._stop.wait(max(0,started+(offset+320)/16000-time.perf_counter())):break
                self.finished.set()
            self._thread=threading.Thread(target=feed,daemon=True)
            self._thread.start()
        def stop(self,wait=False):
            self._stop.set()
            if wait and self._thread is not threading.current_thread():self._thread.join(2)
    pipeline.SystemAudioCapture=ReplayCapture
    reports=[]
    for index in range(args.rounds):
        root=tk.Tk()
        app=Application(root,autostart=False,tray=False)
        root.withdraw()
        atexit.register(app.close)
        default_name=audio_capture.SystemAudioCapture.default_loopback_device()
        app.audio_device_var.set(next(str(item) for item in app.audio_combo["values"] if str(item).startswith("系统播放") and str(item).split("：",1)[-1].strip()==default_name))
        measurements=[]
        presented=app.overlay.on_presented
        def on_presented(now,indices):
            block_ids=list(app._history.blocks)
            preview_id=getattr(app._history,'_preview_block_id',None)
            if preview_id is not None and preview_id not in block_ids:block_ids.append(preview_id)
            for entry_index in indices:
                if entry_index>=len(block_ids):continue
                block_id=block_ids[entry_index]
                update=app._pending_presentations.get(block_id)
                block = app._history.blocks.get(block_id)
                displayed = block.sequence if block is not None else getattr(app._history, '_preview_sequence', None)
                if update is not None and update.sequence==displayed:
                    measurements.append(update.measurements(now))
            presented(now,indices)
        app.overlay.on_presented=on_presented
        app._start()
        started=time.monotonic()
        while time.monotonic()-started < args.audio_seconds+20:
            root.update();time.sleep(.005)
            cap=getattr(app.pipeline,'_capture',None)
            if cap and cap.finished.is_set() and time.monotonic()-started > args.audio_seconds+7:break
        translated=any(translation for _,translation in app._display.snapshot().entries)
        app._stop();deadline=time.monotonic()+10
        while app.pipeline is not None and time.monotonic()<deadline:root.update();time.sleep(.005)
        stopped=app.pipeline is None
        app.close()
        values=[m['输入终点到上屏毫秒'] for m in measurements]
        values=sorted(values)
        report={'round':index+1,'visible_measurements':len(values),'p50_ms':values[len(values)//2] if values else None,
                'p95_ms':values[int(.95*(len(values)-1))] if values else None,'translation':translated,'stopped':stopped}
        reports.append(report)
        print(json.dumps(report),flush=True)
    result={'source':source.name,'audio_sha256':hashlib.sha256(args.wav.read_bytes()).hexdigest(),
            'audio_seconds':len(samples)/16000,'rounds':reports,'scope':'Fixed replay -> actual ASR/MT -> visible compositor submission; not acoustic or scanout latency',
            'formal_settings_unchanged':hashlib.sha256((real_data/'settings.json').read_bytes()).hexdigest()==formal_hash}
    provenance = dict(pid=os.getpid(), parent_pid=os.getppid(), source=str(source),
                      source_sha256=source_hashes, source_unchanged=source_identity(source)==source_hashes,
                      started_epoch=run_started_epoch, ended_epoch=time.time(),
                      trace_root=os.environ.get('ASR_TRACE_DIR', ''),
                      executable=str(Path(sys.executable).resolve()), executable_sha256=sha256(Path(sys.executable)))
    (output/'provenance.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    result['source_unchanged'] = provenance['source_unchanged']
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if not result['source_unchanged'] or not all(r['visible_measurements'] and r['translation'] and r['stopped'] for r in reports):raise SystemExit('Replay benchmark incomplete')


if __name__=='__main__':
    multiprocessing.freeze_support()
    main()
