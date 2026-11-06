import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from assistant_service.app import create_app
from assistant_service.manager import Manager
from assistant_service.mindmap import outline, view
from assistant_service.settings import Settings
from assistant_service.store import Store


def lesson():
    return {
        "title": "第 3 课", "course_title": "操作系统", "time_basis": "video",
        "page_url": "https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=2",
        "segments": [{"id": "speech-1", "start": 62, "end": 90, "text": "检查条件时应使用 while。"}],
        "summary": {"headline": "进程同步", "partial": False, "topics": [
            {"title": "条件变量", "end": 90, "detail": "## 定义\n\n等待共享状态变化。\n- 使用 while 检查条件", "start": 62,
             "segment_ids": ["speech-1"], "material_refs": [
                 {"title": "同步.pdf", "locator": {"page": 7}, "quote": "循环检查条件", "source_id": "a" * 32}]},
            {"title": "互斥锁", "detail": "保护共享状态。", "start": 95, "end": 110, "segment_ids": []},
        ], "groups": [{"title": "线程同步", "detail": "", "topic_indices": [0, 0, 90, True]}]},
    }


class OutlineTests(unittest.TestCase):
    def test_groups_unassigned_topics_details_and_original_locations_survive(self):
        record = lesson()
        original = copy.deepcopy(record)
        markdown, sources = outline(record)
        self.assertIn("## 线程同步", markdown)
        self.assertIn("## 其他知识点", markdown)
        self.assertEqual(markdown.count("### [条件变量]"), 1)
        self.assertIn("等待共享状态变化。", markdown)
        self.assertIn("课堂原话 00:01:02", markdown)
        self.assertIn("同步.pdf · 第 7 页", markdown)
        self.assertEqual(sorted(sources.values()), [0, 1])
        self.assertEqual(record, original)

    def test_evidence_links_never_silently_retarget_after_reordering(self):
        record = lesson()
        old = outline(record)[1]
        record["summary"]["topics"].insert(0, {"title": "新增主题", "detail": "新内容"})
        new = outline(record)[1]
        for key, index in old.items():
            self.assertEqual(new[key], index + 1)
        record["summary"]["topics"][1]["title"] = "修改后的主题"
        newer = outline(record)[1]
        self.assertNotIn(next(key for key, index in old.items() if index == 0), newer)

    def test_capture_time_is_not_labeled_as_video_and_markdown_labels_are_escaped(self):
        record = lesson()
        record["time_basis"] = "capture"
        record["summary"]["topics"][0]["title"] = "[链接](javascript:alert(1)) <img>"
        markdown, _ = outline(record)
        self.assertIn("录音原话 00:01:02", markdown)
        self.assertIn(r"\[链接\]\(javascript:alert\(1\)\) \<img\>", markdown)
        self.assertFalse(view({"title": "无笔记"})["available"])


class MindmapApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(Path(temp.name))
        self.store = Store(self.settings.root)
        self.addCleanup(self.store.db.close)
        self.manager = Manager(self.settings, self.store)
        self.addCleanup(self.manager.close)
        self.sid = self.store.create(dict(mode="subtitle", course_id="1", sub_id="2", **lesson()))["id"]
        self.store.update(self.sid, **lesson(), stopped=True, status="complete")
        self.client = TestClient(create_app(self.settings, self.store, self.manager, run_workers=False))
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer " + self.settings.data["token"]
        self.url = f"/api/sessions/{self.sid}/mindmap"

    def test_edits_persist_separately_and_concurrent_writers_cannot_overwrite(self):
        original = copy.deepcopy(self.store.get(self.sid)["summary"])
        initial = self.client.get(self.url).json()
        body = {"markdown": "# 我的复习提纲\n\n- 要再看条件变量\n", "revision": initial["revision"], "source_hash": initial["source_hash"]}
        response = self.client.post(self.url, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertTrue(saved["custom"])
        self.assertEqual(saved["markdown"], body["markdown"])
        self.assertEqual(self.client.get(self.url).json()["markdown"], body["markdown"])
        self.assertEqual(self.client.post(self.url, json=body).status_code, 409)
        self.assertEqual(self.store.get(self.sid)["summary"], original)
        persisted = json.loads((self.store.directory(self.sid) / "session.json").read_text("utf-8"))
        self.assertEqual(persisted["mindmap"]["markdown"], body["markdown"])
        self.assertNotIn("mindmap", self.client.get("/api/sessions").json()[0])

    def test_note_updates_preserve_edits_and_mark_them_outdated(self):
        initial = self.client.get(self.url).json()
        body = {"markdown": initial["markdown"] + "\n- 手动补充\n", "revision": "", "source_hash": initial["source_hash"]}
        saved = self.client.post(self.url, json=body).json()
        new = lesson()["summary"]
        new["topics"].append({"title": "信号量", "detail": "控制可用资源数量。"})
        self.store.update(self.sid, summary=new)
        stale = self.client.get(self.url).json()
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["markdown"], body["markdown"])
        self.assertIn("信号量", stale["generated_markdown"])
        body.update(revision=saved["revision"])
        self.assertTrue(self.client.post(self.url, json=body).json()["stale"])
        body.update(markdown=stale["generated_markdown"], source_hash=stale["current_source_hash"], revision=self.client.get(self.url).json()["revision"])
        self.assertFalse(self.client.post(self.url, json=body).json()["stale"])

    def test_validation_pairing_and_deleted_lessons(self):
        data = self.client.get(self.url).json()
        body = {"markdown": "  ", "revision": "", "source_hash": data["source_hash"]}
        self.assertEqual(self.client.post(self.url, json=body).status_code, 400)
        body["markdown"] = "x" * 200001
        self.assertEqual(self.client.post(self.url, json=body).status_code, 422)
        body["markdown"] = " " * 61 + "- nested"
        self.assertEqual(self.client.post(self.url, json=body).status_code, 400)
        del self.client.headers["Authorization"]
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.headers["Authorization"] = "Bearer " + self.settings.data["token"]
        self.store.update(self.sid, deleted_at=1)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_offline_assets_have_correct_mime_and_match_the_recorded_checksums(self):
        root = Path(__file__).resolve().parents[1] / "extension"
        manifest = json.loads((root / "markmap-vendor.json").read_text("utf-8"))
        for name, checksum in manifest["files"].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), checksum)
        for name, mime in [("mindmap.js", "text/javascript"), ("markmap-vendor.min.js", "text/javascript"),
                           ("mindmap.css", "text/css"), ("markmap-vendor.css", "text/css")]:
            response = self.client.get("/" + name)
            self.assertEqual(response.status_code, 200)
            self.assertIn(mime, response.headers["content-type"])
            self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
