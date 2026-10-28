"""yt-dlp boundary: inspect videos and transfer existing files, never convert them."""

import errno
import os
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, RequestError
from yt_dlp.utils import DownloadError

from .core import ToolError, is_native_file


class Logger:
    def debug(self, message):
        pass

    def warning(self, message):
        print(f"you-tools: warning: {message}", file=sys.stderr)

    def error(self, message):
        # DownloadError carries the message; the CLI prints it once.
        pass


def extraction_error(exc: Exception) -> ToolError:
    message = str(exc).removeprefix("ERROR: ")
    unavailable = (
        "video unavailable",
        "video is unavailable",
        "private video",
        "removed by",
        "not available in your country",
        "members-only",
        "age-restricted",
        "no video formats found",
        "requested format is not available",
    )
    code = 3 if any(part in message.lower() for part in unavailable) else 4
    return ToolError(message, code)


def download_error(exc: Exception) -> ToolError:
    """yt-dlp may wrap local write failures in the same error as network failures."""
    local_errors = {
        errno.EACCES,
        errno.EPERM,
        errno.ENOSPC,
        errno.EDQUOT,
        errno.EROFS,
        errno.ENOTDIR,
        errno.EISDIR,
        errno.ENOENT,
        errno.EEXIST,
        errno.EFBIG,
        errno.EIO,
        errno.EMFILE,
        errno.ENFILE,
    }
    pending, seen = [exc], set()
    while pending:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, OSError) and cause.errno in local_errors:
            return ToolError(str(exc).removeprefix("ERROR: "), 1)
        pending.extend(
            item for item in (cause.__cause__, cause.__context__) if isinstance(item, Exception)
        )
        details = getattr(cause, "exc_info", None)
        if details and isinstance(details[1], Exception):
            pending.append(details[1])
    return extraction_error(exc)


def make_downloader(options: dict) -> Any:
    """Keep yt-dlp's dynamic dictionary API at this boundary (its stubs vary)."""
    return YoutubeDL(cast(Any, deepcopy(options)))


class Backend:
    def __init__(self, timeout: float = 20):
        runtimes = {
            name: {"path": path} for name in ("deno", "node") if (path := shutil.which(name))
        }
        self.options = {
            "noplaylist": True,
            "cachedir": False,
            "quiet": True,
            "no_color": True,
            "logtostderr": True,
            "logger": Logger(),
            "socket_timeout": timeout,
            "retries": 0,
            "extractor_retries": 0,
            "fragment_retries": 0,
            "skip_unavailable_fragments": False,
            "js_runtimes": runtimes,
            "remote_components": [],
            "extractor_args": {"youtube": {"skip": ["translated_subs", "hls", "dash"]}},
            "fixup": "never",
            "postprocessors": [],
            "external_downloader": "native",
            "overwrites": False,
            "continuedl": False,
            "noprogress": True,
            "format": "best/bestvideo/bestaudio",
            "ignore_no_formats_error": False,
        }
        self.ydl = make_downloader(self.options)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.ydl.close()

    def extract(self, url: str) -> dict:
        try:
            info = self.ydl.extract_info(url, download=False)
        except (DownloadError, RequestError) as exc:
            raise extraction_error(exc) from exc
        if not info or info.get("_type", "video") != "video" or not info.get("id"):
            raise ToolError("Expected one accessible YouTube video, not a collection.", 3)
        return info

    def fetch(self, url: str, headers: dict | None = None, limit: int = 32 * 1024 * 1024):
        if urlsplit(url).scheme not in {"https", "http"}:
            raise ToolError("Asset URL is not an HTTP(S) file.", 4)
        try:
            with self.ydl.urlopen(Request(url, headers=headers or {})) as response:
                body = response.read(limit + 1)
                mime = response.headers.get("Content-Type", "").split(";", 1)[0]
        except HTTPError as exc:
            exc.close()
            raise
        except RequestError as exc:
            raise ToolError(f"Could not retrieve asset: {exc}", 4) from exc
        if len(body) > limit:
            raise ToolError("Asset exceeds the safety size limit.", 4)
        if not body:
            raise ToolError("YouTube returned an empty asset (possibly blocked or expired).", 4)
        return body, mime

    def transcript(self, file: dict, info: dict) -> bytes:
        try:
            headers = file.get("http_headers") or info.get("http_headers")
            body, mime = self.fetch(file["url"], headers)
        except HTTPError as exc:
            raise ToolError(f"Caption download failed: HTTP {exc.status}.", 4) from exc
        if "html" in mime.lower() or body.lstrip().lower().startswith(b"<!doctype html"):
            raise ToolError("Caption endpoint returned HTML instead of captions.", 4)
        return body

    def thumbnail(self, info: dict) -> tuple[str, bytes]:
        candidates = list(info.get("thumbnails") or [])
        candidates.sort(
            key=lambda t: (
                t.get("preference") or 0,
                (t.get("width") or 0) * (t.get("height") or 0),
            ),
            reverse=True,
        )
        # The standalone URL has no rank: it is a fallback, never a new top candidate.
        if info.get("thumbnail"):
            candidates.append({"url": info["thumbnail"]})
        seen = set()
        for candidate in candidates:
            url = candidate.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                body, mime = self.fetch(url, limit=16 * 1024 * 1024)
            except HTTPError as exc:
                if exc.status in {404, 410}:
                    continue
                raise ToolError(f"Thumbnail download failed: HTTP {exc.status}.", 4) from exc
            if mime.startswith("image/"):
                return url, body
        raise ToolError("No accessible thumbnail is available.", 3)

    def download(self, info: dict, fmt: dict, output: str | None) -> Path:
        if not is_native_file(fmt):
            raise ToolError("Refusing a format that is not a single native HTTP(S) file.", 3)
        # Deliberately do not forward sections, requested_formats or extractor postprocessors.
        single = {
            key: info.get(key)
            for key in (
                "id",
                "title",
                "duration",
                "extractor",
                "extractor_key",
                "webpage_url",
                "http_headers",
            )
        }
        keys = (
            "format_id",
            "format",
            "url",
            "ext",
            "protocol",
            "vcodec",
            "acodec",
            "width",
            "height",
            "fps",
            "tbr",
            "abr",
            "filesize",
            "filesize_approx",
            "http_headers",
            "downloader_options",
            "language",
        )
        single.update({key: fmt[key] for key in keys if key in fmt})
        options = self.options | {
            "outtmpl": output.replace("%", "%%") if output else "%(title)s [%(id)s].%(ext)s",
            "ignore_no_formats_error": False,
        }
        with make_downloader(options) as naming:
            path = Path(naming.prepare_filename(single))
        if path.exists() or path.is_symlink():
            raise ToolError(f"Output already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Stage on the same filesystem, then publish with link(2): atomic and no-clobber.
        # yt-dlp's own final rename can replace a destination created during a transfer.
        with TemporaryDirectory(prefix=".you-tools-", dir=path.parent) as directory:
            staged = Path(directory) / f"asset.{fmt['ext']}"
            options["outtmpl"] = str(staged).replace("%", "%%")
            try:
                with make_downloader(options) as downloader:
                    downloader.process_info(single)
            except (DownloadError, RequestError) as exc:
                raise download_error(exc) from exc
            if not staged.is_file():
                raise ToolError("Download did not produce the expected native file.", 4)
            os.link(staged, path)
        return path
