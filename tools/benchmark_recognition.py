"""Replay a fixed PCM WAV through the decoder and merger, without playback/capture."""

import argparse
import hashlib
import unicodedata
import json
from pathlib import Path
import statistics
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import recognition_window_start
from rolling_transcript import RollingTranscript
from transcription import WhisperRecognizer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="NVIDIA GPU（float16）")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--cases", nargs="+", help="Run selected cases only")
    args = parser.parse_args()
    with wave.open(str(args.wav), "rb") as stream:
        if (stream.getframerate(),stream.getnchannels(),stream.getsampwidth()) != (16000,1,2):
            raise ValueError("Expected 16kHz mono PCM16 WAV")
        audio = np.frombuffer(stream.readframes(stream.getnframes()),dtype="<i2").astype(np.float32)/32768
    audio = np.concatenate((audio,np.zeros(16000,dtype=np.float32)))
    recognizer = WhisperRecognizer(args.model,args.device)
    recognizer.load()
    cases = {
        "fixed_8s_beam1": dict(adaptive=False,beam=1,temperature="0"),
        "fixed_8s_beam5": dict(adaptive=False,beam=5,temperature="0,0.2,0.4"),
        "silence_window_beam5": dict(adaptive=True,beam=5,temperature="0,0.2,0.4"),
        "silence_window_beam1": dict(adaptive=True,beam=1,temperature="0"),
    }
    if args.cases:
        cases = {name:cases[name] for name in args.cases}
    state = {name:dict(transcript=RollingTranscript(),start=0,samples=[],blocks={}) for name in cases}
    ends = list(range(8000,len(audio),8000)) + [len(audio)]
    for step,end in enumerate(ends):
        names = list(cases)
        names = names[step%len(names):] + names[:step%len(names)]
        for name in names:
            case, current = cases[name],state[name]
            transcript = current["transcript"]
            start = (recognition_window_start(end,current["start"],transcript.safe_decode_start)
                     if case["adaptive"] else max(0,end-128000))
            current["start"] = start
            began = time.perf_counter()
            result = recognizer.transcribe(audio[start:end],args.language,
                                          beam_size=case["beam"],temperature_schedule=case["temperature"])
            elapsed = (time.perf_counter()-began)*1000
            updates = transcript.update(result.words,start/16000,end/16000)
            current["blocks"].update(updates)
            current["samples"].append(dict(audio_end=end/16000,window_seconds=(end-start)/16000,
                                            inference_ms=elapsed,updates=len(updates)))
        if step%10==0:
            print(f"replay {end/16000:.1f}/{len(audio)/16000:.1f}s",flush=True)
    report = {"input_sha256":hashlib.sha256(args.wav.read_bytes()).hexdigest(),"input_duration_seconds":len(audio)/16000, "model":args.model,"device":recognizer.actual_device,"scope":"decoder and rolling merger only; no UI or acoustic last-word latency", "cases":{}}
    for name,current in state.items():
        samples=current["samples"]
        values=sorted(x["inference_ms"] for x in samples)
        report["cases"][name] = dict(median_ms=statistics.median(values),
                                      p95_ms=values[min(len(values)-1,int(.95*len(values)))],
                                      average_window_seconds=statistics.mean(x["window_seconds"] for x in samples),
                                      text="".join(current["blocks"].values()),samples=samples)
    output = ROOT / "verification_latency"
    output.mkdir(exist_ok=True)
    if args.reference:
        from opencc import OpenCC
        converter=OpenCC("t2s")
        def normalized(text):
            return "".join(c.lower() for c in converter.convert(text) if unicodedata.category(c)[0] in {"L","N"})
        expected=normalized(args.reference.read_text(encoding="utf-8"))
        for result in report["cases"].values():
            observed=normalized(result["text"])
            previous=list(range(len(observed)+1))
            for i,a in enumerate(expected,1):
                current=[i]
                for j,b in enumerate(observed,1):
                    current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(a!=b)))
                previous=current
            result["character_errors"]=previous[-1]
            result["reference_characters"]=len(expected)
    destination=args.output or output/"decoder-comparison.json"
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({name:{k:v for k,v in result.items() if k!="samples"} for name,result in report["cases"].items()},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
