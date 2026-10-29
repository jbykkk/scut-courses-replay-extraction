import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import subtitle
from browser import is_matching_subtitle_response
from config import (
    CourseUrlError,
    build_livingroom_url,
    get_site,
    parse_course_url,
)
from main import build_parser
from subtitle import (
    extract_subtitle_items,
    format_time,
    json_to_srt,
    json_to_text,
    save_subtitle,
)


INTERNAL = "https://video.jw.scut.edu.cn"
EXTERNAL = "https://video-jw-443.webvpn.scut.edu.cn"
FIXTURES = Path(__file__).parent / "fixtures"
URL_CASES = json.loads(
    (FIXTURES / "url_cases.json").read_text(encoding="utf-8")
)
SUBTITLE_CASES = json.loads(
    (FIXTURES / "subtitle_cases.json").read_text(encoding="utf-8")
)


class CourseUrlTests(unittest.TestCase):
    def test_shared_url_cases(self):
        for case in URL_CASES:
            with self.subTest(case=case["name"]):
                if not case["valid"]:
                    with self.assertRaises(CourseUrlError):
                        parse_course_url(case["url"])
                    continue
                course = parse_course_url(case["url"])
                self.assertEqual(course.site_key, case["site_key"])
                self.assertEqual(course.course_id, case["course_id"])
                self.assertEqual(course.sub_id, case["sub_id"])
                self.assertEqual(course.tenant_code, case["tenant_code"])

    def test_external_batch_url_stays_external(self):
        url = build_livingroom_url(EXTERNAL, "2", course_id="1", tenant_code="99")
        course = parse_course_url(url)
        self.assertEqual(course.origin, EXTERNAL)
        self.assertEqual(course.tenant_code, "99")

    def test_batch_url_builder_revalidates_every_id(self):
        for kwargs in (
            {"sub_id": "../../escaped"},
            {"sub_id": "2", "course_id": "../1"},
            {"sub_id": "2", "tenant_code": "21:evil"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_livingroom_url(INTERNAL, **kwargs)


class SubtitleTests(unittest.TestCase):
    def test_nested_extraction(self):
        body = {"list": [{"all_content": [{"BeginSec": 1, "Text": "你好"}]}]}
        self.assertEqual(
            extract_subtitle_items(body), [{"BeginSec": 1, "Text": "你好"}]
        )

    def test_empty_and_non_subtitle_json(self):
        self.assertIsNone(extract_subtitle_items({"list": []}))
        self.assertIsNone(
            extract_subtitle_items({"data": [{"name": "not subtitles"}]})
        )

    def test_shared_conversion_vectors(self):
        self.assertEqual(json_to_srt(SUBTITLE_CASES["items"]), SUBTITLE_CASES["srt"])
        self.assertEqual(json_to_text(SUBTITLE_CASES["items"]), SUBTITLE_CASES["txt"])

    def test_format_time(self):
        self.assertEqual(format_time(3661.5), "01:01:01,500")
        for invalid in ("NaN", "Infinity", "-Infinity", -1):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                format_time(invalid)

    def test_save_creates_three_readable_files(self):
        old_output = subtitle.OUTPUT_DIR
        items = [{"BeginSec": 0, "EndSec": 1, "Text": "测试字幕"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            subtitle.OUTPUT_DIR = output_dir
            try:
                json_path, srt_path, txt_path = save_subtitle(items, "554146")

                self.assertEqual(
                    json.loads(json_path.read_text(encoding="utf-8")), items
                )
                self.assertIn("测试字幕", srt_path.read_text(encoding="utf-8"))
                self.assertEqual(txt_path.read_text(encoding="utf-8"), "测试字幕")
                self.assertTrue(
                    json_path.resolve().is_relative_to((output_dir / "json").resolve())
                )
                self.assertTrue(
                    srt_path.resolve().is_relative_to((output_dir / "srt").resolve())
                )
                self.assertTrue(
                    txt_path.resolve().is_relative_to((output_dir / "txt").resolve())
                )
            finally:
                subtitle.OUTPUT_DIR = old_output

    def test_default_filename_does_not_repeat_sub_id(self):
        old_output = subtitle.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle.OUTPUT_DIR = Path(temp_dir)
            try:
                paths = save_subtitle([{"BeginSec": 0, "Text": "A"}], "554146")
                self.assertEqual(paths[0].name, "554146.json")
            finally:
                subtitle.OUTPUT_DIR = old_output

    def test_unsafe_sub_ids_are_rejected_without_escaping_temp_output(self):
        unsafe_ids = (
            "../../escaped",
            "..%2F..%2Fescaped",
            r"..\..\escaped",
            "/tmp/escaped",
            r"C:\Windows\Temp\escaped",
            "2/3",
            r"2\3",
            "2:3",
            "2\x00",
            "2\n3",
        )
        old_output = subtitle.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir).resolve()
            output_dir = temp_root / "output"
            subtitle.OUTPUT_DIR = output_dir
            try:
                for unsafe_id in unsafe_ids:
                    with self.subTest(sub_id=repr(unsafe_id)), self.assertRaises(
                        ValueError
                    ):
                        save_subtitle(
                            [{"BeginSec": 0, "EndSec": 1, "Text": "测试"}],
                            unsafe_id,
                        )
                written_files = [path for path in temp_root.rglob("*") if path.is_file()]
                self.assertEqual(written_files, [])
            finally:
                subtitle.OUTPUT_DIR = old_output

    def test_malicious_course_name_is_sanitized_and_paths_stay_contained(self):
        old_output = subtitle.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            subtitle.OUTPUT_DIR = output_dir
            try:
                paths = save_subtitle(
                    [{"BeginSec": 0, "EndSec": 1, "Text": "测试"}],
                    "554146",
                    course_name="../../evil:C\x00\n",
                )
                for path, subdirectory in zip(paths, ("json", "srt", "txt")):
                    self.assertTrue(
                        path.resolve().is_relative_to(
                            (output_dir / subdirectory).resolve()
                        )
                    )
                    self.assertNotIn("..", path.name)
            finally:
                subtitle.OUTPUT_DIR = old_output


class SubtitleResponseTests(unittest.TestCase):
    def test_only_exact_path_and_current_sub_id_match(self):
        site = get_site("internal")
        valid = (
            f"{INTERNAL}/courseapi/v3/web-socket/search-trans-result"
            "?sub_id=550530&format=json"
        )
        self.assertTrue(is_matching_subtitle_response(valid, site, "550530"))
        rejected = (
            f"{INTERNAL}/courseapi/v3/web-socket/search-trans-result?sub_id=999999",
            f"{INTERNAL}/courseapi/v3/web-socket/search-trans-result-extra?sub_id=550530",
            f"{INTERNAL}/other/search-trans-result?sub_id=550530",
            f"{INTERNAL}/courseapi/v3/web-socket/search-trans-result",
            f"{INTERNAL}/courseapi/v3/web-socket/search-trans-result?sub_id=550530&sub_id=550530",
            "https://video-jw-443.webvpn.scut.edu.cn/"
            "courseapi/v3/web-socket/search-trans-result?sub_id=550530",
            f"{INTERNAL}:444/courseapi/v3/web-socket/search-trans-result?sub_id=550530",
            "https://user:password@video.jw.scut.edu.cn/"
            "courseapi/v3/web-socket/search-trans-result?sub_id=550530",
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(is_matching_subtitle_response(url, site, "550530"))


class CliCompatibilityTests(unittest.TestCase):
    def test_login_defaults_to_internal_and_accepts_external(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["login"]).site, "internal")
        self.assertEqual(
            parser.parse_args(["login", "--site", "external"]).site,
            "external",
        )

    def test_get_cli_arguments(self):
        parser = build_parser()
        url = f"{INTERNAL}/livingroom?sub_id=2"
        args = parser.parse_args(["get", "--all", "--headless", url])
        self.assertEqual(args.command, "get")
        self.assertEqual(args.urls, [url])
        self.assertTrue(args.all)
        self.assertTrue(args.headless)

    def test_internal_login_can_read_legacy_auth_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            legacy_path = temp_path / "auth_state.json"
            legacy_path.write_text("{}", encoding="utf-8")
            with (
                patch.object(config, "DATA_DIR", temp_path),
                patch.object(auth, "LEGACY_AUTH_STATE_FILE", legacy_path),
            ):
                self.assertEqual(auth.get_auth_path("internal"), str(legacy_path))
                self.assertIsNone(auth.get_auth_path("external"))


class ManifestTests(unittest.TestCase):
    def test_extension_permissions_are_scoped(self):
        manifest_path = Path(__file__).parents[1] / "extension" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertNotIn("<all_urls>", manifest.get("host_permissions", []))
        self.assertIn("scripting", manifest["permissions"])
        self.assertEqual(
            manifest["host_permissions"],
            [f"{INTERNAL}/*", f"{EXTERNAL}/*"],
        )


if __name__ == "__main__":
    unittest.main()
