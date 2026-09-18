"""独立的字幕显示后处理模块，不依赖音频采集、识别模型或 Tk。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from text_processing import display_text


@dataclass
class SubtitleBlock:
    sequence: int
    source: str
    translation: str = ""
    language: str | None = None


class SubtitleHistory:
    """保留稳定字幕并管理当前段的即时草稿。"""

    def __init__(self):
        self.blocks: dict[int, SubtitleBlock] = {}
        self._sequence_blocks: dict[int, int] = {}
        self._preview_block_id: int | None = None
        self._preview_sequence = -1
        self._preview_source = ""
        self._pending_translations = {}

    def clear(self):
        self.blocks.clear()
        self._sequence_blocks.clear()
        self.clear_preview()
        self._pending_translations.clear()

    def clear_preview(self):
        """清除尚未经过质量通道确认的当前段草稿。"""
        self._preview_block_id = None
        self._preview_sequence = -1
        self._preview_source = ""

    def update_source(self, block_id, sequence, text, language):
        previous = self.blocks.get(block_id)
        if previous and sequence < previous.sequence:
            return False
        if previous:
            self._sequence_blocks.pop(previous.sequence, None)
        pending_translation = self._pending_translations.pop(sequence, None)
        if pending_translation is not None:
            # 同一段发生质量修订时，新序号的译文优先于旧版本译文。
            translation = pending_translation
        else:
            translation = previous.translation if previous else ""
        self.blocks[block_id] = SubtitleBlock(sequence, text, translation, language)
        self._sequence_blocks[sequence] = block_id
        # 质量版本覆盖同段草稿，不保留未经确认的旧尾部。
        if self._preview_block_id is not None and block_id >= self._preview_block_id:
            self.clear_preview()
        return True

    def update_preview(self, block_id, sequence, text, language):
        """接收完整段落草稿，以新版本替换旧版本。"""
        if sequence < self._preview_sequence:
            return False
        # 预览接口接收完整段落版本，不把无时间范围的短文本拼接成正文。
        candidate = text.strip(" \t")
        if not candidate:
            return False
        unchanged = (
            self._preview_block_id == block_id
            and self._preview_source == candidate
        )
        self._preview_block_id = block_id
        self._preview_sequence = sequence
        self._preview_source = candidate
        return bool(self._preview_source) and not unchanged

    def update_translation(self, sequence, text):
        block_id = self._sequence_blocks.get(sequence)
        if block_id is None:
            # 翻译线程可能先于界面线程的原文事件完成，先暂存，避免译文丢失。
            self._pending_translations[sequence] = text
            return False
        block = self.blocks[block_id]
        if block.translation == text:
            return False
        block.translation = text
        return True

    def entries(self):
        result = []
        for block_id, block in self.blocks.items():
            if block_id == self._preview_block_id:
                # 草稿对应的稳定翻译可能已经过时，质量结果到达前不显示旧译文。
                result.append((self._preview_source, ""))
            else:
                result.append((block.source, block.translation))
        if self._preview_block_id is not None and self._preview_block_id not in self.blocks:
            result.append((self._preview_source, ""))
        return result

    def entry_modes(self):
        """返回与 entries 对齐的正文状态，供渲染层区分稳定结果和草稿。"""
        result = []
        for block_id in self.blocks:
            result.append("preview" if block_id == self._preview_block_id else "stable")
        if self._preview_block_id is not None and self._preview_block_id not in self.blocks:
            result.append("preview")
        return result

    def latest_source(self) -> str:
        """返回当前应显示的最新原文。"""
        if self._preview_block_id is not None:
            return self._preview_source
        if not self.blocks:
            return ""
        return next(reversed(self.blocks.values())).source

    def translation_for_sequence(self, sequence: int) -> str:
        """返回指定识别序号已经挂接的译文。"""
        block_id = self._sequence_blocks.get(sequence)
        if block_id is None:
            return ""
        return self.blocks[block_id].translation


@dataclass(frozen=True)
class DisplayEvent:
    """识别或翻译层提交给显示模块的统一事件。"""

    kind: Literal["source", "preview", "translation"]
    sequence: int
    text: str
    language: str | None
    block_id: int | None = None


@dataclass(frozen=True)
class DisplaySnapshot:
    """一次 UI 刷新所需的完整、不可变显示数据。"""

    entries: tuple[tuple[str, str], ...]
    revision_modes: tuple[str, ...]
    latest_source: str
    latest_translation: str
    source_language: str | None
    detected_language: str | None
    target_language: str
    translation_visible: bool


class DisplayModule:
    """接收原文、即时预览和翻译结果，输出批量显示快照。"""

    def __init__(self, source_language: str | None, target_language: str) -> None:
        self.history = SubtitleHistory()
        self._source_language = source_language
        self._target_language = target_language
        self._detected_language: str | None = None
        self._latest_source_sequence = 0
        self._latest_source_text = ""
        self._latest_translation = ""
        self._latest_translation_sequence = -1
        self._dirty = True
        self._snapshot: DisplaySnapshot | None = None

    @property
    def latest_source_text(self) -> str:
        """返回当前原文标签需要显示的文字，不生成完整快照。"""
        return self._latest_source_text

    @property
    def latest_translation_text(self) -> str:
        """返回当前译文标签需要显示的文字，不生成完整快照。"""
        return self._latest_translation

    @property
    def translation_visible(self) -> bool:
        """返回当前语言设置下是否显示译文。"""
        return self._translation_visible()

    @staticmethod
    def _normalize_text(text: str, language: str | None) -> str:
        """只做显示层规范化，保留内部段落、换行和标点。"""
        normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
        return display_text(normalized, language).strip(" \t")

    def _translation_visible(self) -> bool:
        source = self._source_language or self._detected_language
        return source != self._target_language

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._snapshot = None

    def reset(self, source_language: str | None, target_language: str) -> None:
        """开始新会话并清空旧显示状态。"""
        self.history.clear()
        self._source_language = source_language
        self._target_language = target_language
        self._detected_language = None
        self._latest_source_sequence = 0
        self._latest_source_text = ""
        self._latest_translation = ""
        self._latest_translation_sequence = -1
        self._mark_dirty()

    def set_languages(self, source_language: str | None, target_language: str) -> bool:
        """更新显示语言设置，不触碰既有字幕历史。"""
        changed = (
            self._source_language != source_language
            or self._target_language != target_language
            or self._detected_language is not None
        )
        self._source_language = source_language
        self._target_language = target_language
        # 语言选择变化后，自动检测结果必须重新等待当前会话的下一条结果。
        self._detected_language = None
        if changed:
            self._mark_dirty()
        return changed

    def apply(self, event: DisplayEvent) -> bool:
        """按事件类型执行后处理；返回是否需要新的显示快照。"""
        if event.kind == "source":
            return self.apply_source(
                event.sequence,
                event.text,
                event.language,
                event.block_id,
            )
        if event.kind == "preview":
            if event.block_id is None:
                return False
            return self.apply_preview(
                event.sequence,
                event.text,
                event.language,
                event.block_id,
            )
        if event.kind == "translation":
            return self.apply_translation(event.sequence, event.text, event.language)
        raise ValueError(f"未知显示事件类型：{event.kind}")

    def apply_many(self, events: Iterable[DisplayEvent]) -> bool:
        """批量处理事件，只在调用方取快照时生成一次完整显示数据。"""
        changed = False
        for event in events:
            changed = self.apply(event) or changed
        return changed

    def apply_source(
        self,
        sequence: int,
        text: str,
        language: str | None,
        block_id: int | None = None,
    ) -> bool:
        """接收质量通道原文。"""
        if sequence < self._latest_source_sequence:
            return False
        normalized = self._normalize_text(text, language)
        if not normalized:
            return False
        self._latest_source_sequence = sequence
        language_changed = self._detected_language != language
        self._detected_language = language
        history_changed = self.history.update_source(
            block_id if block_id is not None else sequence,
            sequence,
            normalized,
            language,
        )
        attached_translation = self.history.translation_for_sequence(sequence)
        translation_changed = False
        if (
            attached_translation
            and sequence >= self._latest_translation_sequence
            and attached_translation != self._latest_translation
        ):
            self._latest_translation = attached_translation
            self._latest_translation_sequence = sequence
            translation_changed = True
        self._latest_source_text = self.history.latest_source()
        if history_changed or language_changed or translation_changed:
            self._mark_dirty()
        return history_changed or language_changed or translation_changed

    def apply_preview(
        self,
        sequence: int,
        text: str,
        language: str | None,
        block_id: int,
    ) -> bool:
        """接收短窗口即时预览。"""
        if sequence < self._latest_source_sequence:
            return False
        normalized = self._normalize_text(text, language)
        if not normalized:
            return False
        self._latest_source_sequence = sequence
        language_changed = bool(language and self._detected_language is None)
        if language_changed:
            self._detected_language = language
        history_changed = self.history.update_preview(
            block_id,
            sequence,
            normalized,
            language,
        )
        self._latest_source_text = self.history.latest_source()
        if history_changed or language_changed:
            self._mark_dirty()
        return history_changed or language_changed

    def apply_translation(
        self,
        sequence: int,
        text: str,
        language: str | None,
    ) -> bool:
        """接收翻译线程的结果；目标语言不匹配的结果直接丢弃。"""
        if language != self._target_language:
            return False
        normalized = self._normalize_text(text, language)
        if not normalized:
            return False
        history_changed = self.history.update_translation(sequence, normalized)
        latest_changed = False
        if (
            history_changed
            and sequence >= self._latest_translation_sequence
            and normalized != self._latest_translation
        ):
            self._latest_translation = normalized
            self._latest_translation_sequence = sequence
            latest_changed = True
        if history_changed or latest_changed:
            self._mark_dirty()
        return history_changed or latest_changed

    def snapshot(self) -> DisplaySnapshot:
        """生成当前完整显示快照，不包含任何 UI 对象。"""
        entries = tuple(self.history.entries())
        return DisplaySnapshot(
            entries=entries,
            revision_modes=tuple(self.history.entry_modes()),
            latest_source=self._latest_source_text,
            latest_translation=self._latest_translation,
            source_language=self._source_language,
            detected_language=self._detected_language,
            target_language=self._target_language,
            translation_visible=self._translation_visible(),
        )

    def take_snapshot(self) -> DisplaySnapshot | None:
        """只在状态改变后取出一次快照，供 UI 批量提交。"""
        if not self._dirty:
            return None
        if self._snapshot is None:
            self._snapshot = self.snapshot()
        self._dirty = False
        return self._snapshot
