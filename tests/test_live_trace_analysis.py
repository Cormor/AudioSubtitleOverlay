import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import wave

from tools.trace_live import analyze


class LiveTraceAnalysisTests(unittest.TestCase):
    def fixture(self, folder):
        root = Path(folder)
        with wave.open(str(root / "audio.wav"), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 160000)
        (root / "trace.json").write_text(json.dumps(dict(error=None, dropped_events=0,
            pending_events=0, audio_included=True, text_included=True)))
        events = [
            dict(kind="audio", perf=104, file_sample_start=0, file_sample_end=64000,
                 timing=dict(clock="wasapi_qpc", packets=[dict(sample_start=0, sample_end=64000, engine_start=100, flags=0)])),
            dict(kind="source", pipeline_id="p", sequence=1, block_id=1,
                 text="prefixabcdefgh", received_at=104, perf=104.3),
            dict(kind="frame", presented_at=104.4, reading_mode="live", visible=[
                dict(pipeline_id="p", sequence=1, block_id=1, fragments=[dict(text="prefix", char_start=0, char_end=6)])]),
            dict(kind="frame", presented_at=105.1, reading_mode="live", visible=[
                dict(pipeline_id="p", sequence=1, block_id=1, fragments=[dict(text="abcdefgh", char_start=6, char_end=14)])]),
        ]
        (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        ref = dict(audio_sha256=hashlib.sha256((root / "audio.wav").read_bytes()).hexdigest(),
                   audio_seconds=10, words=[dict(text="abcdefgh", start=3, end=4)])
        (root / "reference.json").write_text(json.dumps(ref))
        return root, events, ref

    def test_hidden_tail_is_not_counted_until_the_actual_line_is_painted(self):
        with tempfile.TemporaryDirectory() as folder:
            root, _, _ = self.fixture(folder)
            result = analyze(root, root / "reference.json")
            self.assertEqual(result["invalid_frame_versions"], 0)
            self.assertEqual(len(result["phrases"]), 1)
            self.assertAlmostEqual(result["phrases"][0]["source_after_end_seconds"], .3)
            self.assertAlmostEqual(result["phrases"][0]["visible_after_end_seconds"], 1.1)
            self.assertAlmostEqual(result["callback_to_frame_seconds"]["p50"], .1)

    def test_wrong_recording_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, _, ref = self.fixture(folder)
            ref["audio_sha256"] = "wrong recording"
            (root / "reference.json").write_text(json.dumps(ref))
            with self.assertRaises(ValueError):
                analyze(root, root / "reference.json")

    def test_mismatched_leading_word_does_not_hide_exact_following_phrase(self):
        with tempfile.TemporaryDirectory() as folder:
            root, _, ref = self.fixture(folder)
            ref["words"].insert(0, dict(text="wrong", start=3, end=3.1))
            (root / "reference.json").write_text(json.dumps(ref))
            result = analyze(root, root / "reference.json")
            self.assertEqual([p["phrase"] for p in result["phrases"]], ["abcdefgh"])

    def test_unknown_displayed_version_is_reported_as_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            root, events, _ = self.fixture(folder)
            events[-1]["visible"][0]["sequence"] = 999
            (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            result = analyze(root)
            self.assertEqual(result["invalid_frame_versions"], 1)
            self.assertFalse(result["evidence_complete"])


if __name__ == "__main__":
    unittest.main()
