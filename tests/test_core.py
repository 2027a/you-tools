import unittest

from you_tools.core import (
    ToolError,
    caption_tracks,
    native_formats,
    normalize_url,
    select_caption,
    select_media,
    track_inventory,
)

ID = "jNQXAC9IVRw"
CANONICAL = f"https://www.youtube.com/watch?v={ID}"


def media(identifier="18", video="avc1", audio="aac", **extra):
    return {
        "format_id": identifier,
        "ext": "mp4",
        "url": "https://example.test/file",
        "protocol": "https",
        "vcodec": video,
        "acodec": audio,
    } | extra


def caption(language="en", extension="vtt", **extra):
    return {
        "url": f"https://www.youtube.com/api/timedtext?lang={language}",
        "ext": extension,
    } | extra


class URLTests(unittest.TestCase):
    def test_common_forms_normalize_to_one_video(self):
        variants = [
            ID,
            CANONICAL,
            f"http://youtube.com/watch?v={ID}&t=1&list=ignored",
            f"youtu.be/{ID}?si=tracking",
            f"//youtu.be/{ID}",
            f"https://m.youtube.com/watch?v={ID}",
            f"https://music.youtube.com/watch?v={ID}&list=ignored",
            f"https://www.youtube-nocookie.com/embed/{ID}?start=42",
            f"https://www.youtube.com/shorts/{ID}",
            f"https://www.youtube.com/live/{ID}",
            f"https://youtube.com/v/{ID}",
            f"https://youtube.com/e/{ID}",
            f"https://youtube.com/attribution_link?u=%2Fwatch%3Fv%3D{ID}",
            f"https://youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2F{ID}",
        ]
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(normalize_url(value), CANONICAL)

    def test_rejects_non_video_and_non_youtube_inputs(self):
        invalid = [
            "",
            "https://youtube.com/playlist?list=abc",
            "https://youtube.com/@channel",
            "https://youtube.com/watch",
            "https://evil.test/watch?v=" + ID,
            "https://youtube.com.evil.test/watch?v=" + ID,
            "https://youtube.com@evil.test/watch?v=" + ID,
            "file:///etc/passwd",
            "ftp://youtube.com/watch?v=" + ID,
            "https://youtube.com:bad/watch?v=" + ID,
            "https://youtube.com/redirect?q=https://evil.test/",
            "https://youtu.be/" + ID + "/extra",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ToolError) as caught:
                normalize_url(value)
            self.assertEqual(caught.exception.code, 2)

    def test_clip_is_passed_to_extractor_without_trimming(self):
        url = "https://www.youtube.com/clip/Ugkx-example?si=tracking"
        self.assertEqual(normalize_url(url), "https://www.youtube.com/clip/Ugkx-example")

    def test_nested_links_are_bounded(self):
        with self.assertRaises(ToolError):
            normalize_url(CANONICAL, depth=4)


