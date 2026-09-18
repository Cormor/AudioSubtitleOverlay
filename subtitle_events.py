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

    def measurements(self, presented_at: float) -> dict[str, float | None]:
        """输入回调时间不包含声卡内部缓冲；末词时间来自模型估计。"""
        return {
            '输入终点到上屏毫秒': (presented_at-self.received_at)*1000,
            '排队毫秒': (self.inference_started_at-self.received_at)*1000,
            '推理毫秒': (self.inference_finished_at-self.inference_started_at)*1000,
            '结果到上屏毫秒': (presented_at-self.inference_finished_at)*1000,
            '窗口末词到上屏估计毫秒': None if self.last_word_received_at is None else float((presented_at-self.last_word_received_at)*1000),
        }
