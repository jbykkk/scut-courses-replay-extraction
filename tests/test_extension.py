"""Offline popup regression tests. Run: uv run python -m unittest discover -s tests -v."""

import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse, urlencode
import zipfile

from playwright.sync_api import sync_playwright, expect


EXTENSION = Path(__file__).resolve().parents[1] / "extension"
ORIGIN = "https://video.jw.scut.edu.cn"
EXTERNAL_ORIGIN = "https://video-jw-443.webvpn.scut.edu.cn"
ITEMS = [{"BeginSec": 1, "EndSec": 3, "Text": "测试字幕"}]
NO_SUBTITLE = {"code": 10002, "msg": "未查询到语音数据", "total": 0, "list": []}


def lesson(sid, status="6", start="1700000000"):
    return {"sub_id": sid, "title": f"课时 {sid}", "status": status, "start_at": start}


class ExtensionBrowserCase(unittest.TestCase):
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
        self.media = {}
        self.media_error = False
        self.page.add_init_script("""(() => {
            window.downloads = [];
            window.openedTabs = [];
            window.downloadListeners = new Set();
            window.downloadStates = new Map();
            window.pageRequests = [];
            window.courseTab = {id: 7, url: `${location.origin}/livingroom?course_id=42&sub_id=1`};
            const nativeFetch = window.fetch.bind(window);
            window.fetch = (url, options) => {
                if (window.blockDirectRequests && !window.inPageRequest && String(url).includes('/courseapi/')) {
                    return Promise.reject(new TypeError('Failed to fetch'));
                }
                if (window.pauseMedia && String(url).includes('/media/')) {
                    return new Promise((resolve, reject) => options.signal.addEventListener('abort',
                        () => reject(new DOMException('Cancelled', 'AbortError')), {once: true}));
                }
                return nativeFetch(url, options);
            };
            window.chrome = {
                tabs: {query: async () => [window.courseTab], get: async () => window.courseTab,
                    create: async (options) => {
                        if (window.tabError) throw new Error('无法打开新标签页');
                        window.openedTabs.push(options); return {id: 8};
                    }},
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
                runtime: {getURL: (path) => `${location.origin}/${path}`},
                downloads: {
                  onChanged: {addListener: (listener) => window.downloadListeners.add(listener),
                    removeListener: (listener) => window.downloadListeners.delete(listener)},
                  search: ({id}, callback) => callback([{id, state: window.downloadStates.get(id) || 'complete'}]),
                  cancel: (id, callback) => {window.downloadStates.set(id, 'interrupted'); callback();},
                  download: async (options, callback) => {
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
                    window.downloadStates.set(window.downloads.length, window.interruptDownload ? 'interrupted' : 'complete');
                    if (window.holdDownload) await new Promise((resolve) => {window.releaseDownload = resolve;});
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
                sid = parse_qs(url.query)["sub_id"][0]
                body = self.media.get(sid) or {"code": 0, "data": {"lecturer_name": "教师", "sub_title": "当前课时"}}
            else:
                body = {"code": 0, "data": [{"course_title": "测试课程"}]}
            route.fulfill(json=body)
        elif url.path.startswith('/media/'):
            if self.media_error:
                route.fulfill(status=503, body='unavailable')
            else:
                route.fulfill(path=Path(__file__).parent / 'fixtures/replay-aac.mp4', content_type='video/mp4')
        else:
            file = EXTENSION / url.path.lstrip('/')
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


class PopupTests(ExtensionBrowserCase):
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

    def test_single_subtitle_formats_still_download(self):
        self.open_batch()
        self.page.locator('[data-tab="single"]').click()
        self.page.locator('#fmt-json').check()
        self.page.locator('#download-btn').click()
        expect(self.page.locator('#status-single')).to_have_text('已下载 1 条字幕')
        files = self.page.evaluate('window.downloads')
        self.assertEqual([file['filename'] for file in files], ['subtitle_1.srt', 'subtitle_1.txt', 'subtitle_1.json'])
        self.assertEqual(json.loads(base64.b64decode(files[2]['base64'])), ITEMS)

    def test_audio_buttons_open_independent_page_for_current_and_selected_lessons(self):
        self.rows = [lesson('1'), lesson('2')]
        self.open_batch()
        self.page.locator('.lesson-check[data-sub-id="1"]').uncheck()
        self.page.locator('#batch-audio-btn').click()
        self.page.wait_for_function('window.openedTabs.length === 1')
        params = parse_qs(urlparse(self.page.evaluate('window.openedTabs[0].url')).query)
        self.assertEqual(params['sub_ids'], ['2'])
        self.assertEqual(params['origin'], [ORIGIN])
        self.page.locator('[data-tab="single"]').click()
        self.page.locator('#audio-download-btn').click()
        self.page.wait_for_function('window.openedTabs.length === 2')
        self.assertEqual(self.requests, [])
        params = parse_qs(urlparse(self.page.evaluate('window.openedTabs[1].url')).query)
        self.assertEqual(params['sub_ids'], ['1'])

    def test_selected_audio_uses_select_all_and_downloads_available_replays(self):
        self.rows = [lesson('1'), lesson('2'), lesson('3', '2', '4102444800'),
                     lesson('4', '1'), lesson('5', '5'), lesson('6', '6', '4102444800'), lesson('1')]
        for sid in ('1', '2'):
            self.media[sid] = {'code': 0, 'data': {'sub_title': f'测试回放 {sid}',
                'content': {'save_playback': {'contents': f'{ORIGIN}/media/replay.mp4'}}}}
        self.open_batch()
        self.page.locator('.lesson-check[data-sub-id="2"]').uncheck()
        expect(self.page.locator('#batch-audio-btn')).to_have_text('下载已选课程音频（1 次）')
        self.page.locator('#select-all').check()
        expect(self.page.locator('#batch-audio-btn')).to_have_text('下载已选课程音频（2 次）')
        self.page.locator('#batch-audio-btn').click()
        self.page.wait_for_function('window.openedTabs.length === 1')
        target = self.page.evaluate('window.openedTabs[0].url')
        params = parse_qs(urlparse(target).query)
        self.assertEqual(params['sub_ids'], ['1,2'])
        expect(self.page.locator('.lesson-check[data-sub-id="2"]')).to_be_checked()
        self.assertEqual(self.requests, [])
        self.page.goto(target)
        expect(self.page.locator('#audio-summary')).to_have_text('完成：2 次音频已保存，0 次失败，0 次未完成')
        downloads = self.page.evaluate('window.downloads')
        self.assertEqual(len(downloads), 2)
        self.assertEqual(len({item['filename'] for item in downloads}), 2)
        self.assertTrue(all(item['filename'].endswith('.m4a') for item in downloads))

    def test_audio_selection_supports_select_all_and_clear_all(self):
        self.rows = [lesson('1'), lesson('2')]
        self.open_batch()
        expect(self.page.locator('#all-audio-btn')).to_have_count(0)
        expect(self.page.locator('#batch-audio-btn')).to_have_text('下载已选课程音频（2 次）')
        self.page.locator('#select-all').uncheck()
        expect(self.page.locator('#batch-audio-btn')).to_have_text('下载已选课程音频（0 次）')
        expect(self.page.locator('#batch-audio-btn')).to_be_disabled()
        self.page.locator('.lesson-check[data-sub-id="1"]').check()
        expect(self.page.locator('#batch-audio-btn')).to_have_text('下载已选课程音频（1 次）')
        expect(self.page.locator('#batch-audio-btn')).to_be_enabled()

    def test_missing_audio_button_only_selects_no_subtitle_not_errors(self):
        self.rows = [lesson('1'), lesson('2'), lesson('3')]
        self.subtitles = {'2': NO_SUBTITLE, '3': {'code': 5002, 'msg': '未传入token'}}
        self.open_batch()
        self.export()
        self.page.locator('#missing-audio-btn').click()
        self.page.wait_for_function('window.openedTabs.length === 1')
        params = parse_qs(urlparse(self.page.evaluate('window.openedTabs[0].url')).query)
        self.assertEqual(params['sub_ids'], ['2'])
        with self.downloaded_zip() as archive:
            self.assertEqual(json.loads(archive.read('字幕导出结果.json'))['results'][0]['status'], 'exported')

    def test_audio_open_failure_does_not_prevent_subtitle_download(self):
        self.open_batch()
        self.page.locator('[data-tab="single"]').click()
        self.page.evaluate('window.tabError = true')
        self.page.locator('#audio-download-btn').click()
        expect(self.page.locator('#status-single')).to_contain_text('无法打开音频下载')
        self.page.locator('#download-btn').click()
        expect(self.page.locator('#status-single')).to_have_text('已下载 1 条字幕')
        self.assertEqual(self.page.evaluate('window.downloads.length'), 2)

    def test_audio_controls_cannot_interrupt_subtitle_zip(self):
        self.open_batch()
        self.page.evaluate('window.holdDownload = true')
        self.page.locator('#batch-download-btn').click()
        self.page.wait_for_function('typeof window.releaseDownload === "function"')
        expect(self.page.locator('#batch-audio-btn')).to_be_disabled()
        expect(self.page.locator('#audio-download-btn')).to_be_disabled()
        self.page.evaluate('window.releaseDownload()')
        expect(self.page.locator('#status-batch')).to_have_class('status success')
        expect(self.page.locator('#batch-audio-btn')).to_be_enabled()


class AudioPageTests(ExtensionBrowserCase):
    def setUp(self):
        super().setUp()
        self.media['1'] = self.audio_info()

    def audio_info(self):
        return {'code': 0, 'data': {'sub_title': '测试回放', 'sub_status': '6', 'is_m3u8': 'no',
                'content': {'save_playback': {'contents': f'{ORIGIN}/media/replay.mp4'}}}}

    def open_audio(self, ids='1'):
        query = urlencode({'origin': self.origin, 'course_id': '42', 'sub_ids': ids, 'tab_id': '7'})
        self.page.goto(f'{self.origin}/audio.html?{query}')

    def wait_complete(self, count=1):
        expect(self.page.locator('#audio-summary')).to_contain_text(f'{count} 次音频已保存')
        expect(self.page.locator('#cancel-audio')).to_be_disabled()

    def test_real_mp4_is_remuxed_to_audio_and_report_matches_saved_file(self):
        self.open_audio()
        self.wait_complete()
        download = self.page.evaluate('window.downloads[0]')
        self.assertTrue(download['filename'].endswith('.m4a'))
        content = base64.b64decode(download['base64'])
        self.assertGreater(len(content), 0)
        if shutil.which('ffprobe'):
            with tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / 'audio.m4a'
                output.write_bytes(content)
                info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                    'stream=codec_type,codec_name:format=duration', '-of', 'json', str(output)]))
                self.assertEqual([(track['codec_type'], track['codec_name']) for track in info['streams']], [('audio', 'aac')])
                self.assertAlmostEqual(float(info['format']['duration']), 2.021333, places=3)
                if shutil.which('ffmpeg'):
                    def packets(path):
                        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                            '-map', '0:a:0', '-c', 'copy', '-f', 'data', '-'])
                    self.assertEqual(packets(output), packets(Path(__file__).parent / 'fixtures/replay-aac.mp4'))
        self.page.locator('#audio-report-btn').click()
        self.page.wait_for_function('window.downloads.length === 2')
        report = json.loads(base64.b64decode(self.page.evaluate('window.downloads[1].base64')))
        self.assertEqual(report['items'][0]['size_bytes'], len(content))
        self.assertEqual(report['items'][0]['status'], 'downloaded')
        self.assertEqual(self.page.evaluate('window.downloadListeners.size'), 0)
        self.assertEqual(self.requests, [])

    def test_partial_failure_and_retry_keep_successful_download(self):
        self.media['1'] = {'code': 10002, 'msg': '没有回放'}
        self.media['2'] = self.audio_info()
        self.open_audio('1,2')
        self.wait_complete()
        expect(self.page.locator('#audio-summary')).to_contain_text('1 次失败')
        self.media['1'] = self.audio_info()
        self.page.locator('#retry-audio').click()
        self.wait_complete(2)
        self.assertEqual(self.page.evaluate('window.downloads.length'), 2)
        expect(self.page.locator('#retry-audio')).to_be_disabled()

    def test_media_http_failure_and_download_interruption_are_reported(self):
        self.media_error = True
        self.open_audio()
        expect(self.page.locator('#audio-summary')).to_contain_text('1 次失败')
        expect(self.page.locator('.audio-state')).to_contain_text('HTTP 503')
        self.media_error = False
        self.page.evaluate('window.downloadError = true')
        self.page.locator('#retry-audio').click()
        expect(self.page.locator('.audio-state')).to_contain_text('浏览器未能创建下载任务')
        self.page.evaluate('window.downloadError = false')
        self.page.evaluate('window.interruptDownload = true')
        self.page.locator('#retry-audio').click()
        expect(self.page.locator('.audio-state')).to_contain_text('文件保存失败')
        self.assertEqual(self.page.evaluate('window.downloadListeners.size'), 0)

    def test_cancel_and_retry_release_network_task(self):
        self.page.add_init_script('window.pauseMedia = true')
        self.open_audio()
        expect(self.page.locator('.audio-state')).to_contain_text('正在读取回放')
        self.page.locator('#cancel-audio').click()
        expect(self.page.locator('#audio-summary')).to_contain_text('已取消')
        self.assertEqual(self.page.evaluate('window.downloads.length'), 0)
        self.page.evaluate('window.pauseMedia = false')
        self.page.locator('#retry-audio').click()
        self.wait_complete()

    def test_webvpn_fallback_and_media_origin_remain_external(self):
        self.origin = EXTERNAL_ORIGIN
        self.page.add_init_script('window.blockDirectRequests = true')
        self.open_audio()
        self.wait_complete()
        requests = self.page.evaluate('window.pageRequests')
        self.assertTrue(requests)
        self.assertTrue(all(urlparse(url).netloc == urlparse(EXTERNAL_ORIGIN).netloc for url in requests))

    def test_invalid_task_and_hls_fail_without_saving_video(self):
        self.page.goto(f'{ORIGIN}/audio.html?origin=https://example.com&course_id=42&sub_ids=1&tab_id=7')
        expect(self.page.locator('#audio-summary')).to_have_class('status error')
        self.media['1']['data']['is_m3u8'] = 'yes'
        self.open_audio()
        expect(self.page.locator('.audio-state')).to_contain_text('HLS')
        self.assertEqual(self.page.evaluate('window.downloads.length'), 0)


if __name__ == "__main__":
    unittest.main()
