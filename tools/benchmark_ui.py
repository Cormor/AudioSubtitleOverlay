"""Measure owned Tk page switches with isolated settings and no audio capture.

Run the same command before/after a performance change; timings cover Tk work,
not the display compositor or physical click-to-photon latency.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def summary(values):
    ordered = sorted(values)
    return {"median_ms": statistics.median(values),
            "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
            "max_ms": max(values), "samples_ms": values}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--text-scale", type=float, default=1)
    parser.add_argument("--diagnostic-stock-theme", action="store_true",
                        help="Diagnostic comparison only; use Windows native ttk drawing")
    args = parser.parse_args()
    if not 2 <= args.samples <= 100 or not .5 <= args.text_scale <= 2:
        parser.error("samples must be 2..100 and text-scale .5..2")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    formal = Path(os.environ["APPDATA"]) / "AudioSubtitleOverlay/settings.json"
    before = formal.read_bytes() if formal.exists() else None
    os.environ["APPDATA"] = str(out / "isolated-appdata")
    source_names = ("main.py", "theme.py", "choice_popup.py", "application_views.py",
                    "overlay.py", "tools/benchmark_ui.py")
    def source_hashes():
        return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_names}
    loaded_sources = source_hashes()
    import tkinter as tk
    from main import Application
    from overlay import OverlayWindow
    from verify_installed_runtime import capture_visible_window
    root = tk.Tk()
    root.tk.call("tk", "scaling", float(root.tk.call("tk", "scaling")) * args.text_scale)
    with patch.object(OverlayWindow, "show"):
        app = Application(root, autostart=False, tray=False)
    app.overlay.hide()
    root.geometry("1000x820+80+30")
    errors = []
    root.report_callback_exception = lambda *error: errors.append(str(error[1]))

    def pump(seconds=.05):
        root.after(round(seconds * 1000), root.quit)
        root.mainloop()

    results = {"scope": "Tk page selection and pending UI work; no audio capture",
               "text_scale": args.text_scale, "themes": {}, "arguments": vars(args) | {"output": str(out)},
               "python": sys.version, "tk": str(root.tk.call("info", "patchlevel")),
               "source_sha256": loaded_sources}
    profiler = cProfile.Profile()
    try:
        pump(.3)
        for mode, label in (("dark", "深色黑金"), ("light", "浅色白金")):
            app.theme_label_var.set(label)
            app._on_theme_selected()
            if args.diagnostic_stock_theme:
                from tkinter import ttk
                ttk.Style(root).theme_use("vista")
            pump(.15)
            pages = {str(i): [] for i in range(5)}
            cpu = []
            profiler.enable()
            for repeat in range(args.samples):
                for index in range(5):
                    started = time.perf_counter()
                    cpu_started = time.process_time()
                    app.notebook.select(index)
                    root.update()
                    pages[str(index)].append((time.perf_counter() - started) * 1000)
                    cpu.append((time.process_time() - cpu_started) * 1000)
            profiler.disable()
            results["themes"][mode] = {key: summary(values) for key, values in pages.items()}
            results["themes"][mode]["all"] = summary([v for values in pages.values() for v in values])
            results["themes"][mode]["cpu"] = summary(cpu)
            # Keep a visible result for geometry/style review.
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                user = ctypes.windll.user32
                user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
                user.GetAncestor.restype = wintypes.HWND
                capture_visible_window({"hwnd": user.GetAncestor(root.winfo_id(), 2)}, out / (mode + ".png"))
        results["capture_started"] = app.pipeline is not None
    finally:
        profiler.disable()
        app.close()
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(45)
    (out / "profile.txt").write_text(stream.getvalue(), encoding="utf-8")
    results["callback_errors"] = errors
    results["formal_settings_unchanged"] = (formal.read_bytes() if formal.exists() else None) == before
    results["sources_unchanged"] = source_hashes() == loaded_sources
    (out / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "themes": {k: {name: value for name, value in v["all"].items()
                                                        if name != "samples_ms"} for k, v in results["themes"].items()},
                      "callback_errors": errors, "formal_settings_unchanged": results["formal_settings_unchanged"]}))
    if errors or results["capture_started"] or not results["formal_settings_unchanged"] or not results["sources_unchanged"]:
        raise SystemExit("UI benchmark failed correctness checks")


if __name__ == "__main__":
    main()
