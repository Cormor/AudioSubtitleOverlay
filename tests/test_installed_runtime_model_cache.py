"""Keep isolated acceptance models managed and shared, not a custom override."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tools import verify_installed_runtime as module


class InstalledRuntimeModelCacheTests(unittest.TestCase):
    def folders(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        real, isolated = root/'formal', root/'isolated'
        real.mkdir(); isolated.mkdir()
        for name in ('models', 'translation_models'):
            (real/name).mkdir()
        created = []
        def cleanup():
            for destination in reversed(created):
                if os.name == 'nt': os.rmdir(destination)
                else: destination.unlink()
        self.addCleanup(cleanup)
        def create(destination, source):
            if os.name == 'nt':
                subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(destination),str(source)],
                               check=True,capture_output=True)
            else:
                destination.symlink_to(source, target_is_directory=True)
            created.append(destination)
        return real, isolated, create, created

    def test_managed_small_and_sensevoice_remain_distinct_shared_entities(self):
        real, isolated, create, created = self.folders()
        for model, file in [('sensevoice-int8','model.int8.onnx'),('small','model.bin')]:
            folder=real/'models'/model; folder.mkdir(); (folder/file).write_bytes(b'fixture')
        module.link_shared_model_caches(real, isolated, create_link=create)
        self.assertTrue((isolated/'models/small/model.bin').samefile(real/'models/small/model.bin'))
        self.assertTrue((isolated/'models/sensevoice-int8/model.int8.onnx').samefile(real/'models/sensevoice-int8/model.int8.onnx'))
        self.assertTrue((isolated/'translation_models').samefile(real/'translation_models'))
        self.assertEqual(len(created), 2)

    def test_existing_correct_links_are_idempotent(self):
        real, isolated, create, created = self.folders()
        module.link_shared_model_caches(str(real), str(isolated), create_link=create)
        module.link_shared_model_caches(real, isolated, create_link=create)
        self.assertEqual(len(created), 2)

    def test_missing_cache_stops_before_creating_any_link(self):
        real, isolated, create, created = self.folders()
        (real/'translation_models').rmdir()
        with self.assertRaises(ValueError): module.link_shared_model_caches(real, isolated, create_link=create)
        self.assertFalse(created)

    def test_existing_different_cache_is_preserved_and_rejected(self):
        real, isolated, create, created = self.folders()
        (isolated/'models').mkdir()
        sentinel=isolated/'models/keep.txt'; sentinel.write_text('keep')
        with self.assertRaises(ValueError): module.link_shared_model_caches(real, isolated, create_link=create)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertFalse(created)

    def test_uncreated_link_cannot_be_reported_as_success(self):
        real, isolated, _, _ = self.folders()
        with self.assertRaises(RuntimeError):
            module.link_shared_model_caches(real, isolated, create_link=lambda *args: None)

    def test_formal_directory_or_invalid_path_input_is_rejected(self):
        real, isolated, create, created = self.folders()
        with self.assertRaises(ValueError): module.link_shared_model_caches(real, real, create_link=create)
        with self.assertRaises(TypeError): module.link_shared_model_caches(None, isolated, create_link=create)
        self.assertFalse(created)

    def test_cli_settings_leave_custom_model_override_empty(self):
        settings=module.build_acceptance_settings({},source_name='Synthetic speaker')
        self.assertEqual(settings['model_path'],'')
        self.assertEqual(settings['recognition_model'],'sensevoice-int8')



if __name__ == '__main__':
    unittest.main()
