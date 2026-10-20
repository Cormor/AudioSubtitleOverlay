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
from install_windows import install_package, sha256


def main():
    parser=argparse.ArgumentParser(description="打包声幕并更新本机安装版")
    parser.add_argument("--source",type=Path,default=ROOT/"dist_minimal/AudioSubtitleOverlay")
    parser.add_argument("--output-dir",type=Path,default=ROOT/"releases")
    parser.add_argument("--package-only",action="store_true",help="仅供 CI 构建；不能证明用户设备已安装")
    args=parser.parse_args()
    if args.package_only and not os.environ.get("GITHUB_ACTIONS"):
        parser.error("本地发布必须同步安装；--package-only 仅用于 CI")
    version=(ROOT/"VERSION").read_text(encoding="utf-8").strip()
    build=json.loads((args.source/"_internal/BUILD_INFO.json").read_text(encoding="utf-8"))
    if build["version"]!=version or build["source_state"]!="clean":
        raise RuntimeError("构建版本不一致或源码未提交；未打包、安装或发布")
    if not re.fullmatch(r"[0-9a-f]{40}",build["source_commit"]):
        raise RuntimeError("构建缺少准确的源码提交，未打包")
    output=args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=True)
    main_zip=output/f"AudioSubtitleOverlay_精简版_v{version}.zip"
    model_zip=output/f"AudioSubtitleOverlay_tiny模型_v{version}.zip"
    if main_zip.exists() or model_zip.exists():
        raise FileExistsError("该目录已有同版本包；未覆盖。开发构建请指定新的 --output-dir，正式发布请使用新版本号。")
    subprocess.run([sys.executable,"-m","unittest","discover","-s","tests"],cwd=ROOT,check=True)
    main_archive(args.source,main_zip)
    model_archive(ROOT/"assets/tiny",model_zip)
    if main_zip.stat().st_size>150_000_000:
        raise RuntimeError("主程序包超过 150,000,000 字节，未安装或发布")
    hashes={main_zip.name:sha256(main_zip),model_zip.name:sha256(model_zip)}
    (output/"SHA256SUMS.txt").write_text("".join(f"{digest}  {name}\n" for name,digest in hashes.items()),encoding="utf-8")
    installation=None if args.package_only else install_package(main_zip)
    receipt=dict(version=version,created_at=datetime.now().astimezone().isoformat(),
                 files=hashes,installation=installation,ci_package_only=args.package_only)
    (output/"delivery.local.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"version":version,"packages":list(hashes),"installed":installation is not None},ensure_ascii=True))


if __name__=="__main__":main()
