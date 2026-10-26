from pathlib import Path
import tempfile
import unittest
import zipfile

from package_minimal import build_archive


class PackagingTests(unittest.TestCase):
    def test_generated_metadata_replaces_stale_files_without_duplicate_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/"app"
            source.mkdir()
            (source/"AudioSubtitleOverlay.exe").write_bytes(b"exe")
            for name in ("使用说明.txt","版本.txt","LICENSE","THIRD_PARTY_NOTICES.md","COMMERCIAL-LICENSE.md"):
                (source/name).write_text("stale",encoding="utf-8")
            target=root/"app.zip"
            build_archive(source,target)
            with zipfile.ZipFile(target) as archive:
                names=archive.namelist()
                self.assertEqual(len(names),len(set(name.casefold() for name in names)))
                self.assertNotEqual(archive.read("AudioSubtitleOverlay/LICENSE"),b"stale")
                self.assertIn("版本：v",archive.read("AudioSubtitleOverlay/版本.txt").decode("utf-8"))


class BuildProvenanceTests(unittest.TestCase):
    def test_uncommitted_build_requires_explicit_local_mode_and_cannot_ship_from_ci(self):
        import hashlib
        import json
        from tools.package_and_install import verify_build
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/'app'
            (source/'_internal').mkdir(parents=True)
            code=root/'main.py'
            code.write_text('print("local")\n')
            build=dict(version='1.3.1',source_commit='a'*40,source_state='modified',
                       source_files={'main.py':hashlib.sha256(code.read_bytes()).hexdigest()})
            (source/'_internal/BUILD_INFO.json').write_text(json.dumps(build))
            with self.assertRaises(RuntimeError):verify_build(source,'1.3.1',root=root)
            self.assertEqual(verify_build(source,'1.3.1',local_build=True,root=root),build)
            with self.assertRaises(RuntimeError):verify_build(source,'1.3.1',local_build=True,ci=True,root=root)
            code.write_text('print("changed after build")\n')
            with self.assertRaises(RuntimeError):verify_build(source,'1.3.1',local_build=True,root=root)
