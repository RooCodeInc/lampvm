"""Captions `lampvm video` draws stay inside the movie and off the picture.

Builds src/videoedit.swift and cuts a black clip made by ffmpeg, so every pixel that is
not black belongs to a caption. Skipped without swiftc or ffmpeg.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# The prompt whose caption ran off the right edge of a Lamp video.
PROMPT = ("Asking Lamp: Try the app again with one use_app call holding exactly these six actions in this order: "
          "click Left, click Right, type Ada into Name, click Greet, click Left, click Right. "
          "Then tell me in one line what happened. Don't change any code.")


@unittest.skipUnless(shutil.which("xcrun") and shutil.which("ffmpeg"), "needs swiftc and ffmpeg")
class CaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="lampvm-videoedit-")
        cls.dir = Path(cls.temporary.name)
        cls.editor = cls.dir / "videoedit"
        subprocess.run(["xcrun", "swiftc", "-O", str(ROOT / "src/videoedit.swift"), "-o", str(cls.editor), "-suppress-warnings"], check=True)
        cls.clip = cls.dir / "take.mov"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=1440x900:r=30:d=4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.clip)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def cut(self, name, text, width):
        timeline = self.dir / (name + ".json")
        timeline.write_text(json.dumps([{"seconds": 0.5, "text": text}]))
        movie = self.dir / (name + ".mp4")
        out = subprocess.run([str(self.editor), str(self.clip), str(movie), "--timeline", str(timeline),
                              "--crop", "0,150,1440,600", "--width", str(width)], check=True, capture_output=True, text=True)
        report = json.loads(out.stdout)
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "2", "-i", str(movie), "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "gray", "-"], check=True, capture_output=True).stdout
        w, h = report["width"], report["height"]
        self.assertEqual(len(frame), w * h)
        lit = [(i % w, i // w) for i, value in enumerate(frame) if value > 40]
        self.assertTrue(lit, "no caption drawn")
        return report, lit

    def assertInsideBand(self, report, lit, width):
        picture = min(width, 1440) * 600 // 1440 // 2 * 2  # never scaled up past the crop
        xs, ys = [x for x, _ in lit], [y for _, y in lit]
        self.assertGreater(min(xs), 0)
        self.assertLess(max(xs), report["width"] - 1, "caption reaches the right edge")
        self.assertGreater(min(ys), picture - 1, "caption covers the picture")
        self.assertLess(max(ys), report["height"] - 1, "caption reaches the bottom edge")

    def test_long_caption_wraps_inside_the_frame(self):
        for width in [1920, 640]:
            with self.subTest(width=width):
                report, lit = self.cut("long-%d" % width, PROMPT, width)
                self.assertInsideBand(report, lit, width)
                self.assertNotIn("shown", report["marks"][0])

    def test_very_long_caption_is_shortened(self):
        text = " ".join([PROMPT] * 4)
        report, lit = self.cut("very-long", text, 640)
        self.assertInsideBand(report, lit, 640)
        shown = report["marks"][0]["shown"]
        self.assertTrue(shown.endswith("…"))
        self.assertTrue(text.startswith(shown[:-1]))
        self.assertLess(len(shown), len(text) / 2)

    def test_long_address_keeps_what_fits(self):
        text = "Asking Lamp: open https://example.com/" + "segment/" * 25 + "index.html and tell me what it says"
        report, lit = self.cut("address", text, 640)
        self.assertInsideBand(report, lit, 640)
        shown = report["marks"][0]["shown"]
        self.assertTrue(text.startswith(shown[:-1]))
        self.assertGreater(len(shown), 150, "the address was dropped instead of cut")


if __name__ == "__main__":
    unittest.main()
