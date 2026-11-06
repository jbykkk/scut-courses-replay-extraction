import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from assistant_service.analysis import Analyzer
from assistant_service.manager import Manager
from assistant_service.settings import Settings
from assistant_service.store import Store


class ReviewSheetTests(unittest.TestCase):
    def test_long_bilingual_markdown_and_material_quote_survive_analysis_storage_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            detail = "## 条件变量 (Condition Variable)\n\n**定义 (Definition)**：_等待条件成立_。\n\n" + "完整解释与推导步骤。" * 400 + "\n讲义末尾 END"
            overview = "全课逻辑联系。" * 500
            title = "双语知识主题" * 40
            quote = "Condition variables require a while loop. " * 50
            materials = [{"id": "source:0", "source_id": "a" * 32, "title": "同步.pdf", "locator": {"page": 7}, "text": quote}]
            segments = [{"id": "speech", "start": 20, "end": 45, "text": "条件变量使用 while 循环等待。"}]
            requests = []

            def respond(request):
                payload = json.loads(request.content)
                requests.append(payload)
                data = json.loads(payload["messages"][1]["content"])
                content = {"events": [], "overview": "", "topics": []}
                if data.get("materials"):
                    content.update(title=title, overview=overview, topics=[{"title": title, "detail": detail,
                        "segment_ids": ["s1"], "material_refs": [{"id": "m1", "quote": quote}]}])
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})

            analyzer = Analyzer(settings, httpx.MockTransport(respond))
            self.addCleanup(analyzer.close)
            with patch.object(settings, "key", return_value="test-key"):
                note = analyzer.analyze(segments, summary=True, materials=materials)
                analyzer.synthesize(note["topics"])
                analyzer.analyze(segments)
                analyzer.consolidate(segments)
            for payload in requests[:2]:
                self.assertIn("Top 1%", payload["messages"][0]["content"])
                self.assertIn("English Annotation", payload["messages"][0]["content"])
                self.assertNotIn("detail 不超过 160 字", payload["messages"][0]["content"])
                self.assertNotIn("overview 不超过 160 字", payload["messages"][0]["content"])
            for payload in requests[2:]:
                self.assertNotIn("Top 1%", payload["messages"][0]["content"])
            self.assertEqual(note["topics"][0]["detail"], detail)
            self.assertEqual(note["topics"][0]["title"], title)
            self.assertEqual(note["title"], title)
            self.assertEqual(note["overview"], overview)
            self.assertEqual(note["topics"][0]["material_refs"][0]["quote"], quote)

            store = Store(settings.root)
            try:
                sid = store.create({"mode": "subtitle", "course_title": "操作系统"})["id"]
                store.update(sid, segments=segments, summary=note)
                exported = (store.directory(sid) / "notes.md").read_text(encoding="utf-8")
                self.assertIn("\n\n" + detail + "\n", exported)
                self.assertEqual(store.get(sid)["summary"]["topics"][0]["detail"], detail)
            finally:
                store.db.close()

    def test_changed_prompt_regenerates_notes_instead_of_reusing_old_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            store = Store(settings.root)
            manager = Manager(settings, store)
            try:
                sid = store.create({"mode": "subtitle"})["id"]
                store.update(sid, stopped=True, segments=[{"id": "s1", "start": 0, "end": 5, "text": "条件变量"}])
                with patch.object(manager.analyzer, "analyze", return_value={"events": [], "topics": [], "overview": "讲义"}) as analyze, \
                     patch.object(manager.analyzer, "synthesize", return_value={"title": "主题", "overview": "概览", "groups": []}), \
                     patch.object(manager.analyzer, "consolidate", return_value=[]):
                    for version in ("first-prompt", "first-prompt", "new-prompt"):
                        with patch("assistant_service.manager.NOTES_PROMPT_VERSION", version):
                            store.enqueue(sid, "summary", {}, lane="analysis")
                            manager.execute(store.claim("analysis"))
                    self.assertEqual(analyze.call_count, 2)
                    self.assertEqual(store.get(sid)["analysis_status"], "complete")
            finally:
                manager.close()
                store.db.close()


if __name__ == "__main__":
    unittest.main()
