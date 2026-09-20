"""加载本地识别提示词表，并生成长度受控的提示词。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re


_LEXICON_FILES = {
    "日常": ("daily.txt",),
    "游戏": ("gaming.txt",),
    "计算机": ("computing.txt",),
}
_LEXICON_MODES = {
    "关闭": (),
    "游戏": ("游戏",),
    "计算机": ("计算机",),
    "游戏、计算机": ("游戏", "计算机"),
    # 兼容旧调用；旧的通用模式不再把日常短语加入识别提示。
    "日常": (),
    "日常、游戏、计算机": ("游戏", "计算机"),
}
_CUSTOM_SPLIT = re.compile(r"[\s,，;；、]+")
_BUILTIN_MAX_CHARACTERS = 32
_BUILTIN_MIN_TERM_LENGTH = 4
_BUILTIN_MAX_TERM_LENGTH = 12


def _lexicon_root() -> Path:
    return Path(__file__).resolve().parent / "assets" / "lexicon"


@lru_cache(maxsize=None)
def _load_file(file_name: str) -> tuple[tuple[str, float], ...]:
    """读取一个按优先级排列的词表文件。"""
    path = _lexicon_root() / file_name
    if not path.exists():
        return ()
    items: list[tuple[str, float]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        term = parts[0].strip()
        if not term or "\n" in term or "\r" in term:
            continue
        try:
            priority = float(parts[1]) if len(parts) > 1 else 0.0
        except ValueError:
            priority = 0.0
        items.append((term, priority))
    return tuple(items)


def _custom_terms(text: str) -> list[str]:
    return [term.strip() for term in _CUSTOM_SPLIT.split(text.strip()) if term.strip()]


def _usable_term(term: str, *, builtin: bool = False) -> bool:
    if not term or any(char in term for char in "\r\n\t"):
        return False
    if builtin:
        return _BUILTIN_MIN_TERM_LENGTH <= len(term) <= _BUILTIN_MAX_TERM_LENGTH
    return 2 <= len(term) <= 32


@lru_cache(maxsize=256)
def build_hotwords(lexicon_mode: str, custom_text: str, max_characters: int = 180) -> str:
    """按词表选择少量提示词，避免普通短语干扰识别。"""
    selected: list[str] = []
    selected_keys: set[str] = set()
    used_characters = 0
    builtin_characters = 0

    def add(term: str, *, builtin: bool = False) -> bool:
        nonlocal builtin_characters, used_characters
        if not _usable_term(term, builtin=builtin) or term in selected_keys:
            return False
        extra = len(term) + (1 if selected else 0)
        if used_characters + extra > max_characters:
            return False
        if builtin and builtin_characters + extra > min(max_characters, _BUILTIN_MAX_CHARACTERS):
            return False
        selected.append(term)
        selected_keys.add(term)
        used_characters += extra
        if builtin:
            builtin_characters += extra
        return True

    for term in _custom_terms(custom_text):
        add(term)

    categories = _LEXICON_MODES.get(lexicon_mode, ())
    entries_by_category = [
        [
            item
            for file_name in _LEXICON_FILES[category]
            for item in _load_file(file_name)
        ]
        for category in categories
    ]
    positions = [0] * len(entries_by_category)
    while (
        entries_by_category
        and used_characters < max_characters
        and builtin_characters < min(max_characters, _BUILTIN_MAX_CHARACTERS)
    ):
        added_in_round = False
        for index, (entries, position) in enumerate(zip(entries_by_category, positions)):
            if builtin_characters >= min(max_characters, _BUILTIN_MAX_CHARACTERS):
                break
            while position < len(entries):
                term = entries[position][0]
                position += 1
                if add(term, builtin=True):
                    added_in_round = True
                    break
            positions[index] = position
        if not added_in_round:
            break

    return " ".join(selected)
