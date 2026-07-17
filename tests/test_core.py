import json
import tempfile
import unittest
from pathlib import Path

from config import CourseUrlError, build_livingroom_url, parse_course_url
from subtitle import (
    extract_subtitle_items,
    format_time,
    json_to_srt,
    json_to_text,
    save_subtitle,
)


INTERNAL = "https://video.jw.scut.edu.cn"
EXTERNAL = "https://video-jw-443.webvpn.scut.edu.cn"


class CourseUrlTests(unittest.TestCase):
    def test_internal_url(self):
        course = parse_course_url(
            f"{INTERNAL}/livingroom?course_id=1&sub_id=2&tenant_code=21"
        )
        self.assertEqual(course.site_key, "internal")
        self.assertEqual(course.course_id, "1")
        self.assertEqual(course.sub_id, "2")

    def test_external_url_and_parameter_order(self):
        course = parse_course_url(
            f"{EXTERNAL}/livingroom/?sub_id=2&tenant_code=99&course_id=1"
        )
        self.assertEqual(course.site_key, "external")
        self.assertEqual(course.origin, EXTERNAL)
        self.assertEqual(course.tenant_code, "99")

    def test_course_id_optional_and_tenant_default(self):
        course = parse_course_url(f"{EXTERNAL}/livingroom?sub_id=2")
        self.assertIsNone(course.course_id)
        self.assertEqual(course.tenant_code, "21")

    def test_invalid_urls_are_rejected(self):
        invalid = (
            f"http://video.jw.scut.edu.cn/livingroom?sub_id=2",
            "https://example.com/livingroom?sub_id=2",
            "https://video.jw.scut.edu.cn.example.com/livingroom?sub_id=2",
            f"{INTERNAL}/other?sub_id=2",
            f"{INTERNAL}/livingroom?course_id=1",
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(CourseUrlError):
                parse_course_url(url)

    def test_external_batch_url_stays_external(self):
        url = build_livingroom_url(EXTERNAL, "2", course_id="1", tenant_code="99")
        course = parse_course_url(url)
        self.assertEqual(course.origin, EXTERNAL)
        self.assertEqual(course.tenant_code, "99")


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

    def test_srt_skips_invalid_and_numbers_continuously(self):
        items = [
            {"BeginSec": 1, "EndSec": 2, "Text": "第一条"},
            {"BeginSec": 2, "EndSec": 3, "Text": "  "},
            {"BeginSec": 3, "EndSec": 4, "Text": None},
            {"BeginSec": "bad", "EndSec": 4, "Text": "错误时间"},
            {"BeginSec": 5.25, "Text": "第二条"},
        ]
        result = json_to_srt(items)
        self.assertIn("1\n00:00:01,000 --> 00:00:02,000\n第一条", result)
        self.assertIn("2\n00:00:05,250 --> 00:00:10,250\n第二条", result)
        self.assertNotIn("3\n", result)

    def test_time_and_text(self):
        self.assertEqual(format_time(3661.5), "01:01:01,500")
        self.assertEqual(
            json_to_text([{"Text": " A "}, {"Text": ""}, {"Text": "B"}]),
            "A\nB",
        )

    def test_default_filename_does_not_repeat_sub_id(self):
        import subtitle

        old_output = subtitle.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle.OUTPUT_DIR = Path(temp_dir)
            try:
                paths = save_subtitle([{"BeginSec": 0, "Text": "A"}], "554146")
            finally:
                subtitle.OUTPUT_DIR = old_output
        self.assertEqual(paths[0].name, "554146.json")


class ManifestTests(unittest.TestCase):
    def test_extension_permissions(self):
        manifest_path = Path(__file__).parents[1] / "extension" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertIn("scripting", manifest["permissions"])
        self.assertIn(f"{INTERNAL}/*", manifest["host_permissions"])
        self.assertIn(f"{EXTERNAL}/*", manifest["host_permissions"])


if __name__ == "__main__":
    unittest.main()
