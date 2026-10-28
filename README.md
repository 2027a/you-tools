# you-tools

A small CLI to retrieve existing YouTube metadata, thumbnails, captions, audio
and video. One video per invocation, no API key or account required.

**V1 only retrieves what is available.** It does not generate transcripts,
translate, transcode, remux, merge streams, extract audio from video, or convert
caption files. Downloaded assets keep their original bytes; filenames may change.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Node.js 22+ or Deno
2+ for YouTube's JavaScript challenges. FFmpeg is not required or used.
Dependencies (including yt-dlp's challenge solver) are locked in `uv.lock`;
no solver code is fetched at runtime.

```sh
git clone ssh://git@git.2027a.net:222/2027a/you-tools.git ~/Projets/you-tools
cd ~/Projets/you-tools
uv sync --locked
chmod +x bin/you-tools
mkdir -p ~/.local/bin
ln -s "$PWD/bin/you-tools" ~/.local/bin/you-tools
```

The launcher works from any directory, including through a symlink. It uses the
project's virtual environment; it never installs or updates packages on launch.

## Usage

Always quote URLs containing `&`. Standard watch, youtu.be, Shorts, live, embed,
mobile/music, attribution and clip links are accepted, as are video IDs.
Tracking/time parameters are ignored. A video linked from a playlist still means
that one video. Clip links retrieve the underlying **whole video**, not a cut.
Channel links and playlist-only URLs are rejected.

```sh
you-tools 'https://youtu.be/jNQXAC9IVRw'                  # metadata
you-tools 'URL' --info --json
you-tools 'URL' --thumbnail                            # verified image URL
you-tools 'URL' --thumbnail -o cover.jpg                # original image, no conversion
you-tools 'URL' --list-transcripts
you-tools 'URL' --transcript --lang en                  # native captions, prefer VTT
you-tools 'URL' --transcript --lang en --format vtt -o captions.vtt
you-tools 'URL' --chapters
you-tools 'URL' --all --json                            # no audio/video/image files

you-tools 'URL' --formats                              # available native format IDs
you-tools 'URL' --video                                # prefer video with sound
you-tools 'URL' --audio                                # best native audio-only file
you-tools 'URL' --video --choose-quality                # interactive menu
you-tools 'URL' --audio --choose-quality
you-tools 'URL' --video --quality 18 -o video.mp4        # ID must be available
```

Choose one action per invocation. `--info` is the default. `--json` provides
structured results; diagnostics and interactive prompts stay on stderr.
`-o FILE` saves textual results or an asset, and never overwrites existing files.
For media, the downloaded path is printed; without `-o`, filenames use the title
and video ID in the current directory. A different filename extension **does not
convert** the file. Interactive selection requires a terminal; scripts should
use `--quality ID` or the default `best`.

### What is available?

- **Metadata:** title, description, channel, canonical URL, duration, publication,
  views, likes, tags, categories, language and live status, when exposed.
- **Thumbnails:** highest-ranked accessible image, with unavailable candidates
  skipped. `--thumbnail` checks the image even when only returning its URL.
- **Captions:** existing manual or YouTube-generated tracks, clearly labelled.
  Prefer manual captions within the selected language. `--lang fr` never silently
  falls back to English; a base language can match a regional variant. Without
  `--lang`, prefer the identifiable original language; ask for an explicit
  language if several exist and the original is unknown. Translated-on-demand
  tracks are excluded. Only formats actually offered can be downloaded: requesting
  SRT when only VTT exists fails rather than converting. `--transcript --json`
  returns track information and the unconverted caption document in `content`.
- **Chapters:** existing titles and timestamps only.
- **Audio/video:** single direct HTTP(S) files only, with quality, codecs,
  language and size shown when known. Video defaults to the best already-combined
  file; higher-resolution video-only streams are labelled silent. Fetch their
  audio separately with `--audio`; this tool never joins them. Audio requires an
  existing audio-only format. Format IDs vary by video and can expire.
- **All:** metadata, verified thumbnail URL, chapters, format/track inventories
  and one caption document if available. Missing captions are `null` with a reason;
  network/download failures remain errors. No audio/video is downloaded; the
  thumbnail is fetched only to verify its URL.

## Limits and maintenance

No comments, batches, playlists, summaries, generation or conversion. Media
requiring manifests/fragments, DRM, active/upcoming/unprocessed live streams,
or authentication is outside V1. Completed live recordings can work once direct
files are available. Browser cookies and personal profiles are never read.

YouTube can change, rate-limit, restrict or block retrieval, including caption
endpoints even when a track is listed. Metadata extraction succeeding does not
prove downloads will work. `--timeout` sets the per-request timeout (20 seconds
by default); requests are not retried indefinitely. Only retrieve content you
are entitled to download.

Exit codes: **0** success, **1** local/output error, **2** usage/invalid URL,
**3** unavailable asset or unsupported format, **4** network/extraction/blocking
error, **130** cancelled.

```sh
uv run --locked python -m unittest discover -s tests -v  # offline fixtures/local HTTP only
uv run --locked ruff check .
uv lock --upgrade && uv sync --locked                  # explicit dependency update
```

The Forgejo Actions workflow runs the same offline checks on Python 3.11 and
3.14 when an `ubuntu-latest` runner is available.

## License

Original code and documentation: [MIT](LICENSE). Dependencies are not vendored
and retain their own licenses, including yt-dlp and its bundled components.
Downloaded YouTube content is not covered by this project's license.
