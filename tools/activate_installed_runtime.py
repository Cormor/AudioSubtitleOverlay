"""Activate an accepted local install, preserving the user's live settings."""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from verify_installed_runtime import capture_visible_window, process_snapshot, windows_for_pid


def subtitle_window(pid):
    """The user's overlay may be withdrawn and its lane may be collapsed."""
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongW.restype = wintypes.LONG
    matches = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            kind = ctypes.create_unicode_buffer(128)
            user.GetClassNameW(hwnd, kind, 128)
            if kind.value == 'TkTopLevel' and user.GetWindowLongW(hwnd, -20) & 0x80000:
                matches.append(int(hwnd))
        return True
    user.EnumWindows(collect, 0)
    if len(matches) != 1:
        raise RuntimeError('Cannot uniquely identify the owned layered subtitle window')
    return matches[0]


def restore_overlay_geometry(pid, preferred, config_path):
    """Restore saved position after native lane-height changes during startup."""
    from ctypes import wintypes
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from screen_bounds import parse_geometry
    from settings import Settings
    wanted = parse_geometry(preferred)
    user = ctypes.windll.user32
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
    hwnd = subtitle_window(pid)
    if not user.SetWindowPos(hwnd, None, wanted.x, wanted.y, 0, 0, 0x15):
        raise ctypes.WinError()
    time.sleep(.3)
    current = Settings.load()
    current.overlay_geometry = preferred
    current.save()
    time.sleep(1)
    actual = json.loads(config_path.read_text(encoding='utf-8'))['overlay_geometry']
    if actual != preferred:
        raise RuntimeError('Preferred overlay position was not retained')
    return actual


