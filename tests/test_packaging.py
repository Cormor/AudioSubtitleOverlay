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
