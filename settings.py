"""应用配置的读取和保存。"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import ClassVar


RECOGNITION_BEAM_SIZES = (1, 3, 5)
RECOGNITION_NUMERIC_OPTIONS = {
    "window_seconds": (8, 12, 16),
    "update_interval": (0.1, 0.25, 0.5, 0.75, 1.0),
    "repetition_penalty": (1.0, 1.05, 1.1, 1.2),
    "no_repeat_ngram_size": (0, 3, 5, 8),
    "no_speech_threshold": (0.3, 0.6, 0.8),
    "vad_silence_ms": (200, 400, 600, 800),
}
RECOGNITION_TEMPERATURE_SCHEDULES = ("0", "0,0.2,0.4")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_GEOMETRY_RE = re.compile(r"^\d{1,5}x\d{1,5}[+-]\d{1,6}[+-]\d{1,6}$")


class SettingsLoadError(ValueError):
    """配置无法安全保存时抛出，避免覆盖需要恢复的原始文件。"""


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

    schema_version: int = 2
    ui_theme: str = "system"

    auto_start: bool = False
    start_minimized: bool = False
    show_runtime_details: bool = False
    source_language: str = "自动检测"
    target_language: str = "中文"
    recognition_model: str = "sensevoice-int8"
    recognition_device: str = "自动（优先GPU）"
    recognition_beam_size: int = 1
    recognition_condition_on_previous_text: bool = False
    recognition_temperature_schedule: str = "0"
    recognition_window_seconds: int = 8
    recognition_update_interval: float = 0.1
    recognition_repetition_penalty: float = 1.0
    recognition_no_repeat_ngram_size: int = 0
    recognition_no_speech_threshold: float = 0.6
    recognition_vad_silence_ms: int = 400
    model_path: str = ""
    loopback_device: str = ""
    translation_enabled: bool = True
    translation_backend: str = "仅使用本地翻译"
    overlay_background: str = "#152033"
    overlay_text_color: str = "#ffffff"
    overlay_outline: str = "#000000"
    overlay_translation_text_color: str = "#b8d7ff"
    overlay_translation_outline: str = "#000000"
    overlay_background_opacity: float = 0.78
    overlay_text_opacity: float = 1.0
    overlay_scroll_interval: float = 1.8
    overlay_font_size: int = 24
    overlay_translation_font_size: int = 20
    overlay_topmost: bool = True
    overlay_auto_height: bool = True
    overlay_animation: bool = True
    overlay_geometry: str = "680x180+80+80"

    # 瞬态状态不属于持久化数据，也不进入构造参数或JSON。
    last_load_warning: ClassVar[str | None] = None
    _extra: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _load_error: str | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def load(cls) -> "Settings":
        """读取配置；结构损坏时返回默认值并阻止保存覆盖原始内容。"""
        path = application_data_dir() / "settings.json"
        cls.last_load_warning = None
        if not path.exists():
            return cls()
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非法数值 {value}")),
            )
        except (OSError, ValueError, UnicodeError) as exc:
            return cls._invalid_config(f"配置文件无法读取，原文件已保留：{exc}")
        if not isinstance(payload, dict):
            return cls._invalid_config("配置文件顶层必须是 JSON 对象，原文件已保留")
        allowed = {item.name for item in fields(cls) if item.init}
        values = {key: value for key, value in payload.items() if key in allowed}
        settings = cls()
        settings._extra = {key: value for key, value in payload.items() if key not in allowed}
        # 旧版启动后立即识别；迁移已有配置时保留这一选择。新安装仍由用户点击开始。
        values.setdefault("auto_start", True)
        values["schema_version"] = 2
        if values.get("ui_theme", "system") not in {"system", "dark", "light"}:
            values.pop("ui_theme", None)
            cls.last_load_warning = "主题设置无效，已使用系统主题"
        if values.get("translation_backend") == "仅使用本地 Argos":
            values["translation_backend"] = "仅使用本地翻译"
        # 兼容旧版本只保存一个整体透明度的配置文件。
        if "overlay_background_opacity" not in values and "overlay_opacity" in payload:
            values["overlay_background_opacity"] = payload["overlay_opacity"]
        for key in ("overlay_background_opacity", "overlay_text_opacity"):
            if key in values:
                try:
                    values[key] = float(values[key])
                    if not math.isfinite(values[key]) or not 0 <= values[key] <= 1:
                        raise ValueError(key)
                except (TypeError, ValueError):
                    return cls._invalid_config(f"{key} 无效，原配置已保留")
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
        for key in ("overlay_auto_height", "overlay_animation", "overlay_topmost", "translation_enabled", "auto_start", "start_minimized", "show_runtime_details"):
            if key in values and not isinstance(values[key], bool):
                values[key] = str(values[key]).lower() in {"1", "true", "yes", "on", "启用"}
        if "recognition_temperature_schedule" in values and values[
            "recognition_temperature_schedule"
        ] not in RECOGNITION_TEMPERATURE_SCHEDULES:
            values.pop("recognition_temperature_schedule")
        for name, choices in RECOGNITION_NUMERIC_OPTIONS.items():
            key = "recognition_" + name
            if key in values:
                try:
                    value = type(choices[0])(values[key])
                    if value not in choices:
                        raise ValueError(key)
                    values[key] = value
                except (TypeError, ValueError, OverflowError):
                    values.pop(key)
        for key in ("overlay_font_size", "overlay_translation_font_size"):
            if key in values and (type(values[key]) is not int or not 8 <= values[key] <= 96):
                return cls._invalid_config(f"{key} 超出有效范围，原配置已保留")
        for key in ("overlay_background", "overlay_text_color", "overlay_outline",
                    "overlay_translation_text_color", "overlay_translation_outline"):
            if key in values and (not isinstance(values[key], str) or not _COLOR_RE.fullmatch(values[key])):
                return cls._invalid_config(f"{key} 不是有效的十六进制颜色，原配置已保留")
        if "overlay_geometry" in values and (
            not isinstance(values["overlay_geometry"], str)
            or not _GEOMETRY_RE.fullmatch(values["overlay_geometry"])
        ):
            return cls._invalid_config("浮窗几何配置无效，原配置已保留")
        for key in ("overlay_scroll_interval",):
            if key in values:
                try:
                    values[key] = float(values[key])
                except (TypeError, ValueError):
                    return cls._invalid_config(f"{key} 无效，原配置已保留")
                if not math.isfinite(values[key]) or values[key] <= 0:
                    return cls._invalid_config(f"{key} 必须是有限正数，原配置已保留")
        for key in ("source_language", "target_language", "recognition_model", "recognition_device",
                    "model_path", "loopback_device", "translation_backend"):
            if key in values and not isinstance(values[key], str):
                return cls._invalid_config(f"{key} 类型无效，原配置已保留")
        try:
            loaded = cls(**values)
        except (TypeError, ValueError) as exc:
            return cls._invalid_config(f"配置字段无效，原配置已保留：{exc}")
        loaded._extra = settings._extra
        return loaded

    @classmethod
    def _invalid_config(cls, warning: str) -> "Settings":
        cls.last_load_warning = warning
        settings = cls()
        settings._load_error = warning
        return settings

    def save(self) -> None:
        """原子保存配置；损坏配置未显式修复前拒绝覆盖。"""
        if self._load_error:
            raise SettingsLoadError(self._load_error)
        path = application_data_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        persistent = {item.name: getattr(self, item.name) for item in fields(self) if item.init}
        payload = {**self._extra, **persistent}
        payload["schema_version"] = 2
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as temporary:
                temp_path = Path(temporary.name)
                json.dump(payload, temporary, ensure_ascii=False, indent=2, allow_nan=False)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_path, path)
        except BaseException:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
