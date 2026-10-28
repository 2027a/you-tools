"""Command-line interface. Diagnostics/prompts go to stderr, results to stdout."""

import argparse
import json
import math
import sys
from pathlib import Path

from .backend import Backend
from .core import (
    ToolError,
    caption_tracks,
    format_inventory,
    metadata,
    native_formats,
    normalize_url,
    select_caption,
    select_media,
    track_inventory,
)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Retrieve existing YouTube assets. No generation, conversion or merging.",
        epilog="Quote URLs containing &. Each invocation handles exactly one video.",
    )
    cli.add_argument("url", help="YouTube video URL (watch, share, shorts, live, embed...) or ID")
    action = cli.add_mutually_exclusive_group()
    for name, help_text in (
        ("info", "show metadata (default)"),
        ("thumbnail", "print a verified thumbnail URL; -o saves the original image"),
        ("list-transcripts", "list existing caption languages, sources and native formats"),
        ("transcript", "retrieve an existing caption file unchanged (prefer VTT)"),
        ("chapters", "show existing chapters"),
        ("all", "show metadata, thumbnail, chapters, formats and captions; no audio/video"),
        ("formats", "list downloadable native media formats and quality IDs"),
        ("video", "download one native video file (prefer an already combined audio/video file)"),
        ("audio", "download one native audio-only file; no extraction or transcoding"),
    ):
        action.add_argument(
            f"--{name}", dest="action", action="store_const", const=name, help=help_text
        )
    cli.set_defaults(action="info")
    cli.add_argument("--json", action="store_true", help="structured result")
    cli.add_argument("-o", "--output", help="save to this filename; never overwrite; no conversion")
    cli.add_argument("--lang", help="caption language, e.g. fr or en-US; no unrelated fallback")
    cli.add_argument("--format", dest="caption_format", help="native caption format, e.g. vtt")
    quality = cli.add_mutually_exclusive_group()
    quality.add_argument("--quality", help="media format ID from --formats, or best (default)")
    quality.add_argument(
        "--choose-quality",
        action="store_true",
        help="choose an available audio/video format interactively",
    )
    cli.add_argument(
        "--timeout", type=float, default=20, help="request timeout, seconds (default 20)"
    )
    return cli


def validate(cli, args):
    media = args.action in {"video", "audio"}
    if (args.quality or args.choose_quality) and not media:
        cli.error("--quality/--choose-quality require --video or --audio")
    if (args.lang or args.caption_format) and args.action not in {"transcript", "all"}:
        cli.error("--lang/--format require --transcript or --all")
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        cli.error("--timeout must be a positive, finite number")
    if args.choose_quality and not sys.stdin.isatty():
        cli.error("--choose-quality needs an interactive terminal; use --quality ID in scripts")
    if args.action == "thumbnail" and args.output and args.json:
        cli.error("Use --thumbnail -o for the image, or --thumbnail --json for its URL")
    if args.output == "-":
        cli.error("-o expects a filename, not '-'; omit -o for textual stdout")
    if args.output:
        path = Path(args.output).expanduser()
        args.output = str(path)
        if path.exists() or path.is_symlink():
            raise ToolError(f"Output already exists: {path}")


def format_label(fmt: dict) -> str:
    video = fmt.get("vcodec") not in {None, "none"}
    audio = fmt.get("acodec") not in {None, "none"}
    kind = "video+audio" if video and audio else "video only" if video else "audio only"
    parts = [str(fmt["format_id"]), fmt["ext"], kind]
    if fmt.get("height"):
        parts.append(f"{fmt['height']}p")
    if fmt.get("fps"):
        parts.append(f"{fmt['fps']:g}fps")
    bitrate = fmt.get("abr") if not video else fmt.get("tbr")
    if bitrate:
        parts.append(f"{bitrate:g}kb/s")
    if fmt.get("language"):
        parts.append(fmt["language"])
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        approximate = "~" if not fmt.get("filesize") else ""
        parts.append(f"{approximate}{size / 1024 / 1024:.1f}MiB")
    return " | ".join(parts)


