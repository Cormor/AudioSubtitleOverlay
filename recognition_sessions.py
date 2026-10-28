"""VAD segmentation for text-only ASR; never invent word timestamps."""
from __future__ import annotations

import numpy as np


class SegmentedTextSession:
    """Preview speech at 500 ms intervals and commit at Silero endpoints.

    Uses the VAD already shipped by faster-whisper. A 300 ms onset prefix
    protects initial phonemes. This is segmented ASR, not a streaming model.
    """
    def __init__(self, recognizer, language=None, *, silence_ms=400):
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        self._vad = get_speech_timestamps
        self._vad_options = VadOptions(min_speech_duration_ms=100,
                                       min_silence_duration_ms=silence_ms,
                                       speech_pad_ms=0, max_speech_duration_s=12)
        self.recognizer, self.language = recognizer, language
        self.silence_samples = int(silence_ms * 16)
        self.buffer = np.empty(0, dtype=np.float32)
        self.base = self.total = self.decoded = 0
        self.segment_id = self.revision = 0
        self._previous = ""

    def reset(self):
        self.buffer = np.empty(0, dtype=np.float32)
        self.base = self.decoded = self.total
        self.segment_id += 1
        self.revision = 0
        self._previous = ""

    def accept_audio(self, frames, final=False):
        from asr_backends import TextHypothesis
        frames = np.asarray(frames, dtype=np.float32)
        self.buffer = np.concatenate((self.buffer, frames))
        self.total += frames.size
        if not final and self.total - self.decoded < 8000:
            return []
        self.decoded = self.total
        spans = self._vad(self.buffer, self._vad_options)
        updates, trim = [], 0
        if not spans:
            trim = max(0, self.buffer.size - 4800)
        for index, span in enumerate(spans):
            start, end = span["start"], span["end"]
            ended = (final or index < len(spans) - 1 or self.buffer.size - end >= self.silence_samples
                     or end - start >= 12 * 16000)
            audio = self.buffer[max(trim, start - 4800):min(self.buffer.size, end + 1600)]
            if not audio.size:
                continue
            result = self.recognizer.transcribe(audio, self.language)
            text = result.text.strip()
            if text and (text != self._previous or ended):
                self.revision += 1
                updates.append(TextHypothesis(self.segment_id, self.revision, text,
                                              self.language or result.language, ended,
                                              (self.base + start) / 16000,
                                              (self.base + end) / 16000))
                self._previous = text
            if ended:
                self.segment_id += 1
                self.revision = 0
                self._previous = ""
                trim = end
        if trim:
            self.buffer = self.buffer[trim:]
            self.base += trim
        if self.buffer.size > 13 * 16000:
            cut = self.buffer.size - 12 * 16000
            self.buffer = self.buffer[cut:]
            self.base += cut
            self.segment_id += 1
            self.revision = 0
            self._previous = ""
        return updates
