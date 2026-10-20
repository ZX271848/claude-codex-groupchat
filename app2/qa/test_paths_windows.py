"""Clickable paths on Windows: drive letters from file URLs and Git Bash, and relative paths that look like them."""
import os
import tempfile
import unittest
from pathlib import Path

from qa.test_coordinator import A, TEMP


@unittest.skipUnless(os.name == "nt", "Windows path forms")
class WindowsPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.root = Path(self.tmp.name)
        (self.root / "a").mkdir()
        self.target = self.root / "a" / "b.md"
        self.target.write_text("x", encoding="utf-8")
        self.room = {"root": str(self.root), "dir": str(self.root)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_letter_folder_stays_relative(self):
        for value in ("a/b.md", "a\\b.md", "a/b.md:3", "a/b.md#L2"):
            self.assertEqual(A.resolve_path(value, self.room), str(self.target), value)

    def test_drive_letter_forms(self):
        posix = str(self.target).replace("\\", "/")
        drive, rest = posix[0], posix[2:]
        for value in (posix, "file:///" + posix, f"/{drive.lower()}{rest}", f"/{drive}:{rest}"):
            self.assertEqual(os.path.normcase(A.resolve_path(value, self.room) or ""), os.path.normcase(str(self.target)), value)


if __name__ == "__main__":
    unittest.main()
