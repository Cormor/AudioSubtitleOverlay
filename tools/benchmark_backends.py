"""Comparable local ASR benchmark. Explicit output paths; no audio uploads."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asr_backends import SenseVoiceRecognizer, ZipformerRecognizer
from model_manager import find_local_model, validate_model_language
from recognition_sessions import SegmentedTextSession
from rolling_transcript import RollingTranscript
from transcription import WhisperRecognizer


def peak_ram():
    if os.name != "nt":
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    class Memory(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                    ("peak", ctypes.c_size_t), ("working", ctypes.c_size_t),
                    ("pool_peak", ctypes.c_size_t), ("pool", ctypes.c_size_t),
                    ("nonpaged_peak", ctypes.c_size_t), ("nonpaged", ctypes.c_size_t),
                    ("pagefile", ctypes.c_size_t), ("pagefile_peak", ctypes.c_size_t)]
    info = Memory()
    info.cb = ctypes.sizeof(info)
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory), ctypes.c_ulong]
    if not get_memory(kernel.GetCurrentProcess(), ctypes.byref(info), info.cb):
        raise ctypes.WinError()
    return info.peak


def load_audio(path):
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Input must be 16 kHz mono PCM16")
        return np.frombuffer(wav.readframes(wav.getnframes()), "<i2").astype(np.float32) / 32768


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", default="CPU（int8）")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--threads", type=int, choices=range(1, 9), default=2)
    ap.add_argument("--audio", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--replay", action="store_true", help="Pace fresh audio in real time, without capture or UI")
    args = ap.parse_args()
    language = None if args.language == "auto" else args.language
    validate_model_language(args.model, language)
    audio = load_audio(args.audio)
    path = find_local_model(args.model)
    if path is None:
        raise RuntimeError("Prepare the model before benchmarking; this command never downloads")
    if args.model == "sensevoice-int8":
        model = SenseVoiceRecognizer(str(path), language, args.threads)
    elif args.model.startswith("zipformer-"):
        model = ZipformerRecognizer(str(path), language, args.threads)
    else:
        model = WhisperRecognizer(args.model, args.device, str(path))
    before = time.perf_counter()
    model.load()
    report = dict(model=args.model, requested_device=args.device, actual_device=model.actual_device,
                  compute_type=model.actual_compute_type, cpu_threads=args.threads,
                  audio_sha256=hashlib.sha256(args.audio.read_bytes()).hexdigest(),
                  audio_seconds=audio.size / 16000, load_seconds=time.perf_counter() - before,
                  scope="local model and transcript processing only; no audio capture, UI or display scanout")
    wall, cpu, texts = [], [], []
    try:
        for _ in range(3):
            if args.model.startswith("zipformer-"):
                model.reset()
            began, cpu_start = time.perf_counter(), time.process_time()
            if args.model.startswith("zipformer-"):
                updates = model.accept_audio(audio, final=True)
                blocks = {}
                for update in updates:
                    blocks[update.segment_id] = update.text
                text = "".join(blocks.values())
            else:
                text = model.transcribe(audio, language).text
            wall.append(time.perf_counter() - began)
            cpu.append(time.process_time() - cpu_start)
            texts.append(text)
        report.update(decode_seconds=wall, process_cpu_seconds=cpu, texts=texts,
                      median_rtf=statistics.median(wall) / report["audio_seconds"],
                      text_repeat_consistent=len(set(texts)) == 1)
        if args.replay:
            if args.model.startswith("zipformer-"):
                model.reset()
                session = model
            elif args.model == "sensevoice-int8":
                session = SegmentedTextSession(model, language)
                # VAD model initialization is measured separately from steady replay.
                t = time.perf_counter()
                session._vad(np.zeros(16000, np.float32), session._vad_options)
                report["vad_warmup_seconds"] = time.perf_counter() - t
            else:
                session = None
            padded = np.concatenate((audio, np.zeros(16000, np.float32)))
            transcript = RollingTranscript()
            samples, blocks = [], {}
            started, cpu_started = time.perf_counter(), time.process_time()
            step = 1600 if session is not None else 8000
            for start in range(0, padded.size, step):
                end = min(start + step, padded.size)
                time.sleep(max(0, started + end / 16000 - time.perf_counter()))
                began = time.perf_counter()
                if session is not None:
                    events = session.accept_audio(padded[start:end], final=end == padded.size)
                    updates = [(e.segment_id, e.text, e.final) for e in events]
                else:
                    window_start = max(0, end - 128000)
                    result = model.transcribe(padded[window_start:end], language)
                    updates = [(key, text, True) for key, text in transcript.update(
                        result.words, window_start / 16000, end / 16000)]
                finished = time.perf_counter()
                for key, text, final in updates:
                    if text:
                        blocks[key] = text
                        samples.append(dict(audio_end_seconds=end / 16000, wall_seconds=finished - started,
                                            processing_seconds=finished - began, segment_id=key,
                                            final=final, text=text))
            elapsed = time.perf_counter() - started
            report["replay"] = dict(elapsed_seconds=elapsed, process_cpu_seconds=time.process_time() - cpu_started,
                                    first_text_wall_seconds=samples[0]["wall_seconds"] if samples else None,
                                    final_text="".join(blocks.values()), updates=samples,
                                    meaning="first text is measured from WAV start, not speech onset; no renderer")
        report["peak_process_ram_bytes"] = peak_ram()
    finally:
        model.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["model", "median_rtf", "load_seconds", "peak_process_ram_bytes"]}))


if __name__ == "__main__":
    main()
