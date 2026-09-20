"""下载并整理识别提示词表。"""

from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
import re
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "lexicon"
USER_AGENT = "AudioSubtitleOverlay-lexicon-builder/1.0"
CASSOTIS_COMMIT = "029841eb409521e0a8c8c0321a9e763bcbff7b24"
THUOCL_COMMIT = "a30ce79d895d01ab5132a5c74c29703ff7efb4cc"

SOURCES = {
    "daily": (
        f"https://raw.githubusercontent.com/shenmin/cassotis-lexicon/{CASSOTIS_COMMIT}/"
        "manifests/curated_daily_phrases.tsv",
        f"https://raw.githubusercontent.com/shenmin/cassotis-lexicon/{CASSOTIS_COMMIT}/"
        "manifests/curated_daily_supplement_phrases.tsv",
    ),
    "gaming": (
        f"https://raw.githubusercontent.com/shenmin/cassotis-lexicon/{CASSOTIS_COMMIT}/"
        "manifests/vertical/gaming.tsv",
        f"https://raw.githubusercontent.com/shenmin/cassotis-lexicon/{CASSOTIS_COMMIT}/"
        "manifests/vertical/game_dev.tsv",
    ),
    "computing": (
        f"https://raw.githubusercontent.com/shenmin/cassotis-lexicon/{CASSOTIS_COMMIT}/"
        "manifests/vertical/computing.tsv",
        f"https://raw.githubusercontent.com/thunlp/THUOCL/{THUOCL_COMMIT}/data/THUOCL_IT.txt",
    ),
}


def download(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def parse_cassotis(text: str) -> list[tuple[str, float]]:
    entries = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if not columns:
            continue
        term = columns[0].strip()
        if not term:
            continue
        try:
            priority = float(columns[2])
        except (IndexError, ValueError):
            priority = 0.5
        entries.append((term, priority))
    return entries


def parse_thuocl(text: str) -> list[tuple[str, float]]:
    entries = []
    pattern = re.compile(r"^(.*?)\s+(\d+)\s*$")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        term, frequency = match.groups()
        # 用对数压缩词频，避免高频通用词完全挤掉专业术语。
        priority = min(1.0, math.log10(int(frequency) + 1) / 7.0)
        entries.append((term.strip(), priority))
    return entries


def merge_entries(texts: list[str], category: str) -> list[tuple[str, float]]:
    merged: defaultdict[str, float] = defaultdict(float)
    for index, text in enumerate(texts):
        entries = parse_thuocl(text) if category == "computing" and index == 1 else parse_cassotis(text)
        for term, priority in entries:
            if len(term) < 2 or len(term) > 32:
                continue
            merged[term] = max(merged[term], priority)
    return sorted(merged.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))


def write_entries(path: Path, entries: list[tuple[str, float]], source_name: str) -> None:
    lines = [
        "# 识别提示词表；格式：词条<TAB>优先级",
        f"# 来源：{source_name}",
    ]
    lines.extend(f"{term}\t{priority:.6f}" for term, priority in entries)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for category, urls in SOURCES.items():
        texts = [download(url) for url in urls]
        entries = merge_entries(texts, category)
        write_entries(
            OUTPUT / f"{category}.txt",
            entries,
            "、".join(urls),
        )
        print(category, len(entries))


if __name__ == "__main__":
    main()
