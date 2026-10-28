# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import hashlib
import json
import os
import subprocess
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all, copy_metadata

version_text = Path('VERSION').read_text(encoding='utf-8').strip()
version_tuple = tuple(int(part) for part in version_text.split('.')) + (0,)
windows_version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple, mask=0x3f,
                     flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('080404B0', [
        StringStruct('CompanyName', 'Cormor'),
        StringStruct('FileDescription', '声幕 · 实时字幕与翻译'),
        StringStruct('FileVersion', version_text),
        StringStruct('InternalName', 'AudioSubtitleOverlay'),
        StringStruct('OriginalFilename', 'AudioSubtitleOverlay.exe'),
        StringStruct('ProductName', '声幕'),
        StringStruct('ProductVersion', version_text),
    ])]), VarFileInfo([VarStruct('Translation', [2052, 1200])])],
)

metadata_dir = Path(os.environ.get('ASR_BUILD_METADATA_DIR', 'build_metadata'))
metadata_dir.mkdir(exist_ok=True)
source_commit = os.environ.get('GITHUB_SHA') or os.environ.get('SOURCE_COMMIT')
source_state = 'clean' if os.environ.get('GITHUB_SHA') else os.environ.get('SOURCE_STATE', 'unknown')
if not source_commit:
    try:
        source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL).decode().strip()
        source_state = 'clean' if subprocess.run(['git','diff','--quiet','HEAD'], stderr=subprocess.DEVNULL).returncode == 0 else 'modified'
    except (OSError, subprocess.CalledProcessError):
        source_commit = 'unknown'
source_paths = [*Path('.').glob('*.py'), Path('AudioSubtitleOverlay.spec'), Path('VERSION'),
                Path('component_manifest.json'), Path('design_tokens.json'), *Path('assets/brand').glob('*')]
source_hashes = {path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in source_paths if path.is_file()}
(metadata_dir / 'BUILD_INFO.json').write_text(json.dumps({
    'name': '声幕', 'version': version_text, 'source_commit': source_commit,
    'source_state': source_state, 'source_files': source_hashes,
}, ensure_ascii=False, indent=2), encoding='utf-8')

datas = [
    ('component_manifest.json', '.'),
    ('design_tokens.json', '.'),
    ('VERSION', '.'),
    (str(metadata_dir / 'BUILD_INFO.json'), '.'),
    ('assets/brand', 'assets/brand'),
]
datas += copy_metadata('pystray') + copy_metadata('six')
binaries = []
hiddenimports = ['pystray._win32', 'runtime_workers', 'asr_backends', 'transcription', 'translation', 'recognition_sessions']
hiddenimports += collect_submodules('soundcard')
tmp_ret = collect_all('faster_whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('ctranslate2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('opencc')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('huggingface_hub')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sentencepiece')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sherpa_onnx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# sherpa-onnx 2 splits native dependencies into a separate distribution.
datas += copy_metadata('sherpa-onnx-core')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AudioSubtitleOverlay',
    icon='assets/brand/icon.ico',
    version=windows_version,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AudioSubtitleOverlay',
)
