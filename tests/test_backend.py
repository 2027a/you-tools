import errno
import tempfile
import threading
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from yt_dlp.utils import DownloadError

from you_tools.backend import Backend, download_error
from you_tools.core import ToolError

PAYLOAD = b"native-asset\x00\xff\r\n" * 200
CAPTIONS = b"\xef\xbb\xbfWEBVTT\r\n\r\n00:00.000 --> 00:01.000\r\nExisting words.\r\n"


class Handler(BaseHTTPRequestHandler):
    hooks: ClassVar[dict[str, Callable[[], object]]] = {}

    def do_GET(self):
        if self.path == "/missing":
            self.send_error(404)
            return
        callback = self.hooks.get("request")
        if callback is not None:
            callback()
        body = CAPTIONS if self.path == "/captions" else PAYLOAD
        mime = "image/jpeg" if self.path.startswith("/image") else "application/octet-stream"
        if self.path == "/html":
            body, mime = b"<!doctype html>blocked", "text/html"
        elif self.path == "/empty":
            body = b""
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.hooks.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = Backend(timeout=2)
        self.addCleanup(self.backend.ydl.close)
        self.info = {"id": "jNQXAC9IVRw", "title": "Offline fixture", "duration": 1}
        self.fmt = {
            "url": self.base + "/file",
            "protocol": "http",
            "ext": "m4a",
            "format_id": "140",
            "vcodec": "none",
            "acodec": "aac",
        }

    def test_download_is_byte_identical_without_ffmpeg_and_percent_name_is_literal(self):
        output = Path(self.tmp.name) / "100% original.m4a"
        with patch(
            "yt_dlp.postprocessor.ffmpeg.FFmpegPostProcessor.run_ffmpeg",
            side_effect=AssertionError("No FFmpeg allowed"),
        ):
            path = self.backend.download(self.info, self.fmt, str(output))
        self.assertEqual(path, output)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertEqual(sorted(p.name for p in output.parent.iterdir()), [output.name])
        self.assertNotIn("color", self.backend.options)  # upstream must not mutate reusable options

    def test_original_caption_bytes_preserved_including_bom_and_line_endings(self):
        self.assertEqual(self.backend.transcript({"url": self.base + "/captions"}, {}), CAPTIONS)

    def test_existing_file_and_dangling_symlink_are_not_overwritten(self):
        output = Path(self.tmp.name) / "asset.m4a"
        output.write_bytes(b"preserve")
        with self.assertRaises(ToolError):
            self.backend.download(self.info, self.fmt, str(output))
        self.assertEqual(output.read_bytes(), b"preserve")
        link = Path(self.tmp.name) / "link.m4a"
        link.symlink_to("missing-target")
        with self.assertRaises(ToolError):
            self.backend.download(self.info, self.fmt, str(link))
        self.assertTrue(link.is_symlink())

    def test_concurrently_created_output_is_not_replaced(self):
        output = Path(self.tmp.name) / "asset.m4a"
        Handler.hooks["request"] = lambda: output.write_bytes(b"concurrent writer")
        with self.assertRaises(FileExistsError):
            self.backend.download(self.info, self.fmt, str(output))
        self.assertEqual(output.read_bytes(), b"concurrent writer")
        self.assertEqual(list(output.parent.iterdir()), [output])

    def test_local_output_error_is_not_reported_as_network_failure(self):
        blocker = Path(self.tmp.name) / "regular-file"
        blocker.write_text("preserve")
        with self.assertRaises(OSError):
            self.backend.download(self.info, self.fmt, str(blocker / "asset.m4a"))
        for number in (errno.EACCES, errno.EIO, errno.ENOSPC, errno.EMFILE):
            with self.subTest(errno=number):
                try:
                    raise OSError(number, "local write failed")
                except OSError as local:
                    assert local.__traceback__ is not None
                    wrapped = DownloadError(
                        "unable to write", exc_info=(type(local), local, local.__traceback__)
                    )
                    self.assertEqual(download_error(wrapped).code, 1)
        self.assertEqual(download_error(DownloadError("HTTP Error 403")).code, 4)

    def test_output_parent_created_and_no_generated_section_or_processor_forwarded(self):
        output = Path(self.tmp.name) / "new-directory" / "asset.m4a"
        info = self.info | {
            "section_start": 0,
            "section_end": 0.1,
            "requested_formats": [self.fmt, self.fmt],
            "__postprocessors": ["do not run"],
        }
        self.backend.download(info, self.fmt, str(output))
        self.assertEqual(output.read_bytes(), PAYLOAD)

    def test_refuses_manifest_format_before_transfer(self):
        with self.assertRaises(ToolError) as caught:
            self.backend.download(self.info, self.fmt | {"protocol": "m3u8_native"}, None)
        self.assertEqual(caught.exception.code, 3)

    def test_thumbnail_uses_ranked_inventory_before_unranked_fallback(self):
        high, low = self.base + "/image-high", self.base + "/image-low"
        info = {
            "thumbnail": low,
            "thumbnails": [{"url": low, "preference": -7}, {"url": high, "preference": -1}],
        }
        self.assertEqual(self.backend.thumbnail(info), (high, PAYLOAD))

    def test_thumbnail_falls_back_after_404(self):
        fallback = self.base + "/image-low"
        info = {
            "thumbnail": fallback,
            "thumbnails": [{"url": self.base + "/missing", "preference": 9}],
        }
        self.assertEqual(self.backend.thumbnail(info), (fallback, PAYLOAD))

    def test_empty_and_html_caption_responses_are_failures_not_transcripts(self):
        for path in ("/empty", "/html", "/missing"):
            with self.subTest(path=path), self.assertRaises(ToolError) as caught:
                self.backend.transcript({"url": self.base + path}, {})
            self.assertEqual(caught.exception.code, 4)

    def test_unavailable_extraction_is_an_error_not_placeholder_metadata(self):
        with patch.object(
            self.backend.ydl, "extract_info", side_effect=DownloadError("This video is unavailable")
        ):
            with self.assertRaises(ToolError) as caught:
                self.backend.extract("https://www.youtube.com/watch?v=BaW_jenozKc")
        self.assertEqual(caught.exception.code, 3)
        self.assertFalse(self.backend.ydl.params["ignore_no_formats_error"])

    def test_fetch_size_limit(self):
        with self.assertRaises(ToolError) as caught:
            self.backend.fetch(self.base + "/file", limit=10)
        self.assertEqual(caught.exception.code, 4)


if __name__ == "__main__":
    unittest.main()
