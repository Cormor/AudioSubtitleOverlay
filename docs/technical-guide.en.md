# Shengmu technical guide

[English home](../README.en.md) · [中文技术指南](technical-guide.md)

## Models and devices

| Model family | Languages and purpose | Runtime and limits |
|---|---|---|
| SenseVoice INT8 | Mandarin Chinese, Cantonese, English, Japanese, Korean; default for new installations | CPU, VAD segmentation and periodic previews. The model itself is not natively streaming. |
| Whisper tiny/base/small/medium/large-v3/Turbo | Multilingual rolling captions with word times | CPU INT8 or NVIDIA GPU FP16/INT8_FLOAT16. Larger models use more RAM/VRAM; quantization is not faster on every device. |
| tiny.en/base.en/distil-large-v3 | English only | Not for Chinese, Japanese, or Korean speech. |
| Zipformer Transducer/CTC | Chinese/English or English, depending on the option | Native CPU streaming, experimental. Short-sample tests still show omissions/errors; specify the source language for translation. |

Model filters change the list, not the active model. Advanced Whisper presets change decoder parameters without switching the model or device. Runtime details show the actual device, processing time, and backlog; those values are not complete acoustic-to-visible latency.

## Audio capture and beta tracking

The app targets Windows 10/11 x64. System-playback sources capture loopback audio from the selected output device; microphone sources capture an input device. Beta automatic app tracking uses process loopback and requires Windows 10 build 20348 or later, with application compatibility limits. Use an explicit playback device to troubleshoot missing input.

## Downloads, storage, and offline use

The main ZIP contains no speech or translation models. Missing components are prepared on demand; GPU options may also need compatible runtime components. The downloader provides progress, pause/resume, and integrity checks appropriate to the model. CPU SenseVoice needs no CUDA runtime.

| Content | Windows location |
|---|---|
| Settings | `%APPDATA%/AudioSubtitleOverlay/settings.json` |
| Speech models | `%APPDATA%/AudioSubtitleOverlay/models` |
| Local translation models | `%APPDATA%/AudioSubtitleOverlay/translation_models` |
| GPU components | `%APPDATA%/AudioSubtitleOverlay/runtime/cuda12` |
| Partial component downloads | `%APPDATA%/AudioSubtitleOverlay/downloads/components` |
| Runtime logs | `%APPDATA%/AudioSubtitleOverlay/logs` |

Prepare the required speech model and translation language pairs while connected, then use the same user-data directory offline. The optional tiny ZIP merges into `AudioSubtitleOverlay/models/tiny` beside the executable and is for manually selecting Whisper tiny. It does not supply SenseVoice or other Whisper models.

Local translation depends on availability of the selected language pair. MyMemory sends translation text to its online service; local-first mode can also connect when fallback is allowed. Identical source and target languages skip translation.

## Reading and lifecycle

Original text and translations are paired by segment. The latest 64 segments can be reviewed, copied, or exported. Recognition continues while history is scrolled; return to the latest text to follow live output. New sentence-ending punctuation is confirmed across windows before irreversible splitting, while drafts appear immediately.

Stopping rejects new audio and stale callbacks, then enables Start after worker/model cleanup. Translation-only changes preserve recognized source text. The overlay temporarily yields its topmost position while configuration has focus, then restores the saved preference.

Normal launches respect the user's automatic-start setting. `AudioSubtitleOverlay.exe --no-autostart` opens Settings without starting capture for that launch. Runtime logs are bounded by 30 days/30 MiB; recordings and test evidence are separate from runtime logs.

## Explicit local tracing

Normal launches do not save audio recordings or full recognition text. For a bounded diagnostic session:

```text
AudioSubtitleOverlay.exe --trace-dir <absolute-output-directory> --trace-seconds 120 --trace-audio --trace-text
python tools/trace_live.py reference <run-directory> --output <run-directory>/reference.json
python tools/trace_live.py analyze <run-directory> --reference <run-directory>/reference.json --output <run-directory>/timing-report.json --csv <run-directory>/phrase-timing.csv
```

Audio and text are separate opt-ins. Tracing defaults to 120 seconds, allows at most 600 seconds, and has a default 16 MiB limit and bounded queue. It stops at a limit, reports write errors/dropped/pending events, and does not block captions on failure. The reference command uses an already-cached local model without downloading or uploading. Review audio and text before sharing them.

Process loopback retains WASAPI QPC packet timing; ordinary system loopback uses callback estimates. Report them separately. Source versions are joined to the lines actually visible in a submitted image, using session IDs and sequence numbers. Submission time is not physical monitor scanout. Automatic reference word boundaries remain estimates, and exact-match phrases favor easier content. Match coverage is not accuracy; accuracy needs a human-reviewed reference for the same audio.

## Development and release

See [Windows dependencies](../requirements-windows.txt) and [test dependencies](../requirements-test.txt).

```text
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
build_windows.bat
package_windows.bat
```

PyInstaller builds the executable; large models and GPU components are prepared separately. Public packages require committed clean source, the correct version, and source fingerprints. A modified local-development build is not a formal release. See the [release procedure](releasing.md) and [implementation notes, in Chinese](v1.4-engineering.md). Third-party terms are in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
