import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from you_tools.cli import main
from you_tools.core import ToolError

ID = "jNQXAC9IVRw"
CAPTIONS = b"\xef\xbb\xbfWEBVTT\r\n\r\n00:00.000 --> 00:01.000\r\nExisting words.\r\n"
FORMATS = [
    {
        "format_id": "140",
        "ext": "m4a",
        "url": "https://example.test/audio",
        "protocol": "https",
        "vcodec": "none",
        "acodec": "aac",
        "abr": 128,
    },
    {
        "format_id": "18",
        "ext": "mp4",
        "url": "https://example.test/video",
        "protocol": "https",
        "vcodec": "avc1",
        "acodec": "aac",
        "height": 360,
    },
    {
        "format_id": "137",
        "ext": "mp4",
        "url": "https://example.test/silent",
        "protocol": "https",
        "vcodec": "avc1",
        "acodec": "none",
        "height": 1080,
    },
]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.backend = MagicMock()
        self.backend.__enter__.return_value = self.backend
        self.backend.extract.return_value = {
            "id": ID,
            "title": "Offline fixture",
            "language": "en",
            "formats": FORMATS,
            "subtitles": {"en": [{"url": "https://example.test/captions", "ext": "vtt"}]},
            "chapters": [{"start_time": 0, "end_time": 1, "title": "Existing chapter"}],
        }
        self.backend.transcript.return_value = CAPTIONS
        self.backend.thumbnail.return_value = ("https://example.test/image.jpg", b"original-image")
        self.backend.download.return_value = Path("downloaded.m4a")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def invoke(self, *args, stdin=None):
        body = io.BytesIO()
        stderr = io.StringIO()
        fake_stdout = SimpleNamespace(buffer=body)
        stdin = stdin if stdin is not None else io.StringIO()
        with (
            patch("you_tools.cli.Backend", return_value=self.backend),
            patch("sys.stdout", fake_stdout),
            patch("sys.stdin", stdin),
            redirect_stderr(stderr),
        ):
            try:
                code = main([ID, *args])
            except SystemExit as exc:
                code = exc.code
        return code, body.getvalue(), stderr.getvalue()

    def test_default_metadata_does_not_fetch_assets(self):
        code, body, _ = self.invoke("--json")
        self.assertEqual(code, 0)
        data = json.loads(body)
        self.assertEqual(data["id"], ID)
        self.backend.transcript.assert_not_called()
        self.backend.thumbnail.assert_not_called()
        self.backend.download.assert_not_called()

    def test_transcript_stdout_is_original_bytes_not_synthesized_text(self):
        code, body, diagnostics = self.invoke("--transcript", "--lang", "en")
        self.assertEqual(code, 0)
        self.assertEqual(body, CAPTIONS)
        self.assertIn("manual", diagnostics)
        self.assertNotIn(b"manual", body)

    def test_transcript_file_and_json_envelope_are_distinct_outputs(self):
        target = Path(self.tmp.name) / "captions.vtt"
        code, stdout, _ = self.invoke("--transcript", "-o", str(target))
        self.assertEqual((code, stdout, target.read_bytes()), (0, b"", CAPTIONS))
        code, body, _ = self.invoke("--transcript", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(body)["content"], CAPTIONS.decode("utf-8-sig"))

    def test_unavailable_captions_return_3_not_success_or_generated_text(self):
        self.backend.extract.return_value["subtitles"] = {}
        code, body, diagnostics = self.invoke("--transcript")
        self.assertEqual((code, body), (3, b""))
        self.assertIn("No existing transcript", diagnostics)
        self.backend.transcript.assert_not_called()

    def test_caption_download_blocked_is_error_4_not_absence(self):
        self.backend.transcript.side_effect = ToolError("blocked", 4)
        self.assertEqual(self.invoke("--transcript")[0], 4)
        self.assertEqual(self.invoke("--all", "--json")[0], 4)

    def test_all_keeps_missing_captions_explicit_and_never_downloads_media(self):
        self.backend.extract.return_value["subtitles"] = {}
        code, body, _ = self.invoke("--all", "--json")
        self.assertEqual(code, 0)
        data = json.loads(body)
        self.assertIsNone(data["transcript"])
        self.assertIn("transcript_unavailable", data)
        self.assertEqual(data["chapters"][0]["title"], "Existing chapter")
        self.backend.download.assert_not_called()

    def test_thumbnail_url_or_original_image_bytes(self):
        code, body, _ = self.invoke("--thumbnail")
        self.assertEqual((code, body), (0, b"https://example.test/image.jpg\n"))
        target = Path(self.tmp.name) / "renamed.webp"
        code, _, _ = self.invoke("--thumbnail", "-o", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(target.read_bytes(), b"original-image")

    def test_invalid_combinations_fail_before_network(self):
        combinations = [
            ("--quality", "18"),
            ("--lang", "en"),
            ("--format", "vtt"),
            ("--audio", "--choose-quality"),
            ("-o", "-"),
            ("--thumbnail", "--json", "-o", "image.jpg"),
            ("--audio", "--video"),
        ]
        for args in combinations:
            with self.subTest(args=args):
                self.assertEqual(self.invoke(*args)[0], 2)
        self.backend.extract.assert_not_called()

    def test_nonfinite_and_nonpositive_timeouts_rejected(self):
        for timeout in ("nan", "inf", "-1", "0"):
            with self.subTest(timeout=timeout):
                self.assertEqual(self.invoke("--timeout", timeout)[0], 2)
        self.backend.extract.assert_not_called()

    def test_existing_output_not_overwritten(self):
        target = Path(self.tmp.name) / "existing"
        target.write_bytes(b"preserve")
        self.assertEqual(self.invoke("-o", str(target))[0], 1)
        self.assertEqual(target.read_bytes(), b"preserve")
        self.backend.extract.assert_not_called()

    def test_media_quality_and_silent_warning(self):
        code, body, diagnostics = self.invoke("--video", "--quality", "137", "--json")
        self.assertEqual(code, 0)
        self.assertIn("silent", diagnostics)
        self.assertEqual(json.loads(body)["format"]["format_id"], "137")
        self.assertEqual(self.backend.download.call_args.args[1]["format_id"], "137")

    def test_audio_best_does_not_choose_combined_video(self):
        self.assertEqual(self.invoke("--audio")[0], 0)
        self.assertEqual(self.backend.download.call_args.args[1]["format_id"], "140")

    def test_interactive_selection_invalid_default_explicit_and_cancel(self):
        for text, expected in (("invalid\n999\n\n", "18"), ("1\n", "137")):
            stdin = io.StringIO(text)
            stdin.isatty = lambda: True
            code, _, diagnostics = self.invoke("--video", "--choose-quality", stdin=stdin)
            self.assertEqual(code, 0)
            self.assertIn("Available native files", diagnostics)
            self.assertEqual(self.backend.download.call_args.args[1]["format_id"], expected)
        for text in ("q\n", ""):
            stdin = io.StringIO(text)
            stdin.isatty = lambda: True
            self.assertEqual(self.invoke("--audio", "--choose-quality", stdin=stdin)[0], 130)

    def test_local_os_error_and_network_error_have_distinct_codes(self):
        self.backend.download.side_effect = PermissionError("not writable")
        self.assertEqual(self.invoke("--audio")[0], 1)
        self.backend.extract.side_effect = ToolError("network failed", 4)
        self.assertEqual(self.invoke()[0], 4)


if __name__ == "__main__":
    unittest.main()
