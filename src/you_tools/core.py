"""Small, network-free decisions about URLs and existing assets."""

import re
from urllib.parse import parse_qs, unquote, urlsplit

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "gaming.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}


class ToolError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def normalize_url(value: str, depth: int = 0) -> str:
    """Accept video links, not channels, playlists or arbitrary web addresses."""
    value = value.strip()
    if depth > 3:
        raise ToolError("Too many nested YouTube links.", 2)
    if VIDEO_ID.fullmatch(value):
        return f"https://www.youtube.com/watch?v={value}"
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ToolError("Invalid YouTube URL.", 2) from exc
    if (
        parsed.scheme not in {"https", "http"}
        or host not in HOSTS
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        raise ToolError("Expected a YouTube video URL (or an 11-character video ID).", 2)
    query = parse_qs(parsed.query)
    parts = unquote(parsed.path).strip("/").split("/")
    if parts[0] in {"attribution_link", "redirect", "oembed"}:
        nested = next((query[key][0] for key in ("u", "q", "url") if query.get(key)), "")
        if nested.startswith("/") and not nested.startswith("//"):
            nested = "https://www.youtube.com" + nested
        return normalize_url(nested, depth + 1)
    if host in {"youtu.be", "www.youtu.be"}:
        identifier = parts[0] if len(parts) == 1 else ""
    elif parts[0] == "watch":
        identifier = query.get("v", [""])[0]
    elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed", "v", "e"}:
        identifier = parts[1]
    elif len(parts) == 2 and parts[0] == "clip" and re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
        return f"https://www.youtube.com/clip/{parts[1]}"
    else:
        identifier = ""
    if not VIDEO_ID.fullmatch(identifier):
        raise ToolError("Expected one video; channels/playlists are unsupported.", 2)
    return f"https://www.youtube.com/watch?v={identifier}"


def caption_tracks(info: dict) -> list[dict]:
    """Keep native tracks only: never request YouTube's on-demand translations."""
    tracks = {}
    for category, source in (("subtitles", "manual"), ("automatic_captions", "automatic")):
        for key, entries in (info.get(category) or {}).items():
            language = key.removesuffix("-orig")
            formats = []
            for entry in entries:
                url = entry.get("url", "")
                if (
                    urlsplit(url).scheme not in {"http", "https"}
                    or "tlang" in parse_qs(urlsplit(url).query, keep_blank_values=True)
                    or not entry.get("ext")
                ):
                    continue
                if not any(f["ext"] == entry["ext"] for f in formats):
                    formats.append(entry)
            if not formats:
                continue
            identity = (language, source)
            original = key.endswith("-orig") or language == info.get("language")
            if identity in tracks:
                tracks[identity]["original"] |= original
                known = {f["ext"] for f in tracks[identity]["files"]}
                tracks[identity]["files"].extend(f for f in formats if f["ext"] not in known)
            else:
                tracks[identity] = {
                    "language": language,
                    "source": source,
                    "original": original,
                    "name": formats[0].get("name") or language,
                    "files": formats,
                }
    return sorted(tracks.values(), key=lambda t: (t["language"], t["source"] != "manual"))


def track_inventory(tracks: list[dict]) -> list[dict]:
    return [
        {k: v for k, v in track.items() if k != "files"}
        | {"formats": [f["ext"] for f in track["files"]]}
        for track in tracks
    ]


def select_caption(tracks: list[dict], language: str | None, extension: str | None) -> tuple:
    candidates = tracks
    if language:
        exact = [t for t in tracks if t["language"].lower() == language.lower()]
        # A base language may match regional variants, never an unrelated language.
        candidates = exact or [
            t for t in tracks if t["language"].lower().split("-")[0] == language.lower()
        ]
    if extension:
        candidates = [t for t in candidates if any(f["ext"] == extension for f in t["files"])]
    if not candidates:
        raise ToolError("No existing transcript matches the requested language/format.", 3)
    if not language:
        originals = [t for t in candidates if t["original"]]
        if originals:
            candidates = originals
        elif len({t["language"] for t in candidates}) > 1:
            raise ToolError("Original language unknown; use --lang and --list-transcripts.", 3)
    track = min(candidates, key=lambda t: (t["source"] != "manual", t["language"]))
    preferred = extension or next(
        (ext for ext in ("vtt", "json3", "ttml") if any(f["ext"] == ext for f in track["files"])),
        None,
    )
    file = next((f for f in track["files"] if f["ext"] == preferred), track["files"][0])
    return track, file


def is_native_file(fmt: dict) -> bool:
    return bool(
        fmt.get("protocol") in {"http", "https"}
        and urlsplit(fmt.get("url", "")).scheme in {"http", "https"}
        and not fmt.get("fragments")
        and not fmt.get("manifest_url")
        and not fmt.get("has_drm")
        and not fmt.get("requested_formats")
        and fmt.get("ext")
        and fmt.get("format_id")
    )


def native_formats(info: dict, kind: str | None = None) -> list[dict]:
    # Preserve yt-dlp's worst-to-best preference ordering.
    result = []
    for fmt in info.get("formats") or []:
        if not is_native_file(fmt):
            continue
        video = fmt.get("vcodec") not in {None, "none"}
        audio = fmt.get("acodec") not in {None, "none"}
        if not video and not audio:
            continue
        if kind == "video" and not video:
            continue
        if kind == "audio" and (not audio or video):
            continue
        result.append(fmt)
    return result


def format_inventory(formats: list[dict]) -> list[dict]:
    fields = (
        "format_id",
        "ext",
        "width",
        "height",
        "fps",
        "vcodec",
        "acodec",
        "abr",
        "tbr",
        "asr",
        "audio_channels",
        "language",
        "format_note",
        "filesize",
        "filesize_approx",
    )
    return [{k: fmt.get(k) for k in fields} for fmt in formats]


def select_media(info: dict, kind: str, quality: str | None) -> dict:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise ToolError("Live/upcoming/unprocessed streams are unsupported in V1.", 3)
    formats = native_formats(info, kind)
    if quality and quality != "best":
        formats = [f for f in formats if f["format_id"] == quality]
    elif kind == "video":
        # Prefer a playable, already combined file over a higher-resolution silent stream.
        combined = [f for f in formats if f.get("acodec") not in {None, "none"}]
        formats = combined or formats
    if not formats:
        raise ToolError(f"No matching native {kind} file; inspect --formats.", 3)
    return formats[-1]


def metadata(info: dict) -> dict:
    fields = (
        "id",
        "title",
        "description",
        "channel",
        "channel_id",
        "channel_url",
        "uploader",
        "duration",
        "upload_date",
        "release_timestamp",
        "timestamp",
        "view_count",
        "like_count",
        "tags",
        "categories",
        "language",
        "live_status",
        "availability",
    )
    return {"url": f"https://www.youtube.com/watch?v={info['id']}"} | {
        key: info.get(key) for key in fields
    }
