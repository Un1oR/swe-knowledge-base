from pathlib import Path
import tempfile
import unittest

from add_site_verification import VERIFICATION_TAG, add_site_verification


class SiteVerificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "index.html"

    def test_adds_tag_to_head_once_and_preserves_content(self):
        html = "<html><head><title>База знаний</title></head><body>Статьи</body></html>"
        self.path.write_text(html, encoding="utf-8")

        add_site_verification(self.path)
        add_site_verification(self.path)

        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            html.replace("</head>", VERIFICATION_TAG + "</head>", 1),
        )

    def test_missing_head_fails_and_preserves_file(self):
        html = "<html><body>Статьи</body></html>"
        self.path.write_text(html, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Missing </head>"):
            add_site_verification(self.path)

        self.assertEqual(self.path.read_text(encoding="utf-8"), html)


if __name__ == "__main__":
    unittest.main()
