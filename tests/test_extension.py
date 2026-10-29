"""Offline popup regression tests. Run: uv run python -m unittest discover -s tests -v."""

import base64
import io
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse
import zipfile

from playwright.sync_api import sync_playwright, expect


EXTENSION = Path(__file__).resolve().parents[1] / "extension"
ORIGIN = "https://video.jw.scut.edu.cn"
EXTERNAL_ORIGIN = "https://video-jw-443.webvpn.scut.edu.cn"
ITEMS = [{"BeginSec": 1, "EndSec": 3, "Text": "测试字幕"}]
NO_SUBTITLE = {"code": 10002, "msg": "未查询到语音数据", "total": 0, "list": []}


def lesson(sid, status="6", start="1700000000"):
    return {"sub_id": sid, "title": f"课时 {sid}", "status": status, "start_at": start}


class PopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.rows = [lesson("1")]
        self.subtitles = {}
        self.requests = []
        self.http_attempts = {}
        self.transient_failure = set()
        self.catalogue_body = None
        self.metadata_error = False
        self.origin = ORIGIN
        self.page.add_init_script("""(() => {
            window.downloads = [];
            window.pageRequests = [];
            window.courseTab = {id: 7, url: `${location.origin}/livingroom?course_id=42&sub_id=1`};
            const nativeFetch = window.fetch.bind(window);
            window.fetch = (url, options) => {
                if (window.blockDirectRequests && !window.inPageRequest && String(url).includes('/courseapi/')) {
                    return Promise.reject(new TypeError('Failed to fetch'));
                }
                return nativeFetch(url, options);
            };
            window.chrome = {
                tabs: {query: async () => [window.courseTab], get: async () => window.courseTab},
                scripting: {executeScript: async ({target, func, args}) => {
                    if (target.tabId !== window.courseTab.id) throw new Error('Invalid tab');
                    window.pageRequests.push(args[0]);
                    window.inPageRequest = true;
                    try {
                        return [{result: await func(...args)}];
                    } finally {
                        window.inPageRequest = false;
                    }
                }},
                runtime: {},
                downloads: {download: async (options, callback) => {
                    if (window.downloadError) {
                        chrome.runtime.lastError = {message: '下载被阻止'};
                        callback();
                        delete chrome.runtime.lastError;
                        return;
                    }
                    const blob = await (await fetch(options.url)).blob();
                    const bytes = new Uint8Array(await blob.arrayBuffer());
                    let binary = '';
                    for (const byte of bytes) binary += String.fromCharCode(byte);
                    window.downloads.push({filename: options.filename, base64: btoa(binary)});
                    callback(window.downloads.length);
                }}
            };
        })()""")
        for origin in (ORIGIN, EXTERNAL_ORIGIN):
            self.page.route(f"{origin}/**", self.route)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        url = urlparse(route.request.url)
        if url.path.startswith("/courseapi/"):
            if "catalogue" in url.path:
                body = self.catalogue_body or {"success": True, "result": {"code": 200, "data": self.rows}}
            elif "search-trans-result" in url.path:
                sid = parse_qs(url.query)["sub_id"][0]
                self.requests.append(sid)
                self.http_attempts[sid] = self.http_attempts.get(sid, 0) + 1
                if sid in self.transient_failure and self.http_attempts[sid] == 1:
                    route.fulfill(status=503, body="unavailable")
                    return
                body = self.subtitles.get(sid, {"code": 0, "list": [{"all_content": ITEMS}]})
            elif self.metadata_error:
                body = {"code": 5002, "msg": "未传入token"}
            elif "get-sub-info" in url.path:
                body = {"code": 0, "data": {"lecturer_name": "教师", "sub_title": "当前课时"}}
            else:
                body = {"code": 0, "data": [{"course_title": "测试课程"}]}
            route.fulfill(json=body)
        else:
            file = EXTENSION / Path(url.path).name
            route.fulfill(path=file)

    def open_batch(self):
        self.page.goto(f"{self.origin}/popup.html")
        expect(self.page.locator("#lesson-count")).not_to_have_text("加载中...")
        self.page.locator('[data-tab="batch"]').click()

    def export(self):
        self.page.locator("#batch-download-btn").click()
        expect(self.page.locator("#status-batch")).not_to_have_class("status loading")
        expect(self.page.locator("#batch-download-btn")).to_be_enabled()

    def downloaded_zip(self):
        self.page.wait_for_function("window.downloads.length > 0")
        download = self.page.evaluate("window.downloads[0]")
        return zipfile.ZipFile(io.BytesIO(base64.b64decode(download["base64"])))

    def test_only_past_replays_are_selected_and_missing_subtitles_are_reported(self):
        # Same distribution as the reported course: 4 with subtitles, 3 without,
        # 1 live, 2 without replay, 13 future. Numeric IDs also exercise normalization.
        self.rows = [lesson(i) for i in range(1, 8)]
        self.rows += [lesson("101", "1"), lesson("102", "5"), lesson("103", "3")]
        self.rows += [lesson(str(200 + i), "2", "4102444800") for i in range(13)]
        self.rows += [lesson("300", "6", "4102444800000"), lesson(1), lesson("../../escaped")]
        self.subtitles = {str(i): NO_SUBTITLE for i in range(5, 8)}
        self.open_batch()
        expect(self.page.locator(".lesson-check")).to_have_count(7)
        expect(self.page.locator(".lesson-check:checked")).to_have_count(7)
        expect(self.page.locator("#catalogue-note")).to_have_text("已识别 7 节课")
        self.export()
        self.assertEqual(self.requests, list(map(str, range(1, 8))))
        expect(self.page.locator("#catalogue-note")).to_have_text("已识别 7 节课 · 3 节异常")
        expect(self.page.locator("#status-batch")).to_have_text("完成：4 次回放已导出，3 次暂无字幕，0 次请求失败")
        expect(self.page.locator("#status-batch")).to_have_class("status warning")
        expect(self.page.locator("#batch-details-text")).to_contain_text("未查询到语音数据")
        with self.downloaded_zip() as archive:
            self.assertEqual(len(archive.namelist()), 9)  # 4 SRT, 4 TXT, 1 report
            report = json.loads(archive.read("字幕导出结果.json"))
            self.assertEqual([r["status"] for r in report["results"]], ["exported"] * 4 + ["no_subtitle"] * 3)
            self.assertEqual(report["excluded"], {"upcoming": 14, "live": 1, "unavailable": 2})
            srt = next(name for name in archive.namelist() if name.endswith(".srt"))
            self.assertIn("00:00:01,000 --> 00:00:03,000", archive.read(srt).decode())

    def test_all_missing_and_auth_errors_have_separate_reasons_and_downloadable_report(self):
        self.rows = [lesson("1"), lesson("2")]
        self.subtitles = {"1": NO_SUBTITLE, "2": {"code": 5002, "msg": "未传入token"}}
        self.open_batch()
        self.export()
        expect(self.page.locator("#status-batch")).to_contain_text("1 次暂无字幕，1 次请求失败")
        expect(self.page.locator("#batch-details-text")).to_contain_text("未传入token")
        self.assertEqual(self.page.evaluate("window.downloads.length"), 0)
        self.page.locator("#report-download-btn").click()
        self.page.wait_for_function("window.downloads.length === 1")
        download = self.page.evaluate("window.downloads[0]")
        report = json.loads(base64.b64decode(download["base64"]))
        self.assertEqual([r["status"] for r in report["results"]], ["no_subtitle", "error"])

    def test_catalogue_error_is_visible_and_disables_batch_download(self):
        self.catalogue_body = {"success": False, "result": {"code": 403, "msg": "请重新登录"}}
        self.open_batch()
        expect(self.page.locator("#status-batch")).to_have_text("请重新登录")
        expect(self.page.locator("#batch-download-btn")).to_be_disabled()
        expect(self.page.locator("#select-all")).not_to_be_checked()

    def test_download_failure_restores_controls_and_keeps_report(self):
        self.open_batch()
        self.page.evaluate("window.downloadError = true")
        self.export()
        expect(self.page.locator("#status-batch")).to_have_text("导出失败：浏览器未能创建下载任务：下载被阻止")
        expect(self.page.locator("#select-all")).to_be_enabled()
        expect(self.page.locator("#report-download-btn")).to_be_visible()
        self.page.evaluate("window.downloadError = false")
        self.export()
        expect(self.page.locator("#status-batch")).to_have_class("status success")

    def test_metadata_failure_does_not_block_export_and_transient_failure_is_retried(self):
        self.metadata_error = True
        self.transient_failure = {"1"}
        self.subtitles = {"1": {"code": 0, "data": {"result": {"list": [{"all_content": ITEMS}]}}}}
        self.open_batch()
        self.export()
        self.assertEqual(self.http_attempts["1"], 2)
        expect(self.page.locator("#status-batch")).to_have_text("完成：1 次回放已导出，0 次暂无字幕，0 次请求失败")
        expect(self.page.locator("#catalogue-note")).to_have_text("已识别 1 节课 · 0 节异常")
        with self.downloaded_zip() as archive:
            self.assertEqual(len(archive.namelist()), 3)

    def test_select_all_and_partial_selection_only_export_selected_lessons(self):
        self.rows = [lesson("1"), lesson("2")]
        self.open_batch()
        self.page.locator('.lesson-check[data-sub-id="2"]').uncheck()
        self.assertTrue(self.page.locator("#select-all").evaluate("el => el.indeterminate"))
        expect(self.page.locator("#lesson-count")).to_have_text("已选 1 / 2 次回放")
        self.export()
        self.assertEqual(self.requests, ["1"])
        expect(self.page.locator("#catalogue-note")).to_have_text("已识别 2 节课")

    def test_no_past_replays_disables_selection(self):
        self.rows = [lesson("101", "2", "4102444800"), lesson("102", "5")]
        self.open_batch()
        expect(self.page.locator("#lesson-count")).to_have_text("当前没有可导出的回放")
        expect(self.page.locator("#batch-download-btn")).to_be_disabled()
        expect(self.page.locator("#select-all")).to_be_disabled()

    def test_webvpn_fallback_keeps_filtering_and_subtitle_report(self):
        self.origin = EXTERNAL_ORIGIN
        self.rows = [lesson("1"), lesson("2"), lesson("3", "2", "4102444800")]
        self.subtitles = {"2": NO_SUBTITLE}
        self.page.add_init_script("window.blockDirectRequests = true")
        self.open_batch()
        expect(self.page.locator("#site-name")).to_have_text("校外 WebVPN")
        expect(self.page.locator(".lesson-check")).to_have_count(2)
        self.export()
        self.assertEqual(self.requests, ["1", "2"])
        expect(self.page.locator("#catalogue-note")).to_have_text("已识别 2 节课 · 1 节异常")
        page_requests = self.page.evaluate("window.pageRequests")
        self.assertTrue(page_requests)
        self.assertTrue(all(urlparse(url).netloc == urlparse(EXTERNAL_ORIGIN).netloc for url in page_requests))
        with self.downloaded_zip() as archive:
            report = json.loads(archive.read("字幕导出结果.json"))
            self.assertEqual([r["status"] for r in report["results"]], ["exported", "no_subtitle"])

    def test_page_fallback_rejects_tab_switched_to_another_origin(self):
        self.open_batch()
        self.page.evaluate("""() => {
            window.blockDirectRequests = true;
            window.courseTab.url = 'https://example.com/livingroom?course_id=42&sub_id=1';
        }""")
        self.export()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.page.evaluate("window.pageRequests"), [])
        expect(self.page.locator("#batch-details-text")).to_contain_text("课程标签页已经切换")


if __name__ == "__main__":
    unittest.main()
