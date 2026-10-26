import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

import numpy as np

import runtime_trace
from runtime_trace import RuntimeTrace
from subtitle_events import SourceUpdate


class RuntimeTraceTests(unittest.TestCase):
    def update(self):
        return SourceUpdate(4, 2, "private spoken words", "en", 1.0,
                            10.0, 10.1, 10.2, 9.9, pipeline_id="pipeline-a",
                            word_spans=(("private", .5, .9),))

    def test_default_does_not_create_a_recording(self):
        with patch.object(runtime_trace, "_trace", None), patch("sys.argv", ["app"]), patch.dict(
                os.environ, {"ASR_TRACE_DIR": ""}):
            trace = runtime_trace.get_runtime_trace()
            self.assertFalse(trace.enabled)
            self.assertFalse(trace.record_source(self.update()))
            self.assertIsNone(trace.directory)

    def test_metadata_trace_excludes_spoken_text_and_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = RuntimeTrace(folder)
            trace.record_source(self.update())
            trace.record_presented(self.update(), 10.25, "live")
            trace.record_frame(10.25, "live", [{"fragments": [{"text": "private visible words"}]}])
            trace.close()
            self.assertTrue(trace._closed.wait(1))
            content = (trace.directory / "events.jsonl").read_text(encoding="utf8")
            self.assertNotIn("private", content)
            self.assertNotIn("word_spans", content)
            self.assertFalse((trace.directory / "audio.wav").exists())

    def test_explicit_audio_text_and_presented_version_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = RuntimeTrace(folder, include_audio=True, include_text=True)
            trace.record_audio(np.full(320, .1, np.float32), 10.0, "capture-a",
                               {"clock": "wasapi_qpc", "packets": [{"engine_start": 9.98}]})
            trace.record_source(self.update())
            trace.record_presented(self.update(), 10.25, "live")
            trace.close()
            self.assertTrue(trace._closed.wait(1))
            rows = [json.loads(x) for x in (trace.directory / "events.jsonl").read_text(encoding="utf8").splitlines()]
            self.assertEqual([r["kind"] for r in rows], ["audio", "source", "presented"])
            self.assertEqual(rows[2]["text"], rows[1]["text"])
            self.assertEqual((rows[2]["pipeline_id"], rows[2]["sequence"]), ("pipeline-a", 4))
            self.assertEqual(rows[2]["presented_at"], 10.25)
            with wave.open(str(trace.directory / "audio.wav"), "rb") as audio:
                self.assertEqual((audio.getnframes(), audio.getframerate()), (320, 16000))

    def test_recording_stops_at_capacity_without_replacing_prior_data(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel = Path(folder) / "keep.txt"
            sentinel.write_text("original")
            trace = RuntimeTrace(folder, maximum_bytes=4096)
            trace.record("oversized", detail="x" * 5000)
            self.assertTrue(trace._closed.wait(2))
            metadata = json.loads((trace.directory / "trace.json").read_text(encoding="utf8"))
            self.assertEqual(metadata["finish_reason"], "size_limit")
            self.assertLessEqual(sum(p.stat().st_size for p in trace.directory.iterdir()), 4096)
            self.assertEqual(sentinel.read_text(), "original")
            self.assertFalse(trace.record("late"))

    def test_time_limit_ends_even_without_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = RuntimeTrace(folder, seconds=.1)
            self.assertTrue(trace._closed.wait(2))
            metadata = json.loads((trace.directory / "trace.json").read_text(encoding="utf8"))
            self.assertEqual(metadata["finish_reason"], "time_limit")
            self.assertFalse(trace.enabled)

    def test_bad_evidence_does_not_raise_into_the_pipeline(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = RuntimeTrace(folder)
            self.assertTrue(trace.record("invalid", value=float("nan")))
            self.assertTrue(trace._closed.wait(2))
            self.assertFalse(trace.enabled)
            metadata = json.loads((trace.directory / "trace.json").read_text(encoding="utf8"))
            self.assertEqual(metadata["finish_reason"], "write_error")

    def test_full_queue_still_finishes_when_close_sentinel_cannot_fit(self):
        gate = threading.Event()
        original = RuntimeTrace._write
        def blocked(trace):
            gate.wait(3)
            original(trace)
        with tempfile.TemporaryDirectory() as folder, patch.object(RuntimeTrace, "_write", blocked):
            trace = RuntimeTrace(folder)
            for _ in range(512):
                trace.record("queued")
            trace.close()
            gate.set()
            self.assertTrue(trace._closed.wait(2))
            self.assertFalse(trace._thread.is_alive())

    def test_relative_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            RuntimeTrace("relative-output")

    def test_actual_app_entrypoint_accepts_all_diagnostic_switches(self):
        from main import argument_parser
        args = argument_parser().parse_args(["--no-autostart", "--trace-dir", "E:\\AI\\test",
            "--trace-seconds", "180", "--trace-audio", "--trace-text"])
        self.assertTrue(args.no_autostart)
        self.assertEqual(args.trace_seconds, 180)
        self.assertTrue(args.trace_audio and args.trace_text)

    def test_frame_keeps_only_visible_text_and_deduplicates_unchanged_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = RuntimeTrace(folder, include_text=True)
            visible = [{"pipeline_id": "a", "sequence": 4, "fragments": [{"text": "visible tail"}]}]
            self.assertTrue(trace.record_frame(11.0, "live", visible))
            self.assertFalse(trace.record_frame(11.1, "live", visible))
            trace.close()
            rows = [json.loads(x) for x in (trace.directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["presented_at"], 11.0)
            self.assertEqual(rows[0]["visible"][0]["fragments"][0]["text"], "visible tail")

    def test_main_frame_trace_does_not_invent_visibility_for_the_whole_block(self):
        from types import SimpleNamespace
        from main import Application
        import main
        calls = []
        update = SimpleNamespace(sequence=4, pipeline_id="a")
        fragments = [{"text": "only visible tail", "char_start": 40, "char_end": 57}]
        app = SimpleNamespace(
            _history=SimpleNamespace(blocks={2: SimpleNamespace(sequence=4)}, _preview_block_id=None),
            _trace_versions={2: update},
            overlay=SimpleNamespace(visible_source_fragments={0: fragments}, _follow_tail=True))
        with patch.object(main, "get_runtime_trace", return_value=SimpleNamespace(record_frame=lambda *args: calls.append(args))):
            Application._record_visible_frame(app, 11.0)
        self.assertEqual(calls[0][2], [{"pipeline_id": "a", "sequence": 4, "block_id": 2, "fragments": fragments}])

    def test_configuration_focus_and_style_changes_preserve_topmost_preference(self):
        from types import SimpleNamespace
        from overlay import OverlayWindow
        calls = []
        overlay = OverlayWindow.__new__(OverlayWindow)
        overlay.settings = SimpleNamespace(overlay_topmost=True)
        overlay._configuration_active = False
        overlay._settings_master = object()
        overlay._request_redraw = lambda: None
        overlay.window = SimpleNamespace(wm_attributes=lambda *args: calls.append(args),
                                         winfo_ismapped=lambda: True, lower=lambda target: None)
        overlay.set_configuration_active(True)
        overlay.apply_settings()
        self.assertEqual(calls[-1], ("-topmost", False))
        overlay.set_configuration_active(False)
        self.assertEqual(calls[-1], ("-topmost", True))
        self.assertTrue(overlay.settings.overlay_topmost)


if __name__ == "__main__":
    unittest.main()
