"""Analyze explicitly recorded live app timing; never upload audio or start playback."""
import argparse
from bisect import bisect_left
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys
import unicodedata
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def normalized(text):
    from text_processing import display_text
    return "".join(c.casefold() for c in unicodedata.normalize("NFKC", display_text(text, "zh")) if c.isalnum())


def quantiles(values):
    values = sorted(values)
    return ({"n": len(values), "p50": statistics.median(values),
             "p95": values[int(.95 * (len(values) - 1))], "max": max(values)} if values else None)


def reference(trace, output, model, device, language):
    import numpy as np
    from model_manager import find_local_model
    from transcription import WhisperRecognizer
    audio_path = trace / "audio.wav"
    with wave.open(str(audio_path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Expected mono 16 kHz PCM16")
        audio = np.frombuffer(wav.readframes(wav.getnframes()), "<i2").astype(np.float32) / 32768
    path = find_local_model(model)
    if path is None:
        raise ValueError("Reference model is not cached; this tool never downloads")
    recognizer = WhisperRecognizer(model, device, str(path))
    words = []
    try:
        recognizer.load()
        segments, _ = recognizer._model.transcribe(audio, language=language, beam_size=5,
            word_timestamps=True, vad_filter=True, condition_on_previous_text=False, temperature=0)
        for segment in segments:
            for word in segment.words or []:
                words.append(dict(text=word.word, start=word.start, end=word.end,
                                  probability=word.probability))
    finally:
        recognizer.close()
    result = dict(audio_sha256=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                  audio_seconds=len(audio) / 16000, words=words,
                  meaning="Independent automatic word-time reference; not human ground truth")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"reference_words": len(words), "output": str(output)}


def analyze(trace, reference_path=None):
    meta = json.loads((trace / "trace.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (trace / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    sources = {(e["pipeline_id"], e["sequence"]): e for e in events if e["kind"] == "source"}
    frames = [e for e in events if e["kind"] == "frame"]
    rendered = []
    callback_to_frame = []
    seen_live_versions = set()
    invalid = 0
    for frame in frames:
        for item in frame["visible"]:
            source = sources.get((item["pipeline_id"], item["sequence"]))
            if source is None or source["block_id"] != item["block_id"]:
                invalid += 1
                continue
            # A paragraph may be larger than the viewport. Only join adjacent
            # ranges which were actually painted in this frame.
            groups, current, last_end = [], "", None
            for fragment in item["fragments"]:
                if last_end is not None and fragment["char_start"] != last_end:
                    groups.append(normalized(current))
                    current = ""
                current += fragment.get("text", "")
                last_end = fragment["char_end"]
            groups.append(normalized(current))
            rendered.append(dict(source=source, time=frame["presented_at"], groups=groups,
                                 mode=frame["reading_mode"]))
            version = item["pipeline_id"], item["sequence"]
            if frame["reading_mode"] == "live" and version not in seen_live_versions:
                seen_live_versions.add(version)
                callback_to_frame.append(frame["presented_at"] - source["perf"])
    result = dict(scope="Actual live app events and the exact source lines submitted to Windows; not monitor scanout",
                  metadata=meta, counts=dict(Counter(e["kind"] for e in events)),
                  invalid_frame_versions=invalid,
                  callback_to_frame_seconds=quantiles(callback_to_frame),
                  evidence_complete=not(meta["error"] or meta["dropped_events"]
                                        or meta.get("pending_events", 0) or invalid), phrases=[])
    if reference_path is None:
        return result
    if not (meta["text_included"] and meta["audio_included"]):
        raise ValueError("Phrase alignment requires explicit text and audio recording")
    ref = json.loads(reference_path.read_text(encoding="utf-8"))
    if ref.get("audio_sha256") != hashlib.sha256((trace / "audio.wav").read_bytes()).hexdigest():
        raise ValueError("Reference does not belong to this recording")
    audio_events = [e for e in events if e["kind"] == "audio" and "file_sample_end" in e]
    ends = [e["file_sample_end"] for e in audio_events]
    by_arrival = {e["perf"]: e for e in audio_events}

    def audio_time(seconds):
        position = seconds * 16000
        index = bisect_left(ends, position)
        if index >= len(audio_events):
            return None
        event = audio_events[index]
        offset = position - event["file_sample_start"]
        timing = event.get("timing") or {}
        if timing.get("clock") != "wasapi_qpc":
            return None
        for packet in timing["packets"]:
            if packet["sample_start"] <= offset <= packet["sample_end"]:
                if packet["engine_start"] is None or packet["flags"] & 5:
                    return None
                return packet["engine_start"] + (offset - packet["sample_start"]) / 16000
        return None

    full_text = "".join(normalized(w["text"]) for w in ref["words"])
    words = [(w, normalized(w["text"])) for w in ref["words"] if normalized(w["text"])]
    anchors = []
    # Try each word boundary: one mismatched word must not discard an otherwise
    # exact phrase merely because it shares an arbitrary eight-character chunk.
    for index, (word, _) in enumerate(words):
        phrase, end = "", word["end"]
        for following, token in words[index:]:
            phrase += token
            end = following["end"]
            if len(phrase) >= 8:
                break
        start = word["start"]
        if len(phrase) < 8:
            continue
        if full_text.count(phrase) == 1 and end-start <= 6 and 3 <= start and end <= ref["audio_seconds"]-5:
            anchors.append((phrase, start, end))
    matched_until = -1
    for phrase, start, end in anchors:
        if start < matched_until:
            continue
        end_clock = audio_time(end)
        if end_clock is None:
            continue
        def covers(source):
            event = by_arrival.get(source["received_at"])
            return event and end-.5 <= event["file_sample_end"]/16000 <= end+20
        hits = [s for s in sources.values() if phrase in normalized(s.get("text", "")) and covers(s)]
        shown = [f for f in rendered if f["mode"] == "live" and covers(f["source"])
                 and any(phrase in group for group in f["groups"])]
        if not hits or not shown:
            continue
        first_source = min(hits, key=lambda s: s["perf"])
        first_frame = min(shown, key=lambda f: f["time"])
        source_lag, frame_lag = first_source["perf"]-end_clock, first_frame["time"]-end_clock
        if source_lag < -.3 or frame_lag < -.3 or frame_lag > 20:
            continue
        result["phrases"].append(dict(phrase=phrase, audio_start_seconds=start, audio_end_seconds=end,
            native_audio_end_perf=end_clock, first_source_perf=first_source["perf"],
            first_visible_frame_perf=first_frame["time"], source_after_end_seconds=source_lag,
            visible_after_end_seconds=frame_lag,
            sequence=first_frame["source"]["sequence"], block_id=first_frame["source"]["block_id"]))
        matched_until = end
    result["anchor_count"] = len(anchors)
    result["source_after_end_seconds"] = quantiles(p["source_after_end_seconds"] for p in result["phrases"])
    result["visible_after_end_seconds"] = quantiles(p["visible_after_end_seconds"] for p in result["phrases"])
    result["reference_scope"] = "Automatic word times remain estimates; exact phrase coverage is not recognition accuracy"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["analyze", "reference"])
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="NVIDIA GPU（float16）")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    if args.action == "reference":
        result = reference(args.trace, args.output, args.model, args.device, args.language)
    else:
        result = analyze(args.trace, args.reference)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.csv and result["phrases"]:
            with args.csv.open("w", encoding="utf-8-sig", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=list(result["phrases"][0]))
                writer.writeheader()
                writer.writerows(result["phrases"])
        result = {k: v for k, v in result.items() if k not in {"phrases", "metadata"}}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