def all_instances():
    code = "Get-CimInstance Win32_Process -Filter \"Name='AudioSubtitleOverlay.exe'\" | Select-Object ProcessId,ParentProcessId,ExecutablePath | ConvertTo-Json -Compress"
    raw = subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', code], encoding='utf-8', errors='replace').strip()
    value = json.loads(raw) if raw else []
    return [value] if isinstance(value, dict) else value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--acceptance', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--minimum-seconds', type=int, default=3600, help='Required installed observation; UI-only maintenance can use a scoped shorter run (at least 60 seconds)')
    args = parser.parse_args()
    if not 60 <= args.minimum_seconds <= 86400:
        parser.error('minimum-seconds must be between 60 and 86400')
    receipt = json.loads(args.receipt.read_text(encoding='utf-8'))['installation']
    folder = Path(receipt['directory'])
    exe = folder / receipt['executable']
    install_root = Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'AudioSubtitleOverlay'
    if folder.parent != install_root or receipt['identity'] != 'AudioSubtitleOverlay':
        raise ValueError('Unexpected installation identity or location')
    if hashlib.sha256(exe.read_bytes()).hexdigest() != receipt['executable_sha256']:
        raise ValueError('Installed executable differs from accepted package')
    current = json.loads((install_root / 'current-installation.json').read_text(encoding='utf-8'))
    if current['package_sha256'] != receipt['package_sha256']:
        raise ValueError('Current installation receipt differs')
    real = Path(os.environ['APPDATA']) / 'AudioSubtitleOverlay'
    config_path = real / 'settings.json'
    original = json.loads(config_path.read_text(encoding='utf-8'))
    args.output.mkdir(parents=True, exist_ok=True)
    instances = all_instances()
    result_path = args.acceptance / 'result.json'
    accepted = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else None
    check = {'version': receipt['version'], 'package_sha256': receipt['package_sha256'],
             'exe': str(exe), 'instances': instances, 'acceptance_present': accepted is not None,
             'minimum_seconds': args.minimum_seconds, 'auto_start': original.get('auto_start'), 'start_minimized': original.get('start_minimized')}
    (args.output / 'preflight.json').write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.preflight:
        print(json.dumps(check, ensure_ascii=True))
        return
    if not accepted or not (Path(accepted['exe']).resolve() == exe.resolve() and accepted['exit_code'] == 0 and accepted['duration_seconds'] >= args.minimum_seconds - 2
            and accepted['source_submissions'] and accepted['translation_received']
            and accepted['translation_submissions'] and accepted['errors'] == 0
            and not accepted['remaining_processes'] and accepted['formal_settings_unchanged']):
        raise ValueError('Required installed observation has not passed for this executable')
    if process_snapshot(exe):
        raise RuntimeError('Accepted executable already has active instances; inspect ownership')
    old = []
    for instance in instances:
        path = Path(instance['ExecutablePath'] or '')
        if path.name != 'AudioSubtitleOverlay.exe' or install_root not in path.parents:
            raise RuntimeError('Unexpected application instance; inspect before switching')
        if instance['ParentProcessId'] not in {x['ProcessId'] for x in instances}:
            old.append(instance)
    if len(old) > 1:
        raise RuntimeError('Multiple user application roots; inspect before switching')
    for instance in old:
        hwnd = subtitle_window(instance['ProcessId'])
        ctypes.windll.user32.PostMessageW(hwnd, 0x10, 0, 0)
    deadline = time.monotonic() + 20
    while all_instances() and time.monotonic() < deadline:
        time.sleep(.5)
    if all_instances():
        raise RuntimeError('Old application has not exited; no duplicate instance started')
    # The old app can legitimately save its current window position on exit.
    # Preserve its resulting settings; do not rewrite from an earlier snapshot.
    preserved = json.loads(config_path.read_text(encoding='utf-8'))
    log = real / 'logs' / 'app.log'
    offset = log.stat().st_size if log.exists() else 0
    app = subprocess.Popen([str(exe)], cwd=exe.parent, creationflags=subprocess.CREATE_NO_WINDOW)
    (args.output / 'process.json').write_text(json.dumps({'pid': app.pid, 'exe': str(exe)}, indent=2), encoding='utf-8')
    lines = []
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and app.poll() is None:
        if log.exists():
            with log.open(encoding='utf-8', errors='replace') as stream:
                if log.stat().st_size < offset:
                    offset = 0
                stream.seek(offset)
                lines.extend(stream.readlines())
                offset = stream.tell()
        own_lines = [line for line in lines if f'pid={app.pid} ' in line]
        if any('字幕窗口已提交' in line for line in own_lines):
            break
        time.sleep(.5)
    own_lines = [line for line in lines if f'pid={app.pid} ' in line]
    after = json.loads(config_path.read_text(encoding='utf-8'))
    changes = {key: {'before': preserved[key], 'after': after.get(key)} for key in preserved if preserved[key] != after.get(key)}
    if set(changes) == {'overlay_geometry'}:
        before_size = re.match(r'\d+x\d+', preserved['overlay_geometry']).group()
        after_size = re.match(r'\d+x\d+', after['overlay_geometry']).group()
        if before_size == after_size:
            restore_overlay_geometry(app.pid, preserved['overlay_geometry'], config_path)
            after = json.loads(config_path.read_text(encoding='utf-8'))
            changes = {key: {'before': preserved[key], 'after': after.get(key)} for key in preserved if preserved[key] != after.get(key)}
    from PIL import ImageGrab
    captures = 0
    for index, window in enumerate(windows_for_pid(app.pid)):
        capture_visible_window(window,args.output / f'window-{index}.png')
        captures += 1
    result = {'pid': app.pid, 'exe': str(exe), 'version': receipt['version'], 'alive': app.poll() is None,
              'source_submissions': sum('字幕窗口已提交' in line for line in own_lines),
              'configuration_applied': any('识别配置已生效' in line for line in own_lines),
              'errors': sum(' ERROR ' in line for line in own_lines), 'existing_field_changes': changes,
              'same_language_skips_translation': preserved.get('source_language') == preserved.get('target_language'),
              'captures': captures, 'processes': process_snapshot(exe), 'old_application_exited': not old or all(
                  x['ProcessId'] not in {r['ProcessId'] for r in all_instances()} for x in old)}
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))
    if not (result['alive'] and result['source_submissions'] and result['configuration_applied']
            and result['errors'] == 0 and not changes and result['captures']):
        raise SystemExit('Activation needs investigation; current app remains available')


if __name__ == '__main__':
    main()