def choose_media(info: dict, kind: str) -> dict:
    default = select_media(info, kind, None)
    formats = list(reversed(native_formats(info, kind)))
    print("Available native files (no conversion or merging):", file=sys.stderr)
    for index, fmt in enumerate(formats, 1):
        mark = " [default]" if fmt["format_id"] == default["format_id"] else ""
        print(f"  {index}. {format_label(fmt)}{mark}", file=sys.stderr)
    while True:
        print(
            "Choose a number, Enter for default, or q to cancel: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        line = sys.stdin.readline()
        if not line or line.strip().lower() == "q":
            raise ToolError("Cancelled.", 130)
        if not line.strip():
            return default
        try:
            index = int(line.strip())
        except ValueError:
            index = 0
        if 1 <= index <= len(formats):
            return formats[index - 1]
        print("Invalid choice.", file=sys.stderr)


def caption_result(backend: Backend, info: dict, args) -> tuple[dict, bytes]:
    track, file = select_caption(caption_tracks(info), args.lang, args.caption_format)
    body = backend.transcript(file, info)
    result = {k: track[k] for k in ("language", "source", "original", "name")}
    result.update({"format": file["ext"], "content": body.decode("utf-8-sig")})
    return result, body


def text_result(action: str, data) -> str:
    if action == "thumbnail":
        return data["url"] + "\n"
    if action == "formats":
        return "\n".join(format_label(f) for f in reversed(data)) + "\n"
    if action == "list-transcripts":
        if not data:
            return "No existing transcripts.\n"
        return (
            "\n".join(
                f"{t['language']} | {t['source']} | {'original' if t['original'] else 'unknown'}"
                f" | {', '.join(t['formats'])}"
                for t in data
            )
            + "\n"
        )
    if action == "chapters":
        if not data:
            return "No existing chapters.\n"
        return "\n".join(f"{c['start_time']:g}s | {c.get('title', '')}" for c in data) + "\n"
    if action in {"audio", "video"}:
        return data["path"] + "\n"
    if action == "info":
        rows = (f"{key}: {value}" for key, value in data.items() if value is not None)
        return "\n".join(rows) + "\n"
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def emit(data, action: str, args, original: bytes | None = None):
    if original is not None and not args.json:
        body = original
    elif args.json:
        body = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    else:
        body = text_result(action, data).encode()
    if args.output and action not in {"video", "audio"}:
        with Path(args.output).open("xb") as target:
            target.write(body)
    else:
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()


def run(args):
    url = normalize_url(args.url)
    with Backend(args.timeout) as backend:
        info = backend.extract(url)
        original = None
        if args.action == "info":
            data = metadata(info)
        elif args.action == "list-transcripts":
            data = track_inventory(caption_tracks(info))
        elif args.action == "transcript":
            data, original = caption_result(backend, info, args)
            print(
                f"you-tools: captions: {data['language']} ({data['source']}), {data['format']}",
                file=sys.stderr,
            )
        elif args.action == "thumbnail":
            thumbnail_url, image = backend.thumbnail(info)
            data = {"url": thumbnail_url}
            if args.output:
                original = image
        elif args.action == "chapters":
            data = info.get("chapters") or []
        elif args.action == "formats":
            data = format_inventory(native_formats(info))
        elif args.action in {"video", "audio"}:
            fmt = (
                choose_media(info, args.action)
                if args.choose_quality
                else select_media(info, args.action, args.quality)
            )
            print(f"you-tools: downloading {format_label(fmt)}", file=sys.stderr)
            if args.action == "video" and fmt.get("acodec") in {None, "none"}:
                print(
                    "you-tools: this is a silent video stream; use --audio separately for sound.",
                    file=sys.stderr,
                )
            path = backend.download(info, fmt, args.output)
            data = {"path": str(path), "format": format_inventory([fmt])[0]}
        else:
            data = {
                "schema_version": 1,
                "info": metadata(info),
                "chapters": info.get("chapters") or [],
                "transcripts": track_inventory(caption_tracks(info)),
                "formats": format_inventory(native_formats(info)),
                "transcript": None,
                "thumbnail": None,
            }
            try:
                data["thumbnail"] = {"url": backend.thumbnail(info)[0]}
            except ToolError as exc:
                if exc.code != 3:
                    raise
            try:
                data["transcript"] = caption_result(backend, info, args)[0]
            except ToolError as exc:
                if exc.code != 3:
                    raise
                data["transcript_unavailable"] = str(exc)
        emit(data, args.action, args, original)


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        validate(cli, args)
        run(args)
    except ToolError as exc:
        print(f"you-tools: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, UnicodeError) as exc:
        print(f"you-tools: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("you-tools: cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
