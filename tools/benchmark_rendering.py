"""Compare caption pixels and CPU composition without audio or desktop windows."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from overlay import OverlayWindow
from settings import Settings
from subtitle_layout import EntryView, Motion1D, ParagraphLedger, TranslationLaneState


def renderer(cls):
    window = cls.__new__(cls)
    window.settings = Settings(overlay_font_size=27)
    window._benchmark_size = (770, 983)
    window.canvas = SimpleNamespace(winfo_width=lambda: window._benchmark_size[0],
                                    winfo_height=lambda: window._benchmark_size[1])
    window.window = SimpleNamespace(winfo_fpixels=lambda _: 96)
    window._fonts = {}
    window._layout_cache_key = None
    window._layout_cache = None
    window._entries_view = [EntryView(f"第{i}段：声音变成清晰的字幕，原文和译文一起显示。", "Speech becomes readable captions with translation.") for i in range(64)]
    window._ledger = ParagraphLedger()
    window._show_translation = True
    window._same_language = False
    window._follow_tail = True
    window._translation_lane_state = TranslationLaneState()
    window._translation_lane_state.configure(None, "zh", True)
    window._scroll_motion = Motion1D(0, value=0)
    window._scroll_target = 0
    window._auto_scroll_after = None
    window._drag_mode = ""
    window._scrollbar_show_until = 0
    window._update_auto_height = lambda _: False
    window._layer = SimpleNamespace(present=lambda _: None)
    window.on_presented = None
    return window


SCENARIOS = ("static", "scroll", "tail_update", "resize", "background_color",
             "background_opacity", "corner_radius", "text_opacity")


def prepare_frame(window, scenario, index):
    if scenario == "scroll":
        window._follow_tail = False
        window._scroll_motion.snap(float((index % 20) * 37))
    elif scenario == "tail_update":
        entry = window._entries_view[-1]
        window._entries_view[-1] = replace(entry, source=f"最新字幕持续更新，第{index}个片段。" + "文字" * (index % 8))
        window._layout_cache_key = None
    elif scenario == "resize":
        window._benchmark_size = ((770, 983), (690, 620), (900, 740))[index % 3]
    elif scenario == "background_color":
        window.settings.overlay_background = ("#000000", "#245678", "#fafafa")[index % 3]
    elif scenario == "background_opacity":
        window.settings.overlay_background_opacity = (0.0, 0.35, 1.0)[index % 3]
    elif scenario == "corner_radius":
        window.corner_radius = (0, 12, 32)[index % 3]
    elif scenario == "text_opacity":
        window.settings.overlay_text_opacity = (0.0, 0.37, 1.0)[index % 3]


def summary(values):
    return {"median_ms": statistics.median(values),
            "p95_ms": sorted(values)[min(len(values) - 1, int(.95 * len(values)))],
            "samples_ms": values}


def compare_case(baseline_class, scenario, samples, output):
    windows = (renderer(baseline_class), renderer(OverlayWindow))
    values = ([], [])
    mismatches = []
    # Both classes receive exactly the same state changes. Alternating timing
    # order reduces bias from CPU frequency and other processes on the machine.
    for index in range(samples + 3):
        for window in windows:
            prepare_frame(window, scenario, index)
        for which in ((0, 1) if index % 2 == 0 else (1, 0)):
            start = time.perf_counter()
            windows[which]._draw_content(27, start)
            elapsed = (time.perf_counter() - start) * 1000
            if index >= 3:
                values[which].append(elapsed)
        before, after = (window._last_image for window in windows)
        # Compare every RGBA byte; alpha-only differences must also fail.
        if before.size != after.size or before.tobytes() != after.tobytes():
            mismatches.append(index)
    windows[1]._last_image.save(output / f"{scenario}.png")
    if mismatches:
        windows[0]._last_image.save(output / f"{scenario}-baseline.png")
    before, after = (summary(samples_ms) for samples_ms in values)
    return {"before": before, "after": after,
            "median_improvement_percent": 100 * (before["median_ms"] - after["median_ms"]) / before["median_ms"],
            "pixels_equal": not mismatches, "compared_frames": samples + 3,
            "mismatch_frames": mismatches,
            "cache_state": "warm" if scenario in ("static", "scroll", "tail_update", "text_opacity") else "changed every frame"}


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--baseline", help="Local Git ref to compare; does not fetch")
    group.add_argument("--baseline-source", type=Path, help="Exported baseline overlay.py")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", required=True, type=Path, help="Absolute task evidence directory")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    if not args.output.is_absolute():
        parser.error("--output must be absolute")
    source = (args.baseline_source.read_text(encoding="utf-8") if args.baseline_source else
              subprocess.check_output(["git", "show", f"{args.baseline}:overlay.py"], cwd=ROOT).decode("utf-8"))
    baseline = ModuleType("baseline_overlay")
    exec(compile(source, "baseline_overlay.py", "exec"), baseline.__dict__)
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"baseline": str(args.baseline_source or args.baseline),
              "scope": "CPU caption composition; excludes model inference, Tk queue and native presentation",
              "samples_per_case": args.samples,
              "scenarios": {scenario: compare_case(baseline.OverlayWindow, scenario, args.samples, args.output)
                            for scenario in SCENARIOS}}
    result["pixels_equal"] = all(case["pixels_equal"] for case in result["scenarios"].values())
    (args.output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"pixels_equal": result["pixels_equal"], "result": str(args.output / "result.json"),
                      "scenarios": {name: {key: value for key, value in case.items() if key in
                                             ("median_improvement_percent", "pixels_equal", "cache_state")}
                                    for name, case in result["scenarios"].items()}}))
    if not result["pixels_equal"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
