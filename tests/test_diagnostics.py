import json
from pathlib import Path
import tempfile
import unittest
from diagnostics import export_diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_allowlists_exclude_private_paths_text_audio_and_error_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'report.json'
            export_diagnostics(path, version='2.0.0',
                settings={'recognition_model':'sensevoice-int8','model_path':'secret-path','loopback_device':'private name'},
                metrics={'inference_seconds':float('nan'),'text':'private transcript'},
                state={'phase':'recognizing','error':'private error with transcript'},
                build={'version':'2.0.0','source_commit':'abc','source_files':{'private':'secret'}})
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn('private', raw)
            self.assertNotIn('secret', raw)
            self.assertIsNone(json.loads(raw)['metrics']['inference_seconds'])
