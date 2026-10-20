"""Versioned per-user installation, verified before switching the sole Start-menu entry."""

import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import uuid
import zipfile

EXECUTABLE = "AudioSubtitleOverlay.exe"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_installation(directory, manifest):
    for name, expected in manifest["files"].items():
        path = directory / name
        if not path.is_file() or sha256(path) != expected:
            raise RuntimeError(f"安装文件校验失败：{name}")


def prepare_installation(package, install_root):
    package, install_root = Path(package).resolve(), Path(install_root).resolve()
    digest = sha256(package)
    with zipfile.ZipFile(package) as archive:
        # Validate the whole member list before writing anything.
        names = set()
        for item in archive.infolist():
            path = PurePosixPath(item.filename.replace("\\", "/"))
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != "AudioSubtitleOverlay" or ":" in str(path)
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError(f"安装包路径不安全：{item.filename}")
            if str(path).casefold() in names:
                raise ValueError(f"安装包含重复路径：{item.filename}")
            names.add(str(path).casefold())
        text = archive.read("AudioSubtitleOverlay/版本.txt").decode("utf-8-sig").strip()
        match = re.fullmatch(r"版本：v(\d+\.\d+\.\d+)", text)
        if not match:
            raise ValueError("安装包缺少有效版本信息")
        version = match[1]
        if archive.testzip() is not None:
            raise ValueError("安装包损坏")
        destination = install_root / f"v{version}"
        if destination.exists():
            old_manifest = destination / "installation.json"
            if old_manifest.is_file():
                previous = json.loads(old_manifest.read_text(encoding="utf-8"))
                if previous.get("package_sha256") == digest:
                    verify_installation(destination, previous)
                    return destination, previous
            destination = install_root / f"v{version}-{digest[:10]}"
        if destination.exists():
            previous = json.loads((destination / "installation.json").read_text(encoding="utf-8"))
            if previous.get("package_sha256") != digest:
                raise RuntimeError("已有同版本目录与安装包不一致；不覆盖现有文件")
            verify_installation(destination, previous)
            return destination, previous
        stage = install_root / f".installing-v{version}-{uuid.uuid4().hex[:8]}"
        stage.mkdir(parents=True)
        hashes = {}
        for item in archive.infolist():
            path = PurePosixPath(item.filename.replace("\\", "/"))
            target = stage.joinpath(*path.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            hashes[path.as_posix()] = sha256(target)
        executable = stage / "AudioSubtitleOverlay" / EXECUTABLE
        if not executable.is_file() or not (executable.parent / "_internal").is_dir():
            raise ValueError("安装包缺少主程序或运行依赖目录")
        manifest = dict(name="声幕", identity="AudioSubtitleOverlay", version=version,
                        package_sha256=digest, executable=f"AudioSubtitleOverlay/{EXECUTABLE}",
                        executable_sha256=sha256(executable), files=hashes)
        (stage / "installation.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        verify_installation(stage, manifest)
        stage.rename(destination)
        return destination, manifest


def powershell(script):
    prefix = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; "
    encoded = base64.b64encode((prefix+script).encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe","-NoProfile","-EncodedCommand",encoded],
                            capture_output=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def shortcuts():
    value = powershell("$w=New-Object -ComObject WScript.Shell; $items=@(Get-ChildItem ([Environment]::GetFolderPath('StartMenu')) -Filter *.lnk -Recurse | ForEach-Object { $s=$w.CreateShortcut($_.FullName); if([IO.Path]::GetFileName($s.TargetPath) -eq 'AudioSubtitleOverlay.exe') { [PSCustomObject]@{path=$_.FullName; target=$s.TargetPath; directory=$s.WorkingDirectory; icon=$s.IconLocation} } }); ConvertTo-Json -InputObject $items -Compress")
    return json.loads(value) if value else []


def root_from_target(target):
    path = Path(target)
    for parent in path.parents:
        if re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9a-f]+)?", parent.name):
            return parent.parent
    return path.parent


def install_package(package, install_root=None):
    if os.name != "nt":
        raise RuntimeError("本机安装需要在 Windows Python 中执行")
    existing = shortcuts()
    roots = {root_from_target(item["target"]).resolve() for item in existing}
    if install_root is None:
        if len(roots) > 1:
            raise RuntimeError("存在多个安装根目录，请用 --root 明确指定；未覆盖任何文件")
        install_root = next(iter(roots)) if roots else Path(os.environ["LOCALAPPDATA"]) / "Programs" / "AudioSubtitleOverlay"
    install_root = Path(install_root).resolve()
    if any(root != install_root for root in roots):
        raise RuntimeError("存在其他安装根目录，未切换快捷方式")
    directory, manifest = prepare_installation(package, install_root)
    executable = directory / manifest["executable"]
    start_menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
    start_menu.mkdir(parents=True, exist_ok=True)
    shortcut = start_menu / "声幕.lnk"
    known_paths = {Path(item["path"]).resolve() for item in existing}
    if shortcut.exists() and shortcut.resolve() not in known_paths:
        raise RuntimeError("声幕快捷方式名称已被其他应用占用，未覆盖")
    temporary = install_root / f".new-shortcut-{uuid.uuid4().hex}.lnk"
    powershell(f"$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut({literal(temporary)}); $s.TargetPath={literal(executable)}; $s.WorkingDirectory={literal(executable.parent)}; $s.IconLocation={literal(str(executable)+',0')}; $s.Description='声幕 · 实时字幕与翻译'; $s.Save()")
    temporary_target = powershell(f"$w=New-Object -ComObject WScript.Shell; $w.CreateShortcut({literal(temporary)}).TargetPath")
    if Path(temporary_target).resolve() != executable.resolve():
        raise RuntimeError("新快捷方式未正确准备；保留原入口")
    # Keep old shortcuts outside Start Menu as a reversible record.
    backup = install_root / "shortcut-backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for index, item in enumerate(existing):
        path = Path(item["path"])
        if root_from_target(item["target"]).resolve() == install_root and path.exists():
            backup.mkdir(parents=True, exist_ok=True)
            path.rename(backup / f"{index}-{path.name}")
    temporary.replace(shortcut)
    verify_installation(directory, manifest)
    readback = shortcuts()
    if len(readback) != 1 or Path(readback[0]["target"]).resolve() != executable.resolve():
        raise RuntimeError("开始菜单回读不一致；安装文件已保留，请检查快捷方式")
    if Path(readback[0]["directory"]).resolve() != executable.parent.resolve():
        raise RuntimeError("快捷方式工作目录不一致")
    icon_path, icon_index = readback[0]["icon"].rsplit(",", 1)
    if Path(icon_path).resolve() != executable.resolve() or icon_index.strip() != "0":
        raise RuntimeError("快捷方式图标不一致")
    result = dict(manifest, directory=str(directory), shortcut=str(shortcut))
    current = install_root / "current-installation.json"
    temp_manifest = install_root / ".current-installation.json.tmp"
    temp_manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    temp_manifest.replace(current)
    return result


def main():
    parser = argparse.ArgumentParser(description="安装或更新声幕；保留旧版本、配置和模型")
    parser.add_argument("package",type=Path)
    parser.add_argument("--root",type=Path)
    args = parser.parse_args()
    result = install_package(args.package,args.root)
    print(json.dumps({key:value for key,value in result.items() if key!="files"},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
