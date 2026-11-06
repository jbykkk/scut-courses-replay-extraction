import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from assistant_service.analysis import Analyzer, AnalysisPaused, OUTLINE_BATCH_CHARS
from assistant_service.manager import Manager
from assistant_service.settings import Settings, atomic_json
from assistant_service.store import Store
from assistant_service.telemetry import RuntimeLog


def topics(count=18, detail_size=2500):
    return [{"title": f"知识点 {i}", "detail": str(i) + "定义、推导和例子。" * (detail_size // 8),
             "start": i * 10, "end": i * 10 + 5, "segment_ids": [f"speech-{i}"]}
            for i in range(count)]


def response(rows):
    content = {"title": "全课主题", "overview": "知识主线", "events": [],
               "topics": [{"title": "知识归组", "detail": "核心概念之间的联系。",
                           "segment_ids": [r["id"] for r in rows]}]}
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})


class OutlineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root)
        key = patch.object(self.settings, "openai_key", return_value="test-key")
        key.start()
        self.addCleanup(key.stop)
        self.settings.data["llm_provider"] = "openai"

    def analyzer(self, handler):
        analyzer = Analyzer(self.settings, httpx.MockTransport(handler))
        self.addCleanup(analyzer.close)
        return analyzer

    def test_large_lecture_is_bounded_and_maps_back_to_every_original_topic(self):
        original = topics()
        before = copy.deepcopy(original)
        requests = []

        def handler(request):
            payload = json.loads(request.content)
            rows = json.loads(payload["messages"][1]["content"])["segments"]
            self.assertLessEqual(len(json.dumps(rows, ensure_ascii=False, separators=(",", ":"))), OUTLINE_BATCH_CHARS)
            self.assertEqual(payload["max_completion_tokens"], 6000)
            requests.append(rows)
            return response(rows)

        result = self.analyzer(handler).synthesize(original)
        self.assertGreater(len(requests), 2)
        self.assertEqual([r["text"] for pack in requests[:-1] for r in pack],
                         [t["title"] + "：" + t["detail"] for t in original])
        self.assertEqual(result["groups"][0]["topic_indices"], list(range(len(original))))
        self.assertEqual(original, before)
        log = RuntimeLog(self.root)
        starts = [e["data"] for e in log.detail(log.list()[0]["id"])["events"] if e["kind"] == "model.start"]
        self.assertTrue(starts)
        self.assertTrue(all(e["phase"] == "classroom_outline" and e["max_output_tokens"] == 6000 for e in starts))

    def test_one_oversized_topic_is_split_without_losing_text_or_its_index(self):
        original = topics(1)
        original[0]["detail"] = '长讲义\n"\\' * 6000 + "END"
        first_level = []

        def handler(request):
            rows = json.loads(json.loads(request.content)["messages"][1]["content"])["segments"]
            self.assertLessEqual(len(json.dumps(rows, ensure_ascii=False, separators=(",", ":"))), OUTLINE_BATCH_CHARS)
            if not all(r["text"].startswith("知识归组：") for r in rows):
                first_level.extend(r["text"] for r in rows)
            return response(rows)

        result = self.analyzer(handler).synthesize(original)
        self.assertEqual("".join(first_level), original[0]["title"] + "：" + original[0]["detail"])
        self.assertEqual(result["groups"][0]["topic_indices"], [0])

    def test_manager_restart_reuses_segments_and_successful_outline_batches(self):
        store = Store(self.root)
        self.addCleanup(store.db.close)
        sid = store.create({"mode": "subtitle"})["id"]
        store.update(sid, stopped=True, segments=[{"id": "speech", "start": 0, "end": 1, "text": "课程内容"}])
        original = topics()
        requests = []
        fail = True

        def handler(request):
            rows = json.loads(json.loads(request.content)["messages"][1]["content"])["segments"]
            requests.append(rows)
            if fail and len(requests) >= 2:
                raise httpx.ReadTimeout("slow outline", request=request)
            return response(rows)

        analyzer = self.analyzer(handler)
        manager = Manager(self.settings, store, analyzer=analyzer)
        mapped = {"overview": "完整分段笔记", "events": [], "topics": original}
        with patch.object(analyzer, "analyze", return_value=mapped), patch("assistant_service.analysis.time.sleep"):
            store.enqueue(sid, "summary", {}, lane="analysis")
            manager.execute(store.claim("analysis"))
        failed = store.get(sid)
        self.assertEqual(failed["analysis_stage"], "outline")
        self.assertEqual(failed["analysis_status"], "failed")
        self.assertIn("生成整课概览", failed["warning"])
        self.assertEqual(failed["summary"]["topics"], original)
        self.assertEqual(len(requests), 4)
        checkpoint = next((store.directory(sid) / "analysis-cache").glob("*.json"))
        self.assertEqual(len(json.loads(checkpoint.read_text(encoding="utf-8"))["outline-parts-v1"]), 1)

        first_batch = requests[0]
        requests.clear()
        fail = False
        # A new manager/analyzer reads checkpoints from disk, as after a restart.
        analyzer = self.analyzer(handler)
        manager = Manager(self.settings, store, analyzer=analyzer)
        with patch.object(analyzer, "analyze", side_effect=AssertionError("reuse detailed notes")), \
             patch.object(analyzer, "consolidate", return_value=[]):
            store.enqueue(sid, "summary", {}, lane="analysis")
            manager.execute(store.claim("analysis"))
        self.assertNotIn(first_batch, requests)
        self.assertEqual(store.get(sid)["analysis_status"], "complete")
        self.assertFalse(store.get(sid)["summary"]["partial"])
        self.assertEqual(store.get(sid)["summary"]["topics"], original)

    def test_pause_during_outline_keeps_completed_batches_for_resume(self):
        cache, requests = {}, []
        paused = False

        def handler(request):
            nonlocal paused
            rows = json.loads(json.loads(request.content)["messages"][1]["content"])["segments"]
            requests.append(rows)
            paused = len(requests) == 2
            return response(rows)

        def check():
            if paused:
                raise AnalysisPaused()

        path = self.root / "outline.json"
        analyzer = self.analyzer(handler)
        with self.assertRaises(AnalysisPaused):
            analyzer.synthesize(topics(), checkpoint=cache, save=lambda: atomic_json(path, cache), check=check)
        self.assertEqual(len(cache), 1)
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)
        requests.clear()
        paused = False
        # No pause callback on resume; the first successful batch is not sent again.
        result = analyzer.synthesize(topics(), checkpoint=cache)
        self.assertEqual(result["groups"][0]["topic_indices"], list(range(18)))


if __name__ == "__main__":
    unittest.main()
