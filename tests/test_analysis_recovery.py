import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from assistant_service.analysis import Analyzer, analysis_packs
from assistant_service.manager import Manager
from assistant_service.settings import Settings, atomic_json, protect
from assistant_service.store import Store


class AnalysisRecoveryTests(unittest.TestCase):
    def setUp(self):
        mock = patch.object(Analyzer, "consolidate", return_value=[])
        mock.start()
        self.addCleanup(mock.stop)

    def test_short_school_subtitles_are_bounded_without_omission(self):
        segments = [{"id": f"long-database-id-{i}", "start": i, "end": i + 1, "text": "操作系统"} for i in range(1418)]
        packs = analysis_packs(segments)
        self.assertGreater(len(packs), 10)
        self.assertLessEqual(max(map(len, packs)), 96)
        self.assertEqual([s for pack in packs for s in pack], segments)

    def test_failures_distinguish_connection_timeout_format_and_citation_errors(self):
        segments = [{"id": "speech", "start": 0, "end": 5, "text": "条件变量需要循环检查。"}]
        materials = [{"id": "evidence", "source_id": "source", "title": "slides.pdf", "locator": {"page": 1}, "text": "Use a while loop."}]
        for failure, message in (("connection", "连接失败或中断"), ("timeout", "请求超时"),
                                 ("format", "返回内容格式无效"), ("citation", "引用原文未通过核对")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(Path(tmp))
                calls = []
                def respond(request):
                    calls.append(request)
                    if failure == "connection":
                        raise httpx.ConnectError("connection failed", request=request)
                    if failure == "timeout":
                        raise httpx.ReadTimeout("generation timed out", request=request)
                    content = "invalid json" if failure == "format" else json.dumps({"topics": [{
                        "title": "条件变量", "segment_ids": ["s1"], "material_refs": [{"id": "m1", "quote": "Use an if statement."}]}]})
                    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
                analyzer = Analyzer(settings, httpx.MockTransport(respond))
                try:
                    with patch.object(settings, "key", return_value="test-key"), patch("assistant_service.analysis.time.sleep"):
                        with self.assertRaisesRegex(ValueError, message):
                            analyzer.analyze(segments, summary=True, materials=materials)
                    self.assertEqual(len(calls), 3)
                finally:
                    analyzer.close()

    def test_truncated_output_splits_and_restores_original_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            segments = [{"id": f"original-{i}", "start": i, "end": i + 1, "text": "作业周五提交"} for i in range(4)]
            requests = []

            def respond(request):
                data = json.loads(request.content)
                rows = json.loads(data["messages"][1]["content"])["segments"]
                requests.append(rows)
                self.assertNotIn("original-", data["messages"][1]["content"])
                if len(rows) > 2:
                    return httpx.Response(200, json={"choices": [{"finish_reason": "length"}]})
                content = {"overview": "作业", "events": [], "topics": [
                    {"title": "作业", "segment_ids": [s["id"] for s in rows]}]}
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})

            with patch.object(settings, "key", return_value="test-key"):
                result = Analyzer(settings, httpx.MockTransport(respond)).analyze(segments, summary=True)
            self.assertEqual([len(r) for r in requests], [4, 2, 2])
            self.assertEqual([i for t in result["topics"] for i in t["segment_ids"]], [s["id"] for s in segments])

    def test_failed_summary_resumes_completed_packs_after_manager_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            settings.data["analysis_parallelism"] = 1
            store = Store(settings.root)
            session = store.create({"mode": "subtitle", "analysis": True})
            sid = session["id"]
            store.update(sid, stopped=True, segments=[{"id": str(i), "start": i, "end": i + 1, "text": "内容" * 1700} for i in range(3)])
            manager = Manager(settings, store)
            good = {"overview": "已保存的概览", "events": [], "topics": []}
            store.enqueue(sid, "summary", {}, lane="analysis")
            with patch.object(manager.analyzer, "analyze", side_effect=[good, ValueError("暂时失败")]):
                manager.execute(store.claim("analysis"))
            self.assertEqual(store.get(sid)["analysis_done"], 1)
            self.assertTrue(store.get(sid)["summary"]["partial"])
            store.db.close()
            store = Store(settings.root)
            manager = Manager(settings, store)
            store.enqueue(sid, "summary", {}, lane="analysis")
            with patch.object(manager.analyzer, "analyze", return_value=good) as analyze:
                manager.execute(store.claim("analysis"))
            self.assertEqual(analyze.call_count, 2)
            self.assertEqual(store.get(sid)["analysis_status"], "complete")
            self.assertFalse(store.get(sid)["summary"]["partial"])
            self.assertEqual(store.get(sid)["warning"], "")
            # Selecting a different model invalidates the cache.
            settings.data["deepseek_model"] = "another-model"
            store.enqueue(sid, "summary", {}, lane="analysis")
            with patch.object(manager.analyzer, "analyze", return_value=good) as analyze:
                manager.execute(store.claim("analysis"))
            self.assertEqual(analyze.call_count, 3)
            store.db.close()

    def test_failed_outline_reuses_all_completed_map_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            store = Store(settings.root)
            sid = store.create({"mode": "subtitle"})["id"]
            store.update(sid, stopped=True, segments=[{"id": "a", "start": 0, "end": 1, "text": "进程同步"}])
            manager = Manager(settings, store)
            result = {"events": [], "overview": "进程同步", "topics": [{"title": "进程同步", "start": 0, "end": 1, "detail": "同步", "segment_ids": ["a"]}]}
            store.enqueue(sid, "summary", {}, lane="analysis")
            with patch.object(manager.analyzer, "analyze", return_value=result), patch.object(manager.analyzer, "synthesize", side_effect=ValueError("网络中断")):
                manager.execute(store.claim("analysis"))
            self.assertEqual(store.get(sid)["analysis_status"], "failed")
            self.assertTrue(store.get(sid)["summary"]["partial"])
            store.enqueue(sid, "summary", {}, lane="analysis")
            with patch.object(manager.analyzer, "analyze", side_effect=AssertionError("must reuse cache")), patch.object(manager.analyzer, "synthesize", return_value={"title": "进程同步", "overview": "概览", "groups": []}):
                manager.execute(store.claim("analysis"))
            self.assertEqual(store.get(sid)["summary"]["headline"], "进程同步")
            self.assertEqual(store.get(sid)["analysis_status"], "complete")
            store.db.close()

    def test_whole_lecture_outline_links_back_to_original_topic_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            topics = [{"title": "进程", "detail": "进程是资源分配单位", "start": 20, "end": 50},
                      {"title": "线程", "detail": "线程用于调度", "start": 60, "end": 90}]
            def respond(request):
                payload = json.loads(request.content)
                self.assertIn("整节课", payload["messages"][1]["content"])
                content = {"title": "进程与线程", "overview": "对比进程和线程", "events": [],
                           "topics": [{"title": "进程与线程", "detail": "资源分配和调度", "segment_ids": ["s1", "s2"]}]}
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})
            with patch.object(settings, "key", return_value="test-key"):
                result = Analyzer(settings, httpx.MockTransport(respond)).synthesize(topics)
            self.assertEqual(result["title"], "进程与线程")
            self.assertEqual(result["groups"][0]["topic_indices"], [0, 1])

    def test_service_shutdown_stops_future_requests_and_keeps_saved_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            settings.data["analysis_parallelism"] = 1
            store = Store(settings.root)
            sid = store.create({"mode": "subtitle", "analysis": True})["id"]
            store.update(sid, stopped=True, segments=[{"id": str(i), "start": i, "end": i + 1, "text": "字幕" * 1700} for i in range(3)])
            manager = Manager(settings, store)
            def result(*args, **kwargs):
                manager.shutdown.set()
                return {"overview": "已完成的部分", "events": [], "topics": []}
            store.enqueue(sid, "summary", {}, lane="analysis")
            with patch.object(manager.analyzer, "analyze", side_effect=result) as analyze:
                manager.execute(store.claim("analysis"))
            self.assertEqual(analyze.call_count, 1)
            self.assertEqual(store.get(sid)["analysis_status"], "running")
            self.assertTrue(store.get(sid)["summary"]["partial"])
            store.db.close()


