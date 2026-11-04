import os
import tempfile
import unittest
from pathlib import Path

from hub import files


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "share"
        (self.root / "sub" / "deep").mkdir(parents=True)
        (self.root / ".git").mkdir()
        (self.root / "b.txt").write_text("b")
        (self.root / "A.md").write_text("a")
        (self.root / "sub" / "deep" / "needle.md").write_text("x")
        (self.root / ".git" / "needle-in-git").write_text("x")
        (self.tmp / "secret.txt").write_text("top secret")
        self.roots = {"share": str(self.root)}

    def test_paths_that_escape_the_root_are_refused(self):
        for rel in ["../secret.txt", "sub/../../secret.txt", "/etc/passwd", "..\\secret.txt"]:
            with self.subTest(rel=rel):
                try:
                    target = files.resolve(self.roots, "share", rel)
                except files.FileError as exc:
                    self.assertEqual(exc.status, 403)
                else:  # an absolute path is re-rooted under the share, which is also safe
                    target.relative_to(self.root.resolve())

    def test_unknown_root_is_404(self):
        with self.assertRaises(files.FileError) as caught:
            files.resolve(self.roots, "nope", "")
        self.assertEqual(caught.exception.status, 404)

    def test_a_symlink_pointing_outside_is_refused_and_hidden(self):
        link = self.root / "escape"
        try:
            os.symlink(self.tmp / "secret.txt", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        with self.assertRaises(files.FileError) as caught:
            files.resolve(self.roots, "share", "escape")
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn("escape", [e.name for e in files.list_dir(self.roots, "share")])

    def test_folders_come_first_then_names_ignoring_case(self):
        names = [e.name for e in files.list_dir(self.roots, "share")]
        self.assertEqual(names, [".git", "sub", "A.md", "b.txt"])

    def test_search_finds_nested_files_and_skips_hidden_folders(self):
        found = files.search(self.roots, "share", "needle")
        self.assertEqual([f["path"] for f in found], ["sub/deep/needle.md"])
        self.assertEqual(files.search(self.roots, "share", "  "), [])

    def test_html_is_never_served_as_a_page(self):
        page = self.root / "page.html"
        page.write_text("<script>alert(1)</script>")
        media_type, attachment = files.serving_type(page)
        self.assertTrue(media_type.startswith("text/plain"))
        self.assertFalse(attachment)
        svg = self.root / "x.svg"
        svg.write_text("<svg onload=alert(1)/>")
        self.assertTrue(files.serving_type(svg)[0].startswith("text/plain"))

    def test_images_inline_and_binary_downloads(self):
        png = self.root / "p.png"
        png.write_bytes(b"\x89PNG")
        self.assertEqual(files.serving_type(png), ("image/png", False))
        blob = self.root / "data.bin"
        blob.write_bytes(b"\xff\xfe\x00\x80")
        self.assertEqual(files.serving_type(blob), ("application/octet-stream", True))


if __name__ == "__main__":
    unittest.main()
