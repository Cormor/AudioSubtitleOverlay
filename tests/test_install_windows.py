import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.install_windows import prepare_installation


class InstallationTests(unittest.TestCase):
    def package(self, path, payload=b"application", extra=None):
        with zipfile.ZipFile(path,"w") as archive:
            archive.writestr("AudioSubtitleOverlay/版本.txt","版本：v1.3.0\n")
            archive.writestr("AudioSubtitleOverlay/AudioSubtitleOverlay.exe",payload)
            archive.writestr("AudioSubtitleOverlay/_internal/runtime.dll",b"runtime")
            if extra:
                archive.writestr(extra,b"bad")
        return path

    def test_exact_package_is_verified_and_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            package=self.package(root/"app.zip")
            first,manifest=prepare_installation(package,root/"installed")
            second,again=prepare_installation(package,root/"installed")
            self.assertEqual(first,second)
            self.assertEqual(manifest,again)
            self.assertTrue((first/manifest["executable"]).is_file())

    def test_changed_same_version_does_not_overwrite_old_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            first,_=prepare_installation(self.package(root/"first.zip"),root/"installed")
            second,_=prepare_installation(self.package(root/"second.zip",b"changed"),root/"installed")
            self.assertNotEqual(first,second)
            self.assertEqual((first/"AudioSubtitleOverlay/AudioSubtitleOverlay.exe").read_bytes(),b"application")

    def test_corrupted_existing_installation_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            package=self.package(root/"app.zip")
            first,_=prepare_installation(package,root/"installed")
            (first/"AudioSubtitleOverlay/_internal/runtime.dll").write_bytes(b"tampered")
            with self.assertRaises(RuntimeError):
                prepare_installation(package,root/"installed")

    def test_unsafe_members_are_rejected_before_extracting(self):
        for member in ("../outside.txt","AudioSubtitleOverlay/../../outside.txt",
                       "AudioSubtitleOverlay/_INTERNAL/runtime.dll"):
            with self.subTest(member=member),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                package=self.package(root/"app.zip",extra=member)
                with self.assertRaises(ValueError):
                    prepare_installation(package,root/"installed")
                self.assertFalse((root/"installed").exists())
