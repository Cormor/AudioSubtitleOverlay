"""Bounded recognition parameter values shared by UI, saved settings and decoding.

Recommended values are shortcuts. The bounds below are product choices, except
for finite/probability/type rules and positive VAD silence required by the
current segmented-session endpoint condition.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from types import MappingProxyType

_MAX_NUMBER_TEXT = 64
_MAX_TEMPERATURE_TEXT = 512
_MAX_TEMPERATURES = 8
# Eight finite binary floats have at most 326 characters each in ordinary
# decimal form. Persisted canonical text must not inherit edit-buffer limits.
_MAX_COMMITTED_TEMPERATURE_TEXT = 4096
_MAX_COMMITTED_TEMPERATURE_NUMBER = 512
_INTEGER_TEXT = re.compile(r"^[+-]?[0-9]+$")
_DECIMAL_TEXT = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$")
_TEMPERATURE_SEPARATOR = re.compile("[,，、]")


class ParameterValueError(ValueError):
    """An input cannot be committed without changing its requested value."""


def number_text(value: int | float) -> str:
    """Use ordinary decimal text without rounding the decoder's float value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParameterValueError("需要有限数值")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ParameterValueError("数值必须有限")
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if Decimal(text) == 0 else text


@dataclass(frozen=True)
class NumericParameter:
    name: str
    label: str
    integer: bool
    minimum: int | float
    maximum: int | float
    recommended: tuple[int | float, ...]

    @property
    def allowed_text(self) -> str:
        return f"{number_text(self.minimum)}–{number_text(self.maximum)}"

    def _checked(self, value: int | float) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterValueError(f"{self.label}需要数值")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ParameterValueError(f"{self.label}必须是有限数值")
        if self.integer and type(value) is not int:
            raise ParameterValueError(f"{self.label}必须是整数，不会自动取整")
        if not self.minimum <= value <= self.maximum:
            raise ParameterValueError(f"{self.label}允许{self.allowed_text}")
        return int(value) if self.integer else float(value)

    def parse_text(self, text: str, *, max_chars: int = _MAX_NUMBER_TEXT) -> int | float:
        if not isinstance(text, str):
            raise ParameterValueError(f"{self.label}需要文本输入")
        text = text.strip()
        if not text:
            raise ParameterValueError(f"请输入{self.label}")
        if len(text) > max_chars:
            raise ParameterValueError(f"{self.label}最多输入{max_chars}个字符")
        pattern = _INTEGER_TEXT if self.integer else _DECIMAL_TEXT
        if pattern.fullmatch(text) is None:
            kind = "整数" if self.integer else "完整十进制数"
            raise ParameterValueError(f"{self.label}请输入{kind}，不使用科学计数")
        try:
            decimal = Decimal(text)
        except InvalidOperation as error:
            raise ParameterValueError(f"{self.label}不是有效数值") from error
        if not Decimal(str(self.minimum)) <= decimal <= Decimal(str(self.maximum)):
            raise ParameterValueError(f"{self.label}允许{self.allowed_text}")
        result = int(decimal) if self.integer else float(decimal)
        if decimal != 0 and result == 0:
            raise ParameterValueError(f"{self.label}太小，无法表示为识别参数")
        return self._checked(result)

    def from_config(self, value) -> int | float:
        # JSON may encode a committed float as 1e-7. It is already a number,
        # not an uncommitted scientific-notation edit string.
        return self._checked(value)


PARAMETERS = MappingProxyType({
    "beam_size": NumericParameter("beam_size", "搜索范围", True, 1, 16, (1, 3, 5)),
    "window_seconds": NumericParameter("window_seconds", "最大音频窗口（秒）", True, 1, 30, (8, 12, 16)),
    "update_interval": NumericParameter("update_interval", "最短更新间隔（秒）", False, .1, 5, (.1, .25, .5, .75, 1.0)),
    "repetition_penalty": NumericParameter("repetition_penalty", "重复惩罚", False, .1, 5, (1.0, 1.05, 1.1, 1.2)),
    "no_repeat_ngram_size": NumericParameter("no_repeat_ngram_size", "禁止重复词元组", True, 0, 64, (0, 3, 5, 8)),
    "no_speech_threshold": NumericParameter("no_speech_threshold", "无语音判定阈值", False, 0, 1, (.3, .6, .8)),
    "vad_silence_ms": NumericParameter("vad_silence_ms", "语音分段停顿（毫秒）", True, 1, 5000, (200, 400, 600, 800)),
})
RECOGNITION_BEAM_SIZES = PARAMETERS["beam_size"].recommended
RECOGNITION_NUMERIC_OPTIONS = {
    name: item.recommended for name, item in PARAMETERS.items() if name != "beam_size"
}
RECOGNITION_TEMPERATURE_SCHEDULES = ("0", "0,0.2,0.4")
_TEMPERATURE_VALUE = NumericParameter("temperature", "温度", False, 0, 1, ())


