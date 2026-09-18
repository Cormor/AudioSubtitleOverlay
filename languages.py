"""语言代码、界面显示名称和下拉框排序。"""

LANGUAGES = {
    "中文": "zh",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "西班牙语": "es",
    "德语": "de",
    "葡萄牙语": "pt",
    "意大利语": "it",
    "俄语": "ru",
    "阿拉伯语": "ar",
    "泰语": "th",
    "越南语": "vi",
    "土耳其语": "tr",
    "荷兰语": "nl",
    "波兰语": "pl",
    "乌克兰语": "uk",
    "自动检测": None,
}

PRIORITY_LANGUAGE_NAMES = ("中文", "英语", "日语", "韩语", "法语", "西班牙语")


def _other_language_names() -> list[str]:
    """返回排在优先语言之后的其他语言。"""
    excluded = set(PRIORITY_LANGUAGE_NAMES) | {"自动检测"}
    return [name for name in LANGUAGES if name not in excluded]


def source_language_names() -> list[str]:
    """返回源语言下拉框顺序，自动检测单独放在首项。"""
    return ["自动检测", *PRIORITY_LANGUAGE_NAMES, *_other_language_names()]


def target_language_names() -> list[str]:
    """返回目标语言下拉框顺序。"""
    return [*PRIORITY_LANGUAGE_NAMES, *_other_language_names()]


def language_code(display_name: str, allow_auto: bool = True) -> str | None:
    """将界面显示名称转换为 ISO 639-1 代码。"""
    if display_name in LANGUAGES:
        code = LANGUAGES[display_name]
        if code is not None or allow_auto:
            return code
    return None


def language_name(code: str | None) -> str:
    """将语言代码转换为界面显示名称。"""
    if not code:
        return "未知语言"
    for display_name, value in LANGUAGES.items():
        if value == code:
            return display_name
    return code
