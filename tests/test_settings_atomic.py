import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from settings import Settings, SettingsLoadError


class SettingsAtomicTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.patch = patch.dict(os.environ, {"APPDATA": self.temporary.name})
        self.patch.start()
        self.path = Path(self.temporary.name) / "AudioSubtitleOverlay" / "settings.json"
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.temporary.cleanup()

    def test_unknown_fields_round_trip_but_transient_fields_do_not(self):
        self.path.write_text(json.dumps({"future_setting": {"a": 1}, "auto_start": False}), encoding="utf-8")
        loaded = Settings.load()
        loaded.ui_theme = "dark"
        loaded.save()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["future_setting"], {"a": 1})
        self.assertEqual(saved["ui_theme"], "dark")
        self.assertEqual(saved["schema_version"], 2)
        self.assertNotIn("_extra", saved)
        self.assertNotIn("_load_error", saved)

    def test_non_object_and_non_finite_json_are_preserved_and_cannot_be_saved_over(self):
        for raw in ("[]", "NaN", '{"overlay_text_opacity": Infinity}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                loaded = Settings.load()
                self.assertTrue(Settings.last_load_warning)
                with self.assertRaises(SettingsLoadError):
                    loaded.save()
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_invalid_geometry_color_and_font_sizes_preserve_original(self):
        cases = (
            {"overlay_geometry": "invalid"},
            {"overlay_background": "red"},
            {"overlay_font_size": 200},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                raw = json.dumps(payload)
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(SettingsLoadError):
                    Settings.load().save()
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_atomic_replace_failure_keeps_previous_file_and_cleans_temp(self):
        self.path.write_text('{"auto_start": false}', encoding="utf-8")
        loaded = Settings.load()
        loaded.ui_theme = "light"
        with patch("settings.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                loaded.save()
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"auto_start": false}')
        self.assertEqual(list(self.path.parent.glob(".settings.json.*.tmp")), [])

