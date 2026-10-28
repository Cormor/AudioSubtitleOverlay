"""Explicit, bounded local timing evidence; disabled during ordinary use."""
from __future__ import annotations

import atexit
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
import uuid
import wave


class _DisabledTrace:
    enabled = False
    include_text = False
    include_audio = False
    directory = None

    def record(self, *_args, **_kwargs):
        return False

    record_source = record
    record_presented = record
    record_audio = record
    record_frame = record

    def close(self):
        pass


def _producer_identity():
    """Bind explicit diagnostic evidence to this process and executable bytes."""
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256()
    with executable.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return dict(process_id=os.getpid(), executable=str(executable),
                executable_sha256=digest.hexdigest())


class RuntimeTrace:
    """Write one finite diagnostic session without blocking capture or drawing.

    This is user-requested evidence, not an always-on recording or runtime log.
    Limits stop the recording; existing evidence is never overwritten or pruned.
    """
    def __init__(self, directory, *, seconds=120, maximum_bytes=16 * 1024**2,
                 include_audio=False, include_text=False):
        base = Path(directory).expanduser()
        if not base.is_absolute():
            raise ValueError("诊断目录必须是绝对路径")
        for parent in (base, *base.parents):
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ValueError("诊断目录不能经过链接或目录联接")
        base.mkdir(parents=True, exist_ok=True)
        self.maximum_bytes = max(4096, min(int(maximum_bytes), 128 * 1024**2))
        if shutil.disk_usage(base).free < self.maximum_bytes + 1024**2:
            raise OSError("诊断目录可用空间不足")
        self._producer = _producer_identity()
        self.seconds = max(.1, min(float(seconds), 600))
        self.include_audio = bool(include_audio)
        self.include_text = bool(include_text)
        self.directory = base / (datetime.now().strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        self.directory.mkdir()
        self.started_perf = time.perf_counter()
        self.started_epoch = time.time()
        self.enabled = True
        self._queue = queue.Queue(maxsize=512)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._closing = threading.Event()
        self._dropped = 0
        self._bytes = 0
        self._events = 0
        self._finish_reason = "closed"
        self._error = None
        self._last_frame_signature = None
        self._thread = threading.Thread(target=self._write, name="本机诊断记录", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def record(self, kind, **fields):
        if not self.enabled:
            return False
        row = dict(kind=kind, perf=time.perf_counter(), epoch=time.time(), **fields)
        if not self.include_text:
            row = self._redact(row)
        return self._enqueue((row, None))

    @classmethod
    def _redact(cls, value):
        if isinstance(value, dict):
            return {k: cls._redact(v) for k, v in value.items() if k not in {"text", "source_text", "word_spans"}}
        if isinstance(value, (tuple, list)):
            return [cls._redact(v) for v in value]
        return value

    def record_frame(self, presented_at, mode, visible):
        if not self.enabled:
            return False
        signature = json.dumps(visible, ensure_ascii=False, sort_keys=True)
        if signature == self._last_frame_signature:
            return False
        self._last_frame_signature = signature
        return self.record("frame", presented_at=presented_at, reading_mode=mode,
                           visible=visible, meaning="Visible source lines submitted to Windows compositor")

    def _enqueue(self, item):
        with self._lock:
            if not self.enabled:
                return False
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._dropped += 1
                return False
        return True

    def record_source(self, update):
        if not self.enabled:
            return False
        return self.record("source", **asdict(update))

    def record_presented(self, update, presented_at, mode):
        if not self.enabled:
            return False
        return self.record("presented", **asdict(update), presented_at=presented_at,
                           reading_mode=mode,
                           meaning="LayeredWindow.present returned; not display scanout")

    def record_audio(self, frames, received_at, capture_id, timing=None):
        if not self.enabled:
            return False
        row = dict(kind="audio", perf=received_at, epoch=time.time(),
                   capture_id=capture_id, samples=int(frames.size),
                   timing=timing, sample_rate=16000)
        data = None
        if self.include_audio:
            import numpy as np
            data = np.clip(frames * 32767, -32768, 32767).astype("<i2").tobytes()
        return self._enqueue((row, data))

    def _write(self):
        audio = None
        audio_samples = 0
        try:
            with (self.directory / "events.jsonl").open("x", encoding="utf-8") as events:
                if self.include_audio:
                    audio = wave.open(str(self.directory / "audio.wav"), "wb")
                    audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    self._bytes += 44
                last_flush = time.monotonic()
                while True:
                    if self._closing.is_set() and self._queue.empty():
                        break
                    if time.perf_counter() - self.started_perf >= self.seconds:
                        self._finish_reason = "time_limit"
                        break
                    try:
                        item = self._queue.get(timeout=.1)
                    except queue.Empty:
                        if self._closing.is_set():
                            break
                        continue
                    if item is None:
                        break
                    row, data = item
                    if data is not None:
                        row["file_sample_start"] = audio_samples
                        row["file_sample_end"] = audio_samples + len(data) // 2
                    line = json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                    size = len(line.encode("utf-8")) + (len(data) if data else 0)
                    # Leave room for the WAV header and final metadata.
                    if self._bytes + size + 2048 > self.maximum_bytes:
                        self._finish_reason = "size_limit"
                        break
                    if data is not None:
                        audio.writeframesraw(data)
                        audio_samples += len(data) // 2
                    events.write(line)
                    self._bytes += size
                    self._events += 1
                    if time.monotonic() - last_flush >= .25:
                        events.flush()
                        last_flush = time.monotonic()
        except Exception as error:
            self._error = str(error)[:512]
            self._finish_reason = "write_error"
            logging.warning("本机诊断记录已停止：%s", error)
        finally:
            with self._lock:
                self.enabled = False
            if audio is not None:
                try:
                    audio.close()
                except OSError:
                    self._finish_reason = "write_error"
            metadata = dict(**self._producer, started_epoch=self.started_epoch, started_perf=self.started_perf,
                            ended_epoch=time.time(), seconds_limit=self.seconds,
                            maximum_bytes=self.maximum_bytes, data_bytes=self._bytes,
                            events=self._events, dropped_events=self._dropped,
                            pending_events=self._queue.qsize(),
                            audio_included=self.include_audio, text_included=self.include_text,
                            audio_samples=audio_samples, finish_reason=self._finish_reason,
                            error=self._error,
                            clock="Windows perf_counter / WASAPI QPC seconds where available",
                            retention="Explicit local test evidence; preserve until owner removes it")
            try:
                (self.directory / "trace.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                logging.warning("本机诊断结束信息未能保存")
            self._closed.set()

    def close(self):
        self._closing.set()
        with self._lock:
            self.enabled = False
        # Do not block the UI on a slow/full diagnostic queue.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            self._finish_reason = "closed_with_pending_events"
        if self._thread is not threading.current_thread():
            self._closed.wait(1)


_trace = None
_trace_lock = threading.Lock()


def add_trace_arguments(parser):
    """Use the same optional diagnostic switches in the app and trace loader."""
    parser.add_argument("--trace-dir", help="显式指定本机诊断目录；普通启动不记录")
    parser.add_argument("--trace-seconds", type=float, default=None, help="诊断时长，最多600秒")
    parser.add_argument("--trace-audio", action="store_true", help="本次诊断包含音频")
    parser.add_argument("--trace-text", action="store_true", help="本次诊断包含识别与显示文字")


def get_runtime_trace():
    """Enable only with an explicit CLI directory or ASR_TRACE_DIR.

    Audio/text each require their own opt-in. No config is persisted.
    """
    global _trace
    if _trace is not None:
        return _trace
    with _trace_lock:
        if _trace is not None:
            return _trace
        import argparse
        parser = argparse.ArgumentParser(add_help=False)
        add_trace_arguments(parser)
        try:
            args, _ = parser.parse_known_args(sys.argv[1:])
            directory = args.trace_dir or os.environ.get("ASR_TRACE_DIR")
            seconds = args.trace_seconds if args.trace_seconds is not None else float(os.environ.get("ASR_TRACE_SECONDS", 120))
            _trace = (RuntimeTrace(directory, seconds=seconds,
                                   include_audio=args.trace_audio or os.environ.get("ASR_TRACE_AUDIO") == "1",
                                   include_text=args.trace_text or os.environ.get("ASR_TRACE_TEXT") == "1")
                      if directory else _DisabledTrace())
        except (ValueError, OSError, SystemExit) as error:
            logging.warning("本机诊断未启用：%s", error)
            _trace = _DisabledTrace()
        return _trace
