"""CPU recognizers with explicit text and streaming capabilities.

The caller supplies new mono, normalized 16 kHz samples. These classes serialize
load/decode/close so native streams cannot outlive their owning recognizer.
No recognition text is written to logs.
"""

from dataclasses import dataclass, field
from pathlib import Path
import re
from threading import RLock

import numpy as np


@dataclass(frozen=True)
class BackendCapabilities:
    streaming: bool
    word_timestamps: bool
    language_detection: bool
    languages: tuple[str, ...]


@dataclass(frozen=True)
class TextHypothesis:
    segment_id: int
    revision: int
    text: str
    language: str | None
    final: bool
    audio_start: float
    audio_end: float


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None
    words: list = field(default_factory=list)


_META_TAG = re.compile(r"<\|([^|]*)\|>")
_SAMPLE_RATE = 16000


def _language(value, supported):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("language must be auto or a supported language code")
    value = value.strip().lower()
    if value in ("", "auto"):
        return None
    if value not in supported:
        raise ValueError(f"Unsupported language {value!r}; supported: auto, {', '.join(supported)}")
    return value


def _threads(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise ValueError("cpu_threads must be an integer from 1 to 8")
    return value


def _audio(frames):
    samples = np.asarray(frames, dtype=np.float32)
    if samples.ndim != 1:
        raise ValueError("Expected mono 16 kHz audio as a one-dimensional array")
    if not np.isfinite(samples).all():
        raise ValueError("Audio samples must be finite")
    return np.ascontiguousarray(samples)


def _model_files(directory, names):
    root = Path(directory)
    result = {}
    for name in names:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing ASR model file: {path}")
        result[name] = str(path)
    return result


class SenseVoiceRecognizer:
    capabilities = BackendCapabilities(False, False, True, ("zh", "en", "ja", "ko", "yue"))
    actual_device = "cpu"
    actual_compute_type = "int8"

    def __init__(self, model_path: str, language: str | None = None, cpu_threads: int = 2):
        self.model_path = str(model_path)
        self.language = _language(language, self.capabilities.languages)
        self.cpu_threads = _threads(cpu_threads)
        self._lock = RLock()
        self._recognizer = None
        self._loaded_language = None

    def _load_language(self, language):
        # A different source language changes the native model configuration.
        # Build the replacement before dropping the previous working instance.
        import sherpa_onnx

        files = _model_files(self.model_path, ("model.int8.onnx", "tokens.txt"))
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=files["model.int8.onnx"], tokens=files["tokens.txt"],
            num_threads=self.cpu_threads, sample_rate=_SAMPLE_RATE,
            feature_dim=80, decoding_method="greedy_search", provider="cpu",
            language=language or "auto", use_itn=True, debug=False,
        )
        self._recognizer = recognizer
        self._loaded_language = language

    def load(self):
        with self._lock:
            if self._recognizer is None:
                self._load_language(self.language)
        return self

    def transcribe(self, audio, source_language=None, **ignored):
        language = self.language if source_language is None else _language(
            source_language, self.capabilities.languages
        )
        samples = _audio(audio)
        with self._lock:
            if self._recognizer is None:
                raise RuntimeError("SenseVoice recognizer is not loaded")
            if not len(samples):
                return TranscriptionResult("", language)
            if language != self._loaded_language:
                self._load_language(language)
            stream = self._recognizer.create_stream()
            stream.accept_waveform(_SAMPLE_RATE, samples)
            self._recognizer.decode_stream(stream)
            result = stream.result
            raw_text = result.text
            detected = getattr(result, "lang", None) or getattr(result, "language", None)
            if isinstance(detected, str):
                language_tag = _META_TAG.fullmatch(detected)
                if language_tag:
                    detected = language_tag.group(1)
            if detected not in self.capabilities.languages:
                detected = next((tag for tag in _META_TAG.findall(raw_text)
                                 if tag in self.capabilities.languages), None)
            text = _META_TAG.sub("", raw_text).strip()
            return TranscriptionResult(text, detected or language)

    def close(self):
        with self._lock:
            self._recognizer = None
            self._loaded_language = None