@unittest.skipUnless(os.name == "nt", "Windows user-bound credential vault")
class CredentialPersistenceTests(unittest.TestCase):
    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
    def test_migration_survives_new_checkout_and_blank_settings_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root / "checkout-a", vault_root=root / "vault")
            secret = "synthetic-only-secret"
            settings.data["deepseek_key_encrypted"] = protect(secret)
            settings.save()
            migrated = Settings(root / "checkout-a", vault_root=root / "vault")
            self.assertEqual(migrated.key(), secret)
            self.assertNotIn(secret, migrated.vault.read_text())
            self.assertFalse(migrated.data["deepseek_key_encrypted"])
            moved = Settings(root / "checkout-b", vault_root=root / "vault")
            moved.update({"deepseek_key": "  ", "deepseek_model": "deepseek-v4-pro"})
            self.assertEqual(Settings(root / "checkout-b", vault_root=root / "vault").key(), secret)
            self.assertEqual(moved.public()["deepseek_key_status"], "saved")
            self.assertNotIn(secret, json.dumps(moved.public()))
            moved.update({"clear_deepseek_key": True})
            self.assertFalse(moved.key())

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
    def test_unreadable_vault_can_be_replaced_in_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            atomic_json(settings.vault, {"deepseek_key_encrypted": "invalid"})
            self.assertEqual(settings.public()["deepseek_key_status"], "unreadable")
            settings.update({"deepseek_key": "replacement-test-key"})
            self.assertTrue(settings.public()["deepseek_configured"])

    @patch.dict(os.environ, {"OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": ""})
    def test_openai_key_stays_independent_and_never_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root / "checkout", vault_root=root / "vault")
            settings.update({"openai_key": "synthetic-openai-secret", "llm_provider": "openai"})
            self.assertEqual(settings.openai_key(), "synthetic-openai-secret")
            self.assertNotIn("synthetic-openai-secret", settings.vault.read_text())
            self.assertNotIn("synthetic-openai-secret", json.dumps(settings.public()))
            self.assertEqual(settings.public()["openai_key_status"], "saved")
            # The two providers keep separate credentials; switching never leaks one key to the other.
            self.assertFalse(settings.key())
            self.assertEqual(settings.llm()["key"], "synthetic-openai-secret")
            reopened = Settings(root / "checkout", vault_root=root / "vault")
            self.assertEqual(reopened.openai_key(), "synthetic-openai-secret")
            settings.update({"clear_openai_key": True})
            self.assertFalse(settings.openai_key())

    @patch.dict(os.environ, {"OPENAI_API_KEY": "env-openai-key"})
    def test_openai_environment_variable_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            self.assertEqual(settings.openai_key(), "env-openai-key")
            self.assertEqual(settings.public()["openai_key_status"], "environment")
