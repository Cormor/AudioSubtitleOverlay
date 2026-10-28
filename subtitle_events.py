"""字幕结果及跨线程时间记录，不依赖音频设备、模型或界面库。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceUpdate:
    """记录本次结果对应的音频终点，避免把旧结果误算成低延迟。"""
    sequence: int
    block_id: int
    text: str
    language: str | None
    audio_end: float
    received_at: float
    inference_started_at: float
    inference_finished_at: float
    last_word_received_at: float | None
    source_id: str = "audio"
    session_id: int = 0
    revision: int = 0
    final: bool = True
    pipeline_id: str = ""
    capture_id: str = ""
    word_spans: tuple[tuple[str, float, float], ...] = ()
    translation_epoch: int = 0

    def measurements(self, presented_at: float) -> dict[str, float | None]:
        """输入回调时间不包含声卡内部缓冲；末词时间来自模型估计。"""
        return {
            '输入终点到上屏毫秒': (presented_at-self.received_at)*1000,
            '排队毫秒': (self.inference_started_at-self.received_at)*1000,
            '推理毫秒': (self.inference_finished_at-self.inference_started_at)*1000,
            '结果到上屏毫秒': (presented_at-self.inference_finished_at)*1000,
            '窗口末词到上屏估计毫秒': None if self.last_word_received_at is None else float((presented_at-self.last_word_received_at)*1000),
        }


def source_text_extends(previous, current, *, previous_final=False):
    """Match a draft ancestor while allowing its temporary trailing punctuation.

    Internal punctuation/characters stay exact. A punctuation-only change is
    not growth, and finalized source text never receives this draft allowance.
    """
    if not previous or not any(char.isalnum() for char in previous):
        return False

    def extends(old, new):
        if not new.startswith(old):
            return False
        if len(new) > len(old) and old[-1].isascii() and old[-1].isalnum():
            next_char = new[len(old)]
            if next_char.isascii() and next_char.isalnum():
                return False
            if (old[-1].isalpha() and next_char in "'’"
                    and len(new) > len(old) + 1
                    and new[len(old) + 1].isascii() and new[len(old) + 1].isalpha()):
                return False
        return True

    if current.startswith(previous):
        return extends(previous, current)
    if previous_final:
        return False

    def body(text):
        text = text.rstrip()
        without_closers = text
        while without_closers and without_closers[-1] in '\"\'”’』」）》】〉)]}':
            without_closers = without_closers[:-1]
        if not without_closers or without_closers[-1] not in '.?!。！？…':
            return text
        while without_closers and without_closers[-1] in '.?!。！？…':
            without_closers = without_closers[:-1]
        return without_closers.rstrip()

    old, new = body(previous), body(current)
    return bool(old) and len(new) > len(old) and extends(old, new)

@dataclass(frozen=True)
class TranslationContext:
    """Translation policy identity, including queued UI callbacks."""
    pipeline_id: str
    session_id: int
    translation_epoch: int
    target_language: str
    enabled: bool
    source_language: str | None = None


@dataclass(frozen=True)
class TranslationUpdate:
    """A completed translation of one exact source snapshot, never re-labelled."""
    sequence: int
    block_id: int
    source_text: str
    source_language: str | None
    target_language: str
    text: str
    source_id: str = "audio"
    session_id: int = 0
    revision: int = 0
    final: bool = True
    pipeline_id: str = ""
    translation_epoch: int = 0
    queued_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
