"""Offline replay tests; real ffmpeg checks use a tiny generated MP4."""

import asyncio
import contextlib
import functools
import io
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import audio
import main
from browser import get_replay_info
from config import get_site


MEDIA = "https://media.example.test/replay.mp4"


class AudioInputTests(unittest.TestCase):
    def test_report_selection_deduplication_and_error_opt_in(self):
        report = {"course_id": "42", "results": [
            {"sub_id": 1, "status": "no_subtitle"},
            {"sub_id": "1", "status": "no_subtitle"},
            {"sub_id": "2", "status": "error"},
            {"sub_id": "3", "status": "exported"},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text(json.dumps(report))
            self.assertEqual(audio.read_report(path), ("42", ["1"]))
            self.assertEqual(audio.read_report(path, True), ("42", ["1", "2"]))
            for invalid in ({}, [], {"course_id": "42", "results": [None]},
                            {"course_id": "42", "results": [{"sub_id": "../1", "status": "no_subtitle"}]}):
                path.write_text(json.dumps(invalid))
                with self.subTest(invalid=invalid), self.assertRaises(audio.ReplayError):
                    audio.read_report(path)

    def test_input_site_inference_and_conflicts(self):
        parser = main.build_parser()
        url = "https://video-jw-443.webvpn.scut.edu.cn/livingroom?course_id=42&sub_id=1"
        self.assertEqual(main.audio_request(parser.parse_args(["audio", url])),
                         ("external", "42", ["1"]))
        self.assertEqual(main.audio_request(parser.parse_args(
            ["audio", "--course-id", "42", "--sub-id", "1", "1", "2"])),
            ("internal", "42", ["1", "2"]))
        for extra in (["--site", "internal"], ["--course-id", "99"]):
            with self.subTest(extra=extra), self.assertRaises(audio.ReplayError):
                main.audio_request(parser.parse_args(["audio", url, *extra]))

    def test_media_preference_and_malformed_fallbacks(self):
        data = {"content": {"save_playback": {"contents": MEDIA},
                            "playback": {"url": MEDIA + "?backup"}},
                "video_list": [{"type": 4, "preview_url": MEDIA + "?student"},
                               {"type": "3", "preview_url": MEDIA + "?teacher"}]}
        self.assertEqual(audio.extract_media(data), ("content.save_playback.contents", MEDIA))
        del data["content"]["save_playback"]
        self.assertEqual(audio.extract_media(data)[0], "content.playback.url")
        data["content"] = "unexpected"
        self.assertEqual(audio.extract_media(data)[1], MEDIA + "?teacher")
        data["video_list"] = {"teacher": {"type": 3, "preview_url": "file:///tmp/a.mp4"}}
        data["playurl"] = {"invalid": None, "hd": MEDIA}
        self.assertEqual(audio.extract_media(data), ("playurl.hd", MEDIA))
        for invalid in ({}, {"playurl": {"hd": "file:///tmp/a.mp4"}},
                        {"content": {"playback": {"url": MEDIA + ".m3u8"}}}):
            with self.subTest(invalid=invalid), self.assertRaises(audio.ReplayError):
                audio.extract_media(invalid)

    def test_output_paths_contain_untrusted_titles_and_reject_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output, manifest = audio.output_paths(root, "42", "1", "../../evil:\x00")
            self.assertTrue(output.is_relative_to(root / "42"))
            self.assertEqual(manifest, root / "42/媒体地址.json")
            self.assertEqual(audio.output_paths(root, "42", "2", "2026-10-16第3-4节")[0].name,
                             "2026-10-16_2.m4a")
            with self.assertRaises(ValueError):
                audio.output_paths(root, "../42", "1", "title")
            outside = root.parent
            (root / "42").symlink_to(outside)
            with self.assertRaises(audio.ReplayError):
                audio.output_paths(root, "42", "1", "title")

    def test_manifest_updates_preserve_other_lessons_and_download_time(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "媒体地址.json"
            first = {"course_id": "42", "sub_id": "1", "title": "A", "downloaded_at": "original"}
            audio.update_manifest(path, first)
            audio.update_manifest(path, {"course_id": "42", "sub_id": "2", "title": "B"})
            audio.update_manifest(path, {"course_id": "42", "sub_id": "1", "checked_at": "later"})
            data = json.loads(path.read_text())
            self.assertEqual(len(data["items"]), 2)
            self.assertEqual(data["items"][0]["downloaded_at"], "original")
            path.write_text("broken JSON")
            with self.assertRaises(audio.ReplayError):
                audio.update_manifest(path, first)
            self.assertEqual(path.read_text(), "broken JSON")


class ReplayBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_origin_api_and_cookies_for_both_sites(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                for site_key in ("internal", "external"):
                    site = get_site(site_key)
                    context = await browser.new_context()
                    await context.add_cookies([{"name": "test_session", "value": "offline",
                                                "url": site.origin}])
                    requests = []

                    async def route(request_route):
                        request = request_route.request
                        if "/courseapi/" in request.url:
                            requests.append((request.url, await request.all_headers()))
                            await request_route.fulfill(json={"code": 0, "data": {"sub_title": "test"}})
                        else:
                            await request_route.fulfill(body="<html>offline</html>", content_type="text/html")

                    await context.route("**/*", route)
                    result = await get_replay_info(context, site, "42", "1")
                    self.assertEqual(result["sub_title"], "test")
                    self.assertEqual(requests[0][0],
                                     f"{site.origin}/courseapi/v3/portal-home-setting/get-sub-info?course_id=42&sub_id=1")
                    self.assertIn("test_session=offline", requests[0][1]["cookie"])
                    self.assertEqual(context.pages, [])
                    await context.close()
            finally:
                await browser.close()

    async def test_auth_and_api_errors_close_page(self):
        for result in ({"status": 403, "body": None}, {"status": 503, "body": {}},
                       {"status": 200, "body": None},
                       {"status": 200, "body": {"code": 10002, "msg": "missing"}},
                       {"status": 200, "body": {"code": 0, "data": []}}):
            page = AsyncMock()
            page.url = get_site("internal").origin
            page.evaluate.return_value = result
            context = AsyncMock()
            context.new_page.return_value = page
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                await get_replay_info(context, get_site("internal"), "42", "1")
            page.close.assert_awaited_once()


class AudioCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_report_succeeds_without_login_or_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "report.json"
            report.write_text(json.dumps({"course_id": "42", "results": [
                {"sub_id": "1", "status": "exported"}
            ]}))
            args = main.build_parser().parse_args(["audio", "--from-report", str(report)])
            with (patch.object(main, "create_browser", AsyncMock()) as create,
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertTrue(await main.cmd_audio(args))
                create.assert_not_awaited()

    async def test_dry_run_does_not_need_ffmpeg_or_write_files(self):
        with tempfile.TemporaryDirectory() as temp:
            out_dir = Path(temp) / "absent"
            args = main.build_parser().parse_args(["audio", "--course-id", "42", "--sub-id", "1",
                                                  "--dry-run", "--out-dir", str(out_dir)])
            with (patch.object(main, "is_logged_in", return_value=True),
                  patch.object(main, "create_browser", AsyncMock(return_value=(None, None, None))),
                  patch.object(main, "close_browser", AsyncMock()) as close,
                  patch.object(main, "get_replay_info", AsyncMock(return_value={"playurl": {"hd": MEDIA}})),
                  patch.object(main, "require_binary", side_effect=AssertionError("dry-run requires no binary")),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertTrue(await main.cmd_audio(args))
                close.assert_awaited_once()
            self.assertFalse(out_dir.exists())

    async def test_partial_failure_continues_or_stops_and_is_unsuccessful(self):
        for stop in (False, True):
            args = main.build_parser().parse_args(["audio", "--course-id", "42", "--sub-id", "1", "2",
                                                  "--dry-run", *(["--stop-on-error"] if stop else [])])
            with (patch.object(main, "is_logged_in", return_value=True),
                  patch.object(main, "create_browser", AsyncMock(return_value=(None, None, None))),
                  patch.object(main, "close_browser", AsyncMock()) as close,
                  patch.object(main, "get_replay_info", AsyncMock(side_effect=[
                      RuntimeError("first failed"), {"playurl": {"hd": MEDIA}}
                  ])) as fetch, contextlib.redirect_stdout(io.StringIO())):
                self.assertFalse(await main.cmd_audio(args))
                self.assertEqual(fetch.await_count, 1 if stop else 2)
                close.assert_awaited_once()

    async def test_manifest_contains_metadata_after_download(self):
        with tempfile.TemporaryDirectory() as temp:
            args = main.build_parser().parse_args(["audio", "--course-id", "42", "--sub-id", "1",
                                                  "--out-dir", temp])
            with (patch.object(main, "is_logged_in", return_value=True),
                  patch.object(main, "create_browser", AsyncMock(return_value=(None, None, None))),
                  patch.object(main, "close_browser", AsyncMock()),
                  patch.object(main, "get_replay_info", AsyncMock(return_value={
                      "sub_title": "2026-10-16第3-4节", "content": {"save_playback": {"contents": MEDIA}}
                  })), patch.object(main, "require_binary", side_effect=lambda name: name),
                  patch.object(main, "download_audio", AsyncMock(return_value=(
                      "downloaded", {"duration_seconds": 1.0, "size_bytes": 100}
                  ))), contextlib.redirect_stdout(io.StringIO())):
                self.assertTrue(await main.cmd_audio(args))
            item = json.loads((Path(temp) / "42/媒体地址.json").read_text())["items"][0]
            self.assertEqual(item["source_field"], "content.save_playback.contents")
            self.assertEqual(item["duration_seconds"], 1.0)
            self.assertIn("downloaded_at", item)

    def test_cli_failure_has_nonzero_exit(self):
        with (patch("sys.argv", ["main.py", "audio", "--course-id", "42", "--sub-id", "1"]),
              patch.object(main, "cmd_audio", AsyncMock(return_value=False)),
              self.assertRaises(SystemExit) as raised):
            main.main()
        self.assertEqual(raised.exception.code, 1)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class AudioExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_terminates_ffmpeg_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "audio.m4a"
            process = AsyncMock()
            process.returncode = None
            process.communicate.side_effect = asyncio.CancelledError
            process.kill = unittest.mock.Mock()
            with (patch("audio.asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
                  self.assertRaises(asyncio.CancelledError)):
                await audio.download_audio("ffmpeg", "ffprobe", MEDIA, output)
            process.kill.assert_called_once()
            process.wait.assert_awaited_once()
            self.assertEqual(list(Path(temp).iterdir()), [])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
    async def test_http_mp4_extraction_skip_and_failed_force_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "replay.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=32x32:r=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
                "-c:v", "mpeg4", "-c:a", "aac", str(source),
            ], check=True, capture_output=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=temp))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/replay.mp4"
                output = root / "out/audio.m4a"
                status, metadata = await audio.download_audio("ffmpeg", "ffprobe", url, output)
                self.assertEqual(status, "downloaded")
                self.assertGreater(metadata["duration_seconds"], 0.9)
                streams = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)
                ]))["streams"]
                self.assertEqual([(stream["codec_type"], stream["codec_name"]) for stream in streams],
                                 [("audio", "aac")])
                original = output.read_bytes()
                status, _ = await audio.download_audio("ffmpeg", "ffprobe", url + "missing", output)
                self.assertEqual(status, "exists")
                with self.assertRaises(audio.ReplayError):
                    await audio.download_audio("ffmpeg", "ffprobe", url + "missing", output, force=True)
                self.assertEqual(output.read_bytes(), original)
                self.assertEqual(list(output.parent.glob("*.part.m4a")), [])
                output.write_bytes(b"not an audio file")
                with self.assertRaises(audio.ReplayError):
                    await audio.download_audio("ffmpeg", "ffprobe", url, output)
                status, _ = await audio.download_audio("ffmpeg", "ffprobe", url, output, force=True)
                self.assertEqual(status, "downloaded")
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