class CaptionTests(unittest.TestCase):
    def test_manual_preferred_within_original_language(self):
        tracks = caption_tracks(
            {
                "language": "fr",
                "subtitles": {"fr": [caption("fr")]},
                "automatic_captions": {"fr-orig": [caption("fr")]},
            }
        )
        track, file = select_caption(tracks, None, None)
        self.assertEqual((track["language"], track["source"], file["ext"]), ("fr", "manual", "vtt"))

    def test_translations_excluded_even_empty_tlang_and_aliases_deduplicated(self):
        tracks = caption_tracks(
            {
                "automatic_captions": {
                    "en-orig": [caption()],
                    "en": [caption()],
                    "fr": [caption("en", url="https://youtube.com/api/timedtext?lang=en&tlang=fr")],
                    "de": [caption("en", url="https://youtube.com/api/timedtext?lang=en&tlang=")],
                }
            }
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["language"], "en")
        self.assertTrue(tracks[0]["original"])
        self.assertEqual(track_inventory(tracks)[0]["formats"], ["vtt"])

    def test_unknown_original_requires_explicit_language(self):
        tracks = caption_tracks({"subtitles": {"en": [caption()], "de": [caption("de")]}})
        with self.assertRaises(ToolError) as caught:
            select_caption(tracks, None, None)
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(select_caption(tracks, "de", None)[0]["language"], "de")

    def test_language_does_not_fall_back_to_unrelated_track(self):
        tracks = caption_tracks({"subtitles": {"en": [caption()]}})
        with self.assertRaises(ToolError):
            select_caption(tracks, "fr", None)

    def test_base_language_can_match_a_region_but_not_reverse(self):
        tracks = caption_tracks({"subtitles": {"fr-CA": [caption("fr-CA")]}})
        self.assertEqual(select_caption(tracks, "fr", None)[0]["language"], "fr-CA")
        with self.assertRaises(ToolError):
            select_caption(tracks, "fr-FR", None)

    def test_no_srt_conversion_and_no_synthetic_plain_text(self):
        tracks = caption_tracks({"subtitles": {"en": [caption()]}})
        for ext in ("srt", "txt"):
            with self.subTest(ext=ext), self.assertRaises(ToolError):
                select_caption(tracks, "en", ext)

    def test_requested_native_caption_format_is_selected(self):
        tracks = caption_tracks({"subtitles": {"en": [caption(), caption(extension="srt")]}})
        self.assertEqual(select_caption(tracks, "en", "srt")[1]["ext"], "srt")

    def test_single_language_can_be_used_when_original_unknown(self):
        tracks = caption_tracks({"subtitles": {"en": [caption()]}})
        self.assertFalse(select_caption(tracks, None, None)[0]["original"])

    def test_empty_inventory_is_unavailable(self):
        with self.assertRaises(ToolError) as caught:
            select_caption([], None, None)
        self.assertEqual(caught.exception.code, 3)


class MediaTests(unittest.TestCase):
    def test_native_filter_rejects_manifests_fragments_drm_and_unknown_codecs(self):
        valid = media()
        formats = [
            valid,
            media("hls", protocol="m3u8_native"),
            media("fragment", fragments=[{"url": "https://example.test/piece"}]),
            media("manifest", manifest_url="https://example.test/manifest"),
            media("drm", has_drm=True),
            media("file", url="file:///tmp/video"),
            media("none", video="none", audio="none"),
            media("combo", requested_formats=[valid]),
        ]
        self.assertEqual(native_formats({"formats": formats}), [valid])

    def test_video_prefers_existing_combined_file_not_silent_higher_resolution(self):
        combined = media(height=360)
        silent = media("137", audio="none", height=1080)
        info = {"formats": [combined, silent]}
        self.assertEqual(select_media(info, "video", None), combined)
        self.assertEqual(select_media(info, "video", "137"), silent)

    def test_audio_requires_audio_only_file_never_extracts_from_video(self):
        audio = media("140", video="none")
        info = {"formats": [audio, media()]}
        self.assertEqual(select_media(info, "audio", None), audio)
        with self.assertRaises(ToolError):
            select_media({"formats": [media()]}, "audio", None)

    def test_quality_is_an_exact_id_not_a_selector_expression(self):
        info = {"formats": [media()]}
        for quality in ("18+140", "bestvideo+bestaudio", "18/140", "missing"):
            with self.subTest(quality=quality), self.assertRaises(ToolError):
                select_media(info, "video", quality)

    def test_active_and_unprocessed_live_rejected_but_completed_vod_allowed(self):
        for status in ("is_live", "is_upcoming", "post_live"):
            with self.subTest(status=status), self.assertRaises(ToolError):
                select_media({"live_status": status, "formats": [media()]}, "video", None)
        self.assertEqual(
            select_media({"live_status": "was_live", "formats": [media()]}, "video", None)[
                "format_id"
            ],
            "18",
        )


if __name__ == "__main__":
    unittest.main()
