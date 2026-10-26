"""主窗口可独立验证的模型说明与运行状态文案。"""

from __future__ import annotations

import math

MODEL_FILTERS = ("全部模型", "CPU 五语", "CPU 中英实时", "英语专用", "Whisper 多语")


def keep_readonly_value_visible(widget) -> bool:
    """Readonly combo focus must not leave its selected value painted off-view."""
    if not widget.instate(("readonly",)):
        return False
    widget.selection_clear()
    widget.icursor(0)
    widget.xview_moveto(0)
    return True


def model_group(key: str) -> str:
    if key.startswith("sensevoice-"):
        return "CPU 五语"
    if key.startswith("zipformer-zh-en"):
        return "CPU 中英实时"
    if key.endswith(".en") or key.startswith("distil-") or key.startswith("zipformer-en"):
        return "英语专用"
    return "Whisper 多语"


def model_label(key: str, display_name: str = "") -> str:
    names = {
        "sensevoice-int8": "SenseVoice · CPU 五语",
        "zipformer-zh-en-int8": "Zipformer · CPU 中英实时（实验）",
        "zipformer-en-int8": "Zipformer · CPU 英语实时",
        "tiny": "Whisper Tiny · 最小体积",
        "base": "Whisper Base · 较小体积",
        "small": "Whisper Small · 中等体积",
        "medium": "Whisper Medium · 较大体积",
        "large-v3": "Whisper Large v3 · 完整大模型",
        "large-v3-turbo": "Whisper Large v3 Turbo",
        "tiny.en": "Whisper Tiny · 英语专用",
        "base.en": "Whisper Base · 英语专用",
        "small.en": "Whisper Small · 英语专用",
        "medium.en": "Whisper Medium · 英语专用",
        "distil-large-v3": "Distil Large v3 · 英语专用",
    }
    return names.get(key, display_name or key)


def model_guidance(key: str) -> str:
    group = model_group(key)
    if group == "CPU 五语":
        return "中文、粤语、英语、日语、韩语；只用 CPU。实时结果会继续修订。"
    if group == "CPU 中英实时":
        return "实验模型：中文、英语与中英混说；只用 CPU。本机口语样本有错漏，启用翻译时需选定声音的语言。"
    if group == "英语专用":
        return "仅识别英语；其他语言请换模型。" + ("只用 CPU。" if key.startswith("zipformer-") else "可用 CPU 或 NVIDIA GPU。")
    if key in {"tiny", "base"}:
        return "多语言，下载体积较小；对复杂口音、噪声和长句的识别需实际比较。"
    if key in {"large-v3", "large-v3-turbo", "medium"}:
        return "多语言，模型较大；需留意处理速度和待处理音频，支持 NVIDIA GPU。"
    return "多语言，可用 CPU 或 NVIDIA GPU；速度与识别效果取决于设备和声音。"


def finite_nonnegative(value) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    return max(0.0, value) if math.isfinite(value) else None


def runtime_view(phase: str, rms=None, backlog=None) -> tuple[str, str]:
    """phase来自明确生命周期事件，不解析后台状态文字。"""
    if phase == "preparing":
        return "正在准备", "正在加载模型与打开音源；首次使用可能需要下载。"
    if phase == "waiting_audio":
        return "等待声音", "播放一段带人声的内容；一直无输入时刷新音源并选实际播放设备。"
    if phase == "recognizing":
        pending = finite_nonnegative(backlog)
        if pending is not None and pending >= 2:
            return "正在识别 · 有积压", "正在追赶声音。积压持续增加时可停止后选择更小模型或调整设备。"
        level = finite_nonnegative(rms)
        if level is not None and level < .001:
            return "正在识别 · 声音较轻", "当前输入较轻；若持续没有文字，检查实际音源。已有字幕继续保留。"
        return "正在识别", "字幕会先出草稿再修订；点“字幕窗”可查看覆盖在视频上的字幕。"
    if phase == "stopping":
        return "正在释放资源", "正在关闭采集与识别；完成后即可重新开始。"
    if phase == "error":
        return "需要处理", "查看具体原因，调整音源、语言或模型；停止完成后点击重新开始。"
    if phase == "stopped":
        return "已停止", "调整音源、语言或模型后，可以重新开始。"
    return "准备就绪", "选好音源、声音的语言和模型，点击开始字幕。"


def compact_status(text: str, limit: int = 96) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def empty_transcript_hint(running: bool) -> str:
    return ("还没有字幕可复制或导出。请先播放带人声的内容，确认上方有输入；仍无字幕可打开排查。"
            if running else "还没有字幕可复制或导出。选择音源后点击开始字幕，再播放带人声的内容。")
