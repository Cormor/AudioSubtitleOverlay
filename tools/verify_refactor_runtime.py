"""Run an isolated local acceptance session against existing system playback.

Never controls playback. The chosen output contains private local evidence;
no captured audio or transcript is uploaded. Model caches are reused by junction.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=70)
    parser.add_argument('--cycles', type=int, default=0)
    parser.add_argument('--trace-audio', action='store_true', help='仅本地保存有限音频供同样本比较')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output/"process.json").write_text(json.dumps({"pid":os.getpid()}),encoding="utf-8")
    real_data = Path(os.environ['APPDATA']) / 'AudioSubtitleOverlay'
    original = (real_data / 'settings.json').read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    payload = json.loads(original)
    test_data = output / 'isolated-appdata' / 'AudioSubtitleOverlay'
    test_data.mkdir(parents=True, exist_ok=True)
    for name in ('translation_models',):
        link = test_data / name
        if not link.exists():
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(real_data / name)],
                           check=True, capture_output=True)
    payload.update(auto_start=False, start_minimized=False, source_language='自动检测', target_language='英语',
                   recognition_model='sensevoice-int8', recognition_device='CPU（int8）',
                   model_path=str(real_data/'models'/'sensevoice-int8'),
                   translation_enabled=True, translation_backend='仅使用本地翻译',
                   overlay_geometry='720x300+80+120', overlay_topmost=False, ui_theme='dark')
    # A specific existing system-playback device is selected after enumeration;
    # no Windows or browser setting is changed.
    (test_data/'settings.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.environ['APPDATA'] = str(test_data.parent)
    os.environ['ASR_TRACE_DIR'] = str(output/'trace')
    os.environ['ASR_TRACE_SECONDS'] = str(min(600, args.seconds+30))
    os.environ['ASR_TRACE_TEXT'] = '1'
    if args.trace_audio:
        os.environ['ASR_TRACE_AUDIO'] = '1'
    logging.basicConfig(filename=output/'runtime.log', encoding='utf-8', level=logging.INFO,
                        format='%(asctime)s %(levelname)s pid=%(process)d %(message)s')
    import tkinter as tk
    from main import Application
    from runtime_trace import get_runtime_trace
    from PIL import ImageGrab
    root = tk.Tk()
    app = Application(root, autostart=False, tray=False)
    root.geometry('980x780+30+30')
    sources = [str(item) for item in app.audio_combo['values'] if str(item).startswith('系统播放')]
    if not sources:
        app.close()
        raise RuntimeError('No enumerated system playback device')
    from audio_capture import SystemAudioCapture
    default_device = SystemAudioCapture.default_loopback_device()
    selected = next((item for item in sources if item.split('：', 1)[-1].strip() == default_device), None)
    if selected is None:
        app.close()
        raise RuntimeError('Default playback device is not in current enumeration')
    app.audio_device_var.set(selected)
    root.update()
    app._start()
    observed = {'source_updates':0, 'translation_updates':0, 'visible_source_frames':0,
                'visible_translation_frames':0, 'cycles_completed':0, 'errors':[], 'worker_pids':[]}
    last_source = last_translation = ''
    started = time.monotonic()
    next_report = started+5
    try:
        while time.monotonic()-started < args.seconds:
            root.update()
            snapshot = app._display.snapshot()
            if snapshot.latest_source and snapshot.latest_source != last_source:
                observed['source_updates'] += 1
                last_source = snapshot.latest_source
            if snapshot.latest_translation and snapshot.latest_translation != last_translation:
                observed['translation_updates'] += 1
                last_translation = snapshot.latest_translation
            overlay = app.overlay
            if overlay.visible_source_fragments:
                observed['visible_source_frames'] += 1
            if getattr(overlay, 'visible_translation_fragments', {}):
                observed['visible_translation_frames'] += 1
            if app._last_error and app._last_error not in observed['errors']:
                observed['errors'].append(app._last_error)
            if time.monotonic() >= next_report:
                print(json.dumps({'elapsed':round(time.monotonic()-started), **observed}, ensure_ascii=True), flush=True)
                next_report += 5
            time.sleep(.01)
        root.update()
        if hasattr(app.overlay, '_last_image'):
            app.overlay._last_image.save(output/'overlay-render.png')
        # Full window captures include actual compositor output, not just source strings.
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user.GetAncestor.restype = ctypes.c_void_p
        ImageGrab.grab(window=user.GetAncestor(root.winfo_id(), 2)).save(output/'main-window.png')
        overlay_hwnd = user.GetAncestor(app.overlay.window.winfo_id(), 2)
        from verify_installed_runtime import capture_visible_window
        capture_visible_window({'hwnd':overlay_hwnd},output/'overlay-window.png')
        pipeline = app.pipeline
        stop_started = time.monotonic()
        app._stop()
        while app.pipeline is not None and time.monotonic()-stop_started < 10:
            root.update(); time.sleep(.01)
        observed['stop_seconds'] = time.monotonic()-stop_started
        observed['stopped'] = app.pipeline is None and not (pipeline and pipeline._thread.is_alive())
        observed['children_after_stop'] = [p.pid for p in multiprocessing.active_children()]
        for index in range(args.cycles):
            app._start()
            deadline = time.monotonic()+12
            while app.pipeline is not None and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
                if app._runtime_phase in ('recognizing','waiting_audio'):
                    break
            if app.pipeline is None:
                raise RuntimeError('Session failed during cycle')
            app._stop(); deadline=time.monotonic()+10
            while app.pipeline is not None and time.monotonic()<deadline:
                root.update(); time.sleep(.01)
            if app.pipeline is not None or multiprocessing.active_children():
                raise RuntimeError(f'Resources remain after cycle {index}')
            observed['cycles_completed'] += 1
    finally:
        app.close()
        get_runtime_trace().close()
        observed['formal_settings_unchanged'] = hashlib.sha256((real_data/'settings.json').read_bytes()).hexdigest() == original_hash
        observed['children_after_close'] = [p.pid for p in multiprocessing.active_children()]
        (output/'result.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(observed, ensure_ascii=True), flush=True)
    if not (observed['source_updates'] and observed['translation_updates'] and observed['visible_source_frames']
            and observed['visible_translation_frames'] and observed['stopped'] and observed['formal_settings_unchanged']
            and not observed['children_after_close']):
        raise SystemExit('Live acceptance incomplete; inspect local evidence')


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
