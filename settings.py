"""应用配置的读取和保存。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


RECOGNITION_BEAM_SIZES = (1, 3, 5)
RECOGNITION_TEMPERATURE_SCHEDULES = ("0", "0,0.2,0.4")
RECOGNITION_LEXICON_OPTIONS = (
    "关闭",
    "游戏",
    "计算机",
    "游戏、计算机",
)


def application_data_dir() -> Path:
    """返回 Windows 用户级配置目录。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        path = Path(appdata) / "AudioSubtitleOverlay"
    else:
        path = Path.home() / ".audio-subtitle-overlay"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    """应用运行所需的持久化配置。"""

    source_language: str = "自动检测"
    target_language: str = "中文"
    recognition_model: str = "tiny"
    recognition_device: str = "自动（优先GPU）"
    recognition_beam_size: int = 1
    recognition_condition_on_previous_text: bool = False
    recognition_temperature_schedule: str = "0"
    recognition_lexicon: str = "关闭"
    recognition_hotwords: str = ""
    model_path: str = ""
    loopback_device: str = ""
    translation_backend: str = "本地优先，失败转在线"
    overlay_background: str = "#101827"
    overlay_text_color: str = "#ffffff"
    overlay_outline: str = "#000000"
    overlay_translation_text_color: str = "#b8d7ff"
    overlay_translation_outline: str = "#000000"
    overlay_background_opacity: float = 0.92
    overlay_text_opacity: float = 1.0
    overlay_scroll_interval: float = 3.0
    overlay_font_size: int = 24
    overlay_translation_font_size: int = 20
    overlay_topmost: bool = True
    overlay_geometry: str = "680x180+80+80"

    @classmethod
    def load(cls) -> "Settings":
        """读取配置文件；文件损坏时使用默认配置。"""
        path = application_data_dir() / "settings.json"
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        allowed = {field.name for field in fields(cls)}
        values = {key: value for key, value in payload.items() if key in allowed}
        # 兼容旧版本只保存一个整体透明度的配置文件。
        if "overlay_background_opacity" not in values and "overlay_opacity" in payload:
            values["overlay_background_opacity"] = payload["overlay_opacity"]
        for key in ("overlay_background_opacity", "overlay_text_opacity"):
            if key in values:
                try:
                    values[key] = min(1.0, max(0.0, float(values[key])))
                except (TypeError, ValueError):
                    values.pop(key)
        if "recognition_beam_size" in values:
            try:
                beam_size = int(values["recognition_beam_size"])
                values["recognition_beam_size"] = (
                    beam_size if beam_size in RECOGNITION_BEAM_SIZES else cls.recognition_beam_size
                )
            except (TypeError, ValueError):
                values.pop("recognition_beam_size")
        if "recognition_condition_on_previous_text" in values:
            condition = values["recognition_condition_on_previous_text"]
            if isinstance(condition, str):
                values["recognition_condition_on_previous_text"] = condition.lower() in {
                    "1", "true", "yes", "on", "启用"
                }
            else:
                values["recognition_condition_on_previous_text"] = bool(condition)
        if "recognition_temperature_schedule" in values and values[
            "recognition_temperature_schedule"
        ] not in RECOGNITION_TEMPERATURE_SCHEDULES:
            values.pop("recognition_temperature_schedule")
        if "recognition_lexicon" in values and values["recognition_lexicon"] not in RECOGNITION_LEXICON_OPTIONS:
            values.pop("recognition_lexicon")
        if "recognition_hotwords" in values and not isinstance(values["recognition_hotwords"], str):
            values.pop("recognition_hotwords")
        try:
            return cls(**values)
        except (TypeError, ValueError):
            return cls()

    def save(self) -> None:
        """保存配置文件。"""
        path = application_data_dir() / "settings.json"
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
