"""Package a tested build and update the local installation before declaring delivery."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from package_minimal import build_archive as main_archive
from package_tiny_model import build_archive as model_archive
from tools.install_windows import install_package, sha256


def verify_build(source, version, *, local_build=False, ci=False, root=ROOT):
    build=json.loads((source/"_internal/BUILD_INFO.json").read_text(encoding="utf-8"))
    if local_build and ci:
        raise RuntimeError("CI 不允许发布未提交的本地构建")
    allowed={"clean", "modified"} if local_build else {"clean"}
    if build["version"]!=version or build["source_state"] not in allowed:
        raise RuntimeError("构建版本或源码状态不符合要求；未打包、安装或发布")
    if not re.fullmatch(r"[0-9a-f]{40}",build["source_commit"]):
        raise RuntimeError("构建缺少准确的源码提交，未打包")
    if not build.get("source_files"):
        raise RuntimeError("构建缺少源码文件校验记录，未打包")
    for name, expected in build["source_files"].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or sha256(path)!=expected:
            raise RuntimeError(f"当前源码与构建不一致：{name}；请重新构建")
    return build


def main():
    parser=argparse.ArgumentParser(description="打包声幕并更新本机安装版")
    parser.add_argument("--source",type=Path,default=ROOT/"dist_minimal/AudioSubtitleOverlay")
    parser.add_argument("--output-dir",type=Path,default=ROOT/"releases")
    parser.add_argument("--package-only",action="store_true",help="仅供 CI 构建；不能证明用户设备已安装")
    parser.add_argument("--local-build",action="store_true",help="仅本机安装未提交源码；保留真实源码状态与文件哈希，禁止用于 CI 发布")
    parser.add_argument("--skip-model-package",action="store_true",help="仅本地构建：复用已有模型，不重复生成tiny模型归档")
    args=parser.parse_args()
    if args.skip_model_package and (not args.local_build or os.environ.get("GITHUB_ACTIONS")):
        parser.error("--skip-model-package 仅用于本机开发构建；正式发布仍须准备完整发布资产")
    if args.package_only and not os.environ.get("GITHUB_ACTIONS"):
        parser.error("本地发布必须同步安装；--package-only 仅用于 CI")
    version=(ROOT/"VERSION").read_text(encoding="utf-8").strip()
    build=verify_build(args.source,version,local_build=args.local_build,
                       ci=bool(os.environ.get("GITHUB_ACTIONS")))
    output=args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=True)
    main_zip=output/f"AudioSubtitleOverlay_精简版_v{version}.zip"
    model_zip=output/f"AudioSubtitleOverlay_tiny模型_v{version}.zip"
    if main_zip.exists() or (not args.skip_model_package and model_zip.exists()):
        raise FileExistsError("该目录已有同版本包；未覆盖。开发构建请指定新的 --output-dir，正式发布请使用新版本号。")
    subprocess.run([sys.executable,"-m","unittest","discover","-s","tests"],cwd=ROOT,check=True)
    main_archive(args.source,main_zip)
    if not args.skip_model_package:
        model_archive(ROOT/"assets/tiny",model_zip)
    if main_zip.stat().st_size>150_000_000:
        raise RuntimeError("主程序包超过 150,000,000 字节，未安装或发布")
    hashes={main_zip.name:sha256(main_zip)}
    if not args.skip_model_package:
        hashes[model_zip.name]=sha256(model_zip)
    (output/"SHA256SUMS.txt").write_text("".join(f"{digest}  {name}\n" for name,digest in hashes.items()),encoding="utf-8")
    installation=None if args.package_only else install_package(main_zip)
    receipt=dict(version=version,created_at=datetime.now().astimezone().isoformat(),
                 files=hashes,installation=installation,ci_package_only=args.package_only,
                 local_build=args.local_build,build=build)
    receipt["reused_local_models"]=args.skip_model_package
    (output/"delivery.local.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"version":version,"packages":list(hashes),"installed":installation is not None},ensure_ascii=True))


if __name__=="__main__":main()
