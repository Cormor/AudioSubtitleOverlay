"""Compare identical caption composition, without capture, GPU use, or desktop windows."""

import argparse
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
    window.canvas = SimpleNamespace(winfo_width=lambda: 770, winfo_height=lambda: 983)
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


def measure(cls, samples):
    window = renderer(cls)
    values = []
    for index in range(samples + 3):
        start = time.perf_counter()
        window._draw_content(27, start)
        if index >= 3:
            values.append((time.perf_counter() - start) * 1000)
    return {"median_ms": statistics.median(values), "p95_ms": sorted(values)[min(len(values)-1, int(.95*len(values)))], "samples": values}, window


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--baseline", help="Local Git ref to compare; does not fetch")
    group.add_argument("--baseline-source", type=Path, help="Exported baseline overlay.py for cross-OS worktrees")
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    source = (args.baseline_source.read_text(encoding="utf-8") if args.baseline_source else
              subprocess.check_output(["git","show",f"{args.baseline}:overlay.py"], cwd=ROOT).decode("utf-8"))
    baseline = ModuleType("baseline_overlay")
    exec(compile(source,"baseline_overlay.py","exec"),baseline.__dict__)
    before, _ = measure(baseline.OverlayWindow,args.samples)
    after, window = measure(OverlayWindow,args.samples)
    result = {"baseline_ref":args.baseline,"scope":"caption composition only; excludes model inference, Tk queue and native presentation", "before":before,"after":after}
    output = ROOT / "verification_rendering"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    window._last_image.save(output / "captions.png")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
