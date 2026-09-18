"""字幕文字的中文书写形式规范化。"""

from functools import lru_cache
import re


_CHINESE_PUNCTUATION = str.maketrans(
    {
        ",": "，",
        ".": "。",
        "?": "？",
        "!": "！",
        ":": "：",
        ";": "；",
    }
)


# 内部文本净化：在翻译和历史记录前移除不需要的附加文本。
_SUBTITLE_CREDIT = re.compile(
    r"字幕\s*(?:[:：]\s*)?by\s*[@#\w](?:[\w.-]*)",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _simplified_converter():
    """复用词组转换词典，避免每次更新字幕重新加载。"""
    from opencc import OpenCC

    return OpenCC('t2s')


def remove_subtitle_credit(text: str) -> str:
    """移除“字幕by昵称”及其常见空格、大小写和分隔符变体。"""
    cleaned = _SUBTITLE_CREDIT.sub("", str(text))
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    return cleaned.strip()


def display_text(text: str, language: str | None) -> str:
    """中文结果统一显示简体；其他语言保留原文。"""
    cleaned = remove_subtitle_credit(text)
    if language == 'zh':
        return _simplified_converter().convert(cleaned).translate(_CHINESE_PUNCTUATION)
    return cleaned
