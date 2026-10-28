"""独立的字幕显示后处理模块，不依赖音频采集、识别模型或 Tk。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, TYPE_CHECKING

from text_processing import display_text
from subtitle_events import source_text_extends

if TYPE_CHECKING:
    from subtitle_events import SourceUpdate, TranslationContext, TranslationUpdate


@dataclass(frozen=True)
class SourceVersion:
    """译文所依据的完整源文快照；序号始终保留请求时的值。"""

    sequence: int
    block_id: int
    text: str
    language: str | None
    pipeline_id: str = ""
    session_id: int = 0
    source_id: str = "audio"
    revision: int = 0
    final: bool = True

    @property
    def scope(self):
        return self.pipeline_id, self.session_id, self.source_id, self.block_id


@dataclass
class SubtitleBlock:
    sequence: int
    source: str
    translation: str = ""
    language: str | None = None
    translation_sequence: int = -1
    final: bool = True
    version: SourceVersion | None = None
    lineage_start_sequence: int = 0
    translation_version: SourceVersion | None = None

    @property
    def translation_state(self):
        if not self.translation or self.translation_version is None:
            return "waiting"
        translated = self.translation_version
        if translated.text != self.source or (self.final and not translated.final):
            return "updating"
        return "final" if self.final and translated.final else "draft"


class SubtitleHistory:
    """保留最近字幕，当前段草稿和定稿沿用同一身份。"""

    max_blocks = 64

    def __init__(self):
        self.blocks: dict[int, SubtitleBlock] = {}
        self._sequence_blocks: dict[int, int] = {}
        self._preview_block_id: int | None = None
        self._preview_sequence = -1
        self._preview_source = ""
        self._pending_translations: dict[int, tuple[str, SourceVersion | None]] = {}
        self._latest_seen_sequence = -1

    def clear(self):
        self.blocks.clear()
        self._sequence_blocks.clear()
        self.clear_preview()
        self._pending_translations.clear()
        self._latest_seen_sequence = -1

    def clear_preview(self):
        self._preview_block_id = None
        self._preview_sequence = -1
        self._preview_source = ""

    def clear_translations(self):
        for block in self.blocks.values():
            block.translation = ""
            block.translation_sequence = -1
            block.translation_version = None
        self._pending_translations.clear()

    def update_source(self, block_id, sequence, text, language, *, version=None, final=True):
        version = version or SourceVersion(sequence, block_id, text, language, final=final)
        previous = self.blocks.get(block_id)
        if previous and sequence < previous.sequence:
            return False
        if previous and sequence == previous.sequence and (
            previous.source != text or previous.language != language
            or previous.version.scope != version.scope
            or (previous.final and not final)
        ):
            return False
        append = bool(previous and previous.version.scope == version.scope
                      and previous.language == language
                      and source_text_extends(previous.source, text, previous_final=previous.final))
        cutoff = previous.lineage_start_sequence if append else sequence
        if previous:
            self._sequence_blocks.pop(previous.sequence, None)
        if not append:
            # 改写后即便又回到旧前缀，旧请求仍低于本次修订的起点。
            self._pending_translations = {
                seq: item for seq, item in self._pending_translations.items()
                if item[1] is None or item[1].block_id != block_id or seq >= cutoff
            }
        block = SubtitleBlock(
            sequence, text,
            previous.translation if append else "", language,
            previous.translation_sequence if append else -1,
            final, version, cutoff,
            previous.translation_version if append else None,
        )
        self.blocks[block_id] = block
        self._sequence_blocks[sequence] = block_id
        self._latest_seen_sequence = max(self._latest_seen_sequence, sequence)
        for pending_sequence, pending in sorted(list(self._pending_translations.items())):
            translated, translated_version = pending
            if pending_sequence == sequence or (
                translated_version is not None and translated_version.block_id == block_id
                and pending_sequence <= sequence
            ):
                self._pending_translations.pop(pending_sequence, None)
                self.update_translation(pending_sequence, translated, version=translated_version or version)
        self._pending_translations = {
            seq: item for seq, item in self._pending_translations.items() if seq > self._latest_seen_sequence
        }
        while len(self.blocks) > self.max_blocks:
            removed_id = next(iter(self.blocks))
            removed = self.blocks.pop(removed_id)
            self._sequence_blocks.pop(removed.sequence, None)
            if self._preview_block_id == removed_id:
                self.clear_preview()
        if final:
            if self._preview_block_id is not None and block_id >= self._preview_block_id:
                self.clear_preview()
        else:
            self._preview_block_id = block_id
            self._preview_sequence = sequence
            self._preview_source = text
        return True

    def update_preview(self, block_id, sequence, text, language, *, version=None):
        candidate = text.strip(" \t")
        if not candidate:
            return False
        return self.update_source(block_id, sequence, candidate, language, version=version, final=False)

    def update_translation(self, sequence, text, *, version=None):
        if version is None:
            block_id = self._sequence_blocks.get(sequence)
            block = self.blocks.get(block_id)
            version = block.version if block is not None else None
        else:
            block = self.blocks.get(version.block_id)
        if block is None or sequence > block.sequence:
            if sequence > self._latest_seen_sequence:
                self._pending_translations[sequence] = (text, version)
                while len(self._pending_translations) > self.max_blocks:
                    self._pending_translations.pop(min(self._pending_translations))
            return False
        current = block.version
        if (version is None or current is None or version.scope != current.scope
            or version.language != block.language
            or not block.lineage_start_sequence <= sequence <= block.sequence
            or version.revision > current.revision
            or not version.text or not source_text_extends(version.text, block.source, previous_final=version.final)
            or (sequence == block.sequence and (
                version.text != block.source or version.revision != current.revision
                or version.final != block.final))
            or sequence < block.translation_sequence):
            return False
        if block.translation == text and block.translation_version == version:
            return False
        block.translation = text
        block.translation_sequence = sequence
        block.translation_version = version
        return True

    def entries(self):
        return [(block.source, block.translation) for block in self.blocks.values()]

    def entry_modes(self):
        return ["stable" if block.final else "preview" for block in self.blocks.values()]

    def entry_translation_states(self):
        return [block.translation_state for block in self.blocks.values()]

    def entry_translation_pending(self):
        return [block.translation_state == "updating" for block in self.blocks.values()]

    def latest_source(self) -> str:
        return next(reversed(self.blocks.values())).source if self.blocks else ""

    def latest_translation(self) -> str:
        return next(reversed(self.blocks.values())).translation if self.blocks else ""

    def latest_translation_state(self) -> str:
        return next(reversed(self.blocks.values())).translation_state if self.blocks else "waiting"

    def translation_for_sequence(self, sequence: int) -> str:
        block_id = self._sequence_blocks.get(sequence)
        block = self.blocks.get(block_id)
        return block.translation if block and block.translation_sequence == sequence else ""


@dataclass(frozen=True)
class DisplayEvent:
    """保留旧验证入口；正式流水线使用携带完整身份的事件。"""

    kind: Literal["source", "preview", "translation"]
    sequence: int
    text: str
    language: str | None
    block_id: int | None = None


@dataclass(frozen=True)
class DisplaySnapshot:
    entries: tuple[tuple[str, str], ...]
    revision_modes: tuple[str, ...]
    latest_source: str
    latest_translation: str
    source_language: str | None
    detected_language: str | None
    target_language: str
    translation_visible: bool
    translation_pending: tuple[bool, ...] = ()
    translation_activity: int = 0
    translation_states: tuple[str, ...] = ()
    latest_translation_state: str = "waiting"


class DisplayModule:
    """串行接收源文/译文事件，输出有界、完整的显示快照。"""

    def __init__(self, source_language: str | None, target_language: str, translation_enabled: bool = True) -> None:
        self.history = SubtitleHistory()
        self._translation_enabled = translation_enabled
        self._translation_activity = 0
        self._source_language = source_language
        self._target_language = target_language
        self._detected_language: str | None = None
        self._latest_source_sequence = 0
        self._latest_source_text = ""
        self._latest_translation = ""
        self._latest_translation_sequence = -1
        self._context = None
        self._awaiting_context = False
        self._dirty = True
        self._snapshot: DisplaySnapshot | None = None

    @property
    def latest_source_text(self) -> str:
        return self._latest_source_text

    @property
    def latest_translation_text(self) -> str:
        return self._latest_translation

    @property
    def translation_visible(self) -> bool:
        return self._translation_visible()

    @property
    def translation_epoch(self) -> int:
        return self._context.translation_epoch if self._context is not None else 0

    @staticmethod
    def _normalize_text(text: str, language: str | None) -> str:
        normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
        return display_text(normalized, language).strip(" \t")

    def _translation_visible(self) -> bool:
        return self._translation_enabled and (self._source_language is None or self._source_language != self._target_language)

    @property
    def awaiting_translation_context(self) -> bool:
        return self._awaiting_context

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._snapshot = None

    def _sync_latest(self):
        self._latest_source_text = self.history.latest_source()
        self._latest_translation = self.history.latest_translation()
        latest = next(reversed(self.history.blocks.values()), None)
        self._latest_translation_sequence = latest.translation_sequence if latest else -1

    def invalidate_translation_context(self) -> None:
        """界面已提交新配置时立即屏蔽旧配置排队结果。"""
        self._awaiting_context = self._context is not None
        self.history.clear_translations()
        self._sync_latest()
        self._mark_dirty()

    def set_translation_enabled(self, enabled: bool) -> bool:
        if self._translation_enabled == enabled:
            return False
        self._translation_enabled = enabled
        self.invalidate_translation_context()
        return True

    def reset(self, source_language: str | None, target_language: str) -> None:
        self.history.clear()
        self._source_language = source_language
        self._target_language = target_language
        self._detected_language = None
        self._latest_source_sequence = 0
        self._latest_source_text = ""
        self._latest_translation = ""
        self._latest_translation_sequence = -1
        self._context = None
        self._awaiting_context = False
        self._translation_activity = 0
        self._mark_dirty()

    def set_languages(self, source_language: str | None, target_language: str) -> bool:
        if self._source_language == source_language and self._target_language == target_language:
            return False
        self._source_language = source_language
        self._target_language = target_language
        self._detected_language = None
        self.invalidate_translation_context()
        return True

    def apply_translation_context(self, context: TranslationContext) -> bool:
        if (context.source_language != self._source_language
            or context.target_language != self._target_language
            or context.enabled != self._translation_enabled):
            return False
        previous = self._context
        if previous is not None:
            if context.pipeline_id != previous.pipeline_id or context.session_id < previous.session_id:
                return False
            if context.session_id == previous.session_id and context.translation_epoch < previous.translation_epoch:
                return False
            if context == previous:
                return False
            if context.session_id != previous.session_id:
                self.history.clear()
                self._latest_source_sequence = 0
                self._detected_language = None
        self._context = context
        self._awaiting_context = False
        self.history.clear_translations()
        self._sync_latest()
        self._mark_dirty()
        return True

    def _matches_context(self, update, *, translation=False):
        context = self._context
        return bool(context is not None and (not translation or not self._awaiting_context)
                    and update.pipeline_id == context.pipeline_id
                    and update.session_id == context.session_id
                    and update.translation_epoch == context.translation_epoch)

    def apply_update(self, update: SourceUpdate) -> bool:
        if not self._matches_context(update):
            return False
        text = self._normalize_text(update.text, update.language)
        version = SourceVersion(update.sequence, update.block_id, text, update.language,
                                update.pipeline_id, update.session_id, update.source_id,
                                update.revision, update.final)
        return self._apply_source_version(version)

    def apply_translation_update(self, update: TranslationUpdate) -> bool:
        if (not self._matches_context(update, translation=True) or not self._translation_visible()
            or update.target_language != self._target_language):
            return False
        version = SourceVersion(update.sequence, update.block_id,
                                self._normalize_text(update.source_text, update.source_language),
                                update.source_language, update.pipeline_id, update.session_id,
                                update.source_id, update.revision, update.final)
        return self._apply_translation_version(update.sequence, update.text, update.target_language, version)

    def apply(self, event: DisplayEvent) -> bool:
        if event.kind == "source":
            return self.apply_source(event.sequence, event.text, event.language, event.block_id)
        if event.kind == "preview":
            return event.block_id is not None and self.apply_preview(event.sequence, event.text, event.language, event.block_id)
        if event.kind == "translation":
            return self.apply_translation(event.sequence, event.text, event.language)
        raise ValueError(f"未知显示事件类型：{event.kind}")

    def apply_many(self, events: Iterable[DisplayEvent]) -> bool:
        changed = False
        for event in events:
            changed = self.apply(event) or changed
        return changed

    def _apply_source_version(self, version):
        if version.sequence < self._latest_source_sequence or not version.text:
            return False
        changed = self.history.update_source(version.block_id, version.sequence, version.text,
                                             version.language, version=version, final=version.final)
        if not changed:
            return False
        self._latest_source_sequence = version.sequence
        self._detected_language = version.language
        self._sync_latest()
        self._mark_dirty()
        return True

    def apply_source(self, sequence, text, language, block_id=None) -> bool:
        if self._context is not None:
            return False
        return self._apply_source_version(SourceVersion(sequence, block_id if block_id is not None else sequence,
                                                        self._normalize_text(text, language), language))

    def apply_preview(self, sequence, text, language, block_id) -> bool:
        if self._context is not None:
            return False
        return self._apply_source_version(SourceVersion(sequence, block_id, self._normalize_text(text, language),
                                                        language, final=False))

    def _apply_translation_version(self, sequence, text, language, version=None):
        if not self._translation_visible() or language != self._target_language:
            return False
        normalized = self._normalize_text(text, language)
        if not normalized:
            return False
        pending_before = self.history._pending_translations.get(sequence)
        changed = self.history.update_translation(sequence, normalized, version=version)
        pending_after = self.history._pending_translations.get(sequence)
        new_pending = pending_after is not None and pending_after != pending_before
        if changed or new_pending:
            self._translation_activity += 1
            self._sync_latest()
            self._mark_dirty()
        return changed

    def apply_translation(self, sequence, text, language) -> bool:
        if self._context is not None:
            return False
        return self._apply_translation_version(sequence, text, language)

    def snapshot(self) -> DisplaySnapshot:
        return DisplaySnapshot(
            entries=tuple(self.history.entries()),
            revision_modes=tuple(self.history.entry_modes()),
            translation_pending=tuple(self.history.entry_translation_pending()),
            translation_states=tuple(self.history.entry_translation_states()),
            latest_translation_state=self.history.latest_translation_state(),
            latest_source=self._latest_source_text,
            latest_translation=self._latest_translation,
            source_language=self._source_language,
            detected_language=self._detected_language,
            target_language=self._target_language,
            translation_visible=self._translation_visible(),
            translation_activity=self._translation_activity,
        )

    def take_snapshot(self) -> DisplaySnapshot | None:
        if not self._dirty:
            return None
        if self._snapshot is None:
            self._snapshot = self.snapshot()
        self._dirty = False
        return self._snapshot
