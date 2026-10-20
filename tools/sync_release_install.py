"""Install the exact public GitHub package after checking its published SHA-256."""

import argparse
import json
from pathlib import Path
import re
import urllib.request
import uuid
import zipfile

from install_windows import install_package, sha256

REPOSITORY = "Cormor/AudioSubtitleOverlay"


def read_url(url):
    request=urllib.request.Request(url,headers={"User-Agent":"Shengmu-local-release-sync"})
    return urllib.request.urlopen(request,timeout=60)


def main():
    parser=argparse.ArgumentParser(description="更新本机声幕为与 GitHub 发布包一致的安装版")
    parser.add_argument("--version",help="例如 v1.3.0；默认最新正式发布")
    args=parser.parse_args()
    endpoint=f"https://api.github.com/repos/{REPOSITORY}/releases/"+(f"tags/{args.version}" if args.version else "latest")
    with read_url(endpoint) as stream:
        release=json.load(stream)
    version=release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch(r"v\d+\.\d+\.\d+",version):
        raise RuntimeError("目标不是正式版本")
    name=f"AudioSubtitleOverlay_Portable_{version}.zip"
    assets={item["name"]:item for item in release["assets"]}
    with read_url(assets["SHA256SUMS.txt"]["browser_download_url"]) as stream:
        checksums=stream.read().decode("utf-8-sig")
    expected=None
    for line in checksums.splitlines():
        parts=line.split()
        if len(parts)==2 and parts[1].lstrip("*")==name and re.fullmatch(r"[0-9a-fA-F]{64}",parts[0]):
            expected=parts[0].lower()
    if expected is None:
        raise RuntimeError("发布校验清单缺少主程序哈希")
    output=Path(__file__).resolve().parents[1]/"releases"/version
    output.mkdir(parents=True,exist_ok=True)
    package=output/name
    if not package.exists():
        temporary=output/(name+f".{uuid.uuid4().hex[:8]}.part")
        with read_url(assets[name]["browser_download_url"]) as source,temporary.open("xb") as target:
            while block:=source.read(1024*1024):target.write(block)
        if sha256(temporary)!=expected:
            raise RuntimeError("下载包 SHA-256 不匹配，未安装；保留下载文件供检查")
        temporary.rename(package)
    if sha256(package)!=expected:
        raise RuntimeError("本地已有同名包与发布校验值不一致，未覆盖")
    with zipfile.ZipFile(package) as archive:
        build=json.loads(archive.read("AudioSubtitleOverlay/_internal/BUILD_INFO.json"))
    if build["version"]!=version.lstrip("v") or build["source_state"]!="clean":
        raise RuntimeError("包内构建版本或源码状态不符合正式发布，未安装")
    if build["source_commit"]!=release["target_commitish"]:
        raise RuntimeError("包内源码提交与 Release 目标不一致，未安装")
    result=install_package(package)
    receipt=dict(version=version,release_url=release["html_url"],package_sha256=expected,
                 executable_sha256=result["executable_sha256"],directory=result["directory"],
                 shortcut=result["shortcut"],source_commit=build["source_commit"])
    (output/"installed.local.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(receipt,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
