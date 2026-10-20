import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from settings import Settings


class StartupMigrationTests(unittest.TestCase):
    def test_new_install_waits_for_user_but_legacy_settings_keep_autostart(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{"APPDATA":temporary}):
            self.assertFalse(Settings.load().auto_start)
            folder=Path(temporary)/"AudioSubtitleOverlay"
            (folder/"settings.json").write_text(json.dumps({"recognition_beam_size":5}),encoding="utf-8")
            migrated=Settings.load()
            self.assertTrue(migrated.auto_start)
            self.assertEqual(migrated.recognition_beam_size,5)

    def test_explicit_startup_choices_are_not_overridden_by_migration(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{"APPDATA":temporary}):
            folder=Path(temporary)/"AudioSubtitleOverlay"
            folder.mkdir()
            (folder/"settings.json").write_text(json.dumps({"auto_start":False,"start_minimized":True}),encoding="utf-8")
            settings=Settings.load()
            self.assertFalse(settings.auto_start)
            self.assertTrue(settings.start_minimized)