class ZipformerRecognizer:
    capabilities = BackendCapabilities(True, False, False, ("zh", "en"))
    actual_device = "cpu"
    actual_compute_type = "int8"
    # Same tail padding as sherpa-onnx's official online-decode-files.py.
    _TAIL_SAMPLES = int(0.66 * _SAMPLE_RATE)
    _CHUNK_SAMPLES = int(0.1 * _SAMPLE_RATE)

    def __init__(self, model_path: str, language: str | None = None, cpu_threads: int = 2):
        self.model_path = str(model_path)
        # This bilingual model has no language-forcing API. The requested
        # supported code is a caller preference, never a detected language.
        self.language = _language(language, self.capabilities.languages)
        self.cpu_threads = _threads(cpu_threads)
        self._lock = RLock()
        self._recognizer = None
        self._stream = None
        self._reset_timeline()

    def _reset_timeline(self):
        self._total_samples = 0
        self._segment_start = 0
        self._segment_id = 0
        self._revision = 0
        self._last_emitted = None

    def load(self):
        with self._lock:
            if self._recognizer is not None:
                return self
            import sherpa_onnx

            names = ("tokens.txt", "encoder-epoch-99-avg-1.int8.onnx",
                     "decoder-epoch-99-avg-1.int8.onnx", "joiner-epoch-99-avg-1.int8.onnx")
            common = dict(
                num_threads=self.cpu_threads, sample_rate=_SAMPLE_RATE,
                feature_dim=80, decoding_method="greedy_search", provider="cpu",
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=1.2,
                rule2_min_trailing_silence=0.6,
                rule3_min_utterance_length=20.0, debug=False,
            )
            if (Path(self.model_path) / names[1]).is_file():
                files = _model_files(self.model_path, names)
                recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                    tokens=files[names[0]], encoder=files[names[1]],
                    decoder=files[names[2]], joiner=files[names[3]], **common)
            else:
                names = ("data/tokens.txt", "ctc-chunk-16-left-64.int8.onnx")
                files = _model_files(self.model_path, names)
                recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
                    tokens=files[names[0]], model=files[names[1]], **common)
            stream = recognizer.create_stream()
            self._recognizer, self._stream = recognizer, stream
            self._reset_timeline()
        return self

    def _decode_ready(self):
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)

    def _hypothesis(self, final):
        raw = self._recognizer.get_result(self._stream)
        text = (raw if isinstance(raw, str) else raw.text).strip()
        if not text:
            return None
        # Timestamp progress alone does not create another text revision.
        state = (text, final)
        if state == self._last_emitted:
            return None
        self._revision += 1
        self._last_emitted = state
        return TextHypothesis(
            segment_id=self._segment_id, revision=self._revision, text=text,
            language=self.language, final=final,
            audio_start=self._segment_start / _SAMPLE_RATE,
            audio_end=self._total_samples / _SAMPLE_RATE,
        )

    def _next_segment(self):
        self._segment_id += 1
        self._segment_start = self._total_samples
        self._revision = 0
        self._last_emitted = None

    def accept_audio(self, frames, final=False) -> list[TextHypothesis]:
        """Consume only new samples; final flushes and starts a fresh stream.

        Endpoint reset keeps the native stream so its buffered features survive.
        Explicit final calls input_finished and replaces that finished stream.
        Audio times describe submitted real samples, excluding flush padding.
        """
        samples = _audio(frames)
        events = []
        with self._lock:
            if self._recognizer is None:
                raise RuntimeError("Zipformer recognizer is not loaded")
            for start in range(0, len(samples), self._CHUNK_SAMPLES):
                chunk = samples[start:start + self._CHUNK_SAMPLES]
                self._stream.accept_waveform(_SAMPLE_RATE, chunk)
                self._total_samples += len(chunk)
                self._decode_ready()
                endpoint = not final and self._recognizer.is_endpoint(self._stream)
                event = self._hypothesis(endpoint)
                if event is not None:
                    events.append(event)
                if endpoint:
                    self._recognizer.reset(self._stream)
                    self._next_segment()
            if final and self._total_samples > self._segment_start:
                self._stream.accept_waveform(
                    _SAMPLE_RATE, np.zeros(self._TAIL_SAMPLES, dtype=np.float32)
                )
                self._stream.input_finished()
                self._decode_ready()
                event = self._hypothesis(True)
                if event is not None:
                    events.append(event)
                self._stream = self._recognizer.create_stream()
                self._next_segment()
        return events

    def close(self):
        with self._lock:
            # Drop the stream before its native recognizer owner.
            self._stream = None
            self._recognizer = None
            self._reset_timeline()

    def reset(self):
        """Discard an interrupted segment and cached audio, keeping the model."""
        with self._lock:
            if self._recognizer is None:
                raise RuntimeError("Zipformer recognizer is not loaded")
            self._stream = self._recognizer.create_stream()
            self._next_segment()