def parse_numeric_text(name: str, text: str) -> int | float:
    return PARAMETERS[name].parse_text(text)


def numeric_value(name: str, value) -> int | float:
    return PARAMETERS[name].from_config(value)


def _temperature_values(value, *, text_limit: int, item_limit: int) -> tuple[float, ...]:
    if isinstance(value, str):
        if len(value.strip()) > text_limit:
            raise ParameterValueError(f"温度序列最多输入{text_limit}个字符")
        pieces = _TEMPERATURE_SEPARATOR.split(value)
        if not 1 <= len(pieces) <= _MAX_TEMPERATURES:
            raise ParameterValueError(f"温度序列需要1–{_MAX_TEMPERATURES}项")
        values = [_TEMPERATURE_VALUE.parse_text(piece, max_chars=item_limit) for piece in pieces]
    elif isinstance(value, (tuple, list)):
        if not 1 <= len(value) <= _MAX_TEMPERATURES:
            raise ParameterValueError(f"温度序列需要1–{_MAX_TEMPERATURES}项")
        values = [_TEMPERATURE_VALUE.from_config(piece) for piece in value]
    else:
        raise ParameterValueError("温度需要非空逗号序列")
    if not 1 <= len(values) <= _MAX_TEMPERATURES:
        raise ParameterValueError(f"温度序列需要1–{_MAX_TEMPERATURES}项")
    if len(set(values)) != len(values):
        raise ParameterValueError("温度序列不能重复，不会自动删除重复项")
    return tuple(float(item) for item in values)


def temperature_values(value: str | tuple[float, ...] | list[float]) -> tuple[float, ...]:
    """Read a committed value, including canonical text expanded from a float."""
    return _temperature_values(value, text_limit=_MAX_COMMITTED_TEMPERATURE_TEXT,
                               item_limit=_MAX_COMMITTED_TEMPERATURE_NUMBER)


def parse_temperature_text(text: str) -> tuple[float, ...]:
    """Validate an edit and ensure its canonical form is editable again."""
    if not isinstance(text, str):
        raise ParameterValueError("温度需要逗号分隔的文本")
    values = _temperature_values(text, text_limit=_MAX_TEMPERATURE_TEXT,
                                 item_limit=_MAX_NUMBER_TEXT)
    canonical = [number_text(item) for item in values]
    if any(len(item) > _MAX_NUMBER_TEXT for item in canonical) or len(",".join(canonical)) > _MAX_TEMPERATURE_TEXT:
        raise ParameterValueError("温度规范表示超出输入长度，请使用可完整显示的数值；未应用")
    return values


def temperature_text(value: str | tuple[float, ...] | list[float]) -> str:
    return ",".join(number_text(item) for item in temperature_values(value))


def parameter_is_consumed(model: str, name: str) -> bool:
    """Match the three existing pipeline consumer paths, not a future SDK."""
    if model == "sensevoice-int8":
        return name == "vad_silence_ms"
    if model.startswith("zipformer-"):
        return False
    return name in PARAMETERS or name in {"temperature_schedule", "condition_on_previous_text"}


class ParameterDraft:
    """One recognition field: uncommitted text never mutates its typed value."""
    def __init__(self, name: str, committed):
        if name not in PARAMETERS and name != "temperature_schedule":
            raise KeyError(name)
        self.name = name
        self.set_committed(committed)

    @property
    def committed(self):
        return self._committed

    @property
    def current_text(self) -> str:
        return temperature_text(self._committed) if self.name == "temperature_schedule" else number_text(self._committed)

    @property
    def dirty(self) -> bool:
        return self.draft_text != self.current_text

    def set_committed(self, value) -> None:
        self._committed = (temperature_values(value) if self.name == "temperature_schedule"
                           else numeric_value(self.name, value))
        self.cancel()

    def edit(self, text: str) -> None:
        self.draft_text = text
        self.error = ""

    def cancel(self) -> None:
        self.draft_text = self.current_text
        self.error = ""

    def submit(self) -> bool:
        if not self.dirty:
            self.error = ""
            return False
        try:
            value = (parse_temperature_text(self.draft_text) if self.name == "temperature_schedule"
                     else parse_numeric_text(self.name, self.draft_text))
        except ParameterValueError as error:
            self.error = str(error)
            return False
        changed = value != self._committed
        self.set_committed(value)
        return changed
