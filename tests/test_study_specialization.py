from __future__ import annotations

import io
import json
import tempfile
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

from assistant_service.study_agent.classroom_search import find_classroom_mentions, location
from assistant_service.study_agent.daily import daily_plan, is_daily_question, requested_minutes
from assistant_service.study_agent.dialogue import converse
from assistant_service.study_agent.ingest import sync_sessions
from assistant_service.study_agent.knowledge import set_policy
from assistant_service.study_agent.mcp import Server, serve
from assistant_service.study_agent.planner import TZ
from assistant_service.study_agent.reasoner import run_agent
from assistant_service.study_agent.storage import StudyStore
from scripts.prepare_workbuddy import configuration, export_course
from fastapi.testclient import TestClient
from assistant_service.app import create_app
from assistant_service.settings import Settings
from assistant_service.store import Store


class SpecializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StudyStore(self.root)
        self.now = datetime(2026, 10, 29, 19, tzinfo=TZ).timestamp()
        self.sessions = [self.lesson("a", "1", "2026-09-15第2-4节", "条件变量的 wait 必须在循环里检查条件。"),
                         self.lesson("b", "2", "2026-09-22第2-4节", "A condition variable is used with a mutex.")]
        sync_sessions(self.store, self.sessions)

    def tearDown(self):
        self.temp.cleanup()

    def lesson(self, sid, sub, title, text, basis="video", course="操作系统"):
        return {"id": sid, "course_title": course, "title": title, "mode": "subtitle", "time_basis": basis,
            "course_id": "10", "sub_id": sub, "page_url": f"https://video.jw.scut.edu.cn/livingroom?course_id=10&sub_id={sub}&tenant_code=21&token=must-not-copy",
            "segments": [{"id": "1", "start": 50, "end": 60, "text": "先回顾一下上节课的同步问题。"},
                         {"id": "2", "start": 300, "end": 308, "text": text},
                         {"id": "3", "start": 309, "end": 320, "text": "接着看一个生产者消费者的例子。"}]}

    def reference(self):
        source = self.store.source(origin="reference", course="操作系统", title="同步课件", kind="material", fingerprint="r",
            chunks=[{"text": "条件变量的 wait 需要在循环中重新检查谓词。", "locator": {"page": 3}}])
        return {"evidence_id": source["id"] + ":0", "quote": "条件变量的 wait 需要在循环中重新检查谓词。"}

    def task(self, title="同步作业", deadline="2026-10-30", **extra):
        return self.store.save("task", {"course": "操作系统", "title": title, "deadline": deadline, "estimate_max": 90,
            "status": "accepted", "completed": False, **extra})

    def review(self, topic="条件变量的谓词循环", **extra):
        return self.store.save("review", {"course": "操作系统", "topic": topic, "due_at": self.now - 86400, "status": "active",
            "citations": [self.reference()], "attempts": [{"rating": 1, "at": self.now - 172800}], **extra})

    def test_cross_lesson_alias_and_quote_exact_time(self):
        value = find_classroom_mentions(self.store, "操作系统", "老师在哪讲过条件变量？")
        self.assertEqual(len(value["items"]), 2)
        self.assertEqual({x["lesson"] for x in value["items"]}, {s["title"] for s in self.sessions})
        for item in value["items"]:
            self.assertEqual(item["start"], 300)
            self.assertEqual(item["end"], 308)
            self.assertEqual(item["precision"], "segment")
            self.assertIn("note_play=300", item["playback_url"])
            self.assertNotIn("token", item["playback_url"])
            self.assertIn(item["quote"], self.store.evidence(item["evidence_id"])["text"])

    def test_quote_across_segments_covers_both_timestamps(self):
        item = find_classroom_mentions(self.store, "操作系统", "条件变量")["items"][0]
        e = self.store.evidence(item["evidence_id"])
        quote = "\n".join(e["text"].splitlines()[1:])
        where = location(e, quote)
        self.assertEqual((where["start"], where["end"]), (300,320))
        e["source_meta"]["sub_id"] = "999"
        self.assertEqual(location(e, quote)["playback_url"], "")

    def test_scope_exclusion_and_old_citations(self):
        item = find_classroom_mentions(self.store, "操作系统", "条件变量")["items"][0]
        e = self.store.evidence(item["evidence_id"])
        set_policy(self.store, e["source_id"], role="transcript", excluded=True)
        self.assertEqual(len(find_classroom_mentions(self.store, "操作系统", "条件变量")["items"]), 1)
        self.assertEqual(find_classroom_mentions(self.store, "神经科学", "条件变量")["items"], [])
        self.assertEqual(location(self.store.evidence(e["id"]))["playback_url"], "")

    def test_capture_and_missing_source_do_not_invent_video_positions(self):
        sync_sessions(self.store, [self.lesson("c", "3", "直播录音", "条件变量要与锁配合使用", "capture")])
        value = find_classroom_mentions(self.store, "操作系统", "条件变量")["items"][0]
        self.assertEqual(value["playback_url"], "")
        self.assertEqual(value["time_basis"], "capture")
        e = self.store.evidence(value["evidence_id"])
        with self.assertRaisesRegex(ValueError, "引文"):
            location(e, "模型编造的原句")
        e["source_meta"]["page_url"] = "https://example.test/livingroom?course_id=10&sub_id=3"
        e["locator"]["time_basis"] = "video"
        self.assertEqual(location(e)["playback_url"], "")

    def test_model_failure_keeps_local_mentions_and_never_fabricates_explanation(self):
        class Offline:
            def complete(self, *args): raise ValueError("测试离线")
        result = run_agent(self.store, Offline(), course="操作系统", question="老师在哪讲过条件变量？", purpose="classroom")
        self.assertEqual(result["status"], "sources_only")
        self.assertEqual(len(result["classroom_matches"]), 2)
        self.assertEqual(result["claims"], [])

    def test_no_match_is_not_inferred_from_textbook_or_old_focus(self):
        e = self.store.evidence(self.reference()["evidence_id"])
        class NoCall:
            def complete(self, *args): self.fail("should not call")
        result = run_agent(self.store, NoCall(), course="操作系统", question="老师在哪讲过量子纠缠？", purpose="classroom", focus=[e])
        self.assertEqual(result["status"], "needs_sources")

    def test_ingestion_migration_preserves_old_source_and_is_idempotent(self):
        initial = len(self.store.sources())
        sync_sessions(self.store, self.sessions)
        self.assertEqual(len(self.store.sources()), initial)
        old = self.store.sources()[0]
        self.sessions[1]["page_url"] = "https://video-jw-443.webvpn.scut.edu.cn/livingroom?course_id=10&sub_id=2"
        sync_sessions(self.store, self.sessions)
        self.assertFalse(self.store.evidence(old["id"] + ":0")["active"])

    def test_budget_urgent_tasks_weakness_due_reviews_and_unknown_dates(self):
        urgent = self.task()
        weak = self.review()
        recall = self.review("进程与线程", attempts=[{"rating": 4, "at": self.now - 86400}])
        self.task("不知道什么时候交的阅读", deadline=None)
        value = daily_plan(self.store, 30, now=self.now)
        self.assertEqual([x["id"] for x in value["steps"]], [urgent["id"], weak["id"], recall["id"]])
        self.assertEqual(value["used_minutes"], 28)
        self.assertEqual(value["buffer_minutes"], 2)
        self.assertTrue(value["steps"][0]["partial"])
        self.assertEqual(len(value["deferred"]), 1)

    def test_stale_paused_completed_and_snoozed_are_not_scheduled(self):
        self.task(completed=True)
        self.review(status="paused")
        self.review("未来复习", due_at=self.now + 86400)
        r = self.review("已停用资料")
        e = self.store.evidence(r["citations"][0]["evidence_id"])
        set_policy(self.store, e["source_id"], role="reference", excluded=True)
        result = daily_plan(self.store, 30, now=self.now)
        self.assertEqual(result["steps"], [])
        self.assertTrue(result["needs_source_check"])

    def test_general_chat_plan_ignores_selected_course_and_needs_no_model(self):
        self.task(course="数学", deadline="2026-10-30")
        result = converse(self.store, None, self.root, conversation="test", client_id="a", course="操作系统",
                          question="今晚只有30分钟，先学什么？")
        self.assertEqual(result["course"], "")
        self.assertEqual(result["result"]["daily_plan"]["steps"][0]["course"], "数学")
        second = converse(self.store, None, self.root, conversation="test", client_id="a", course="操作系统",
                          question="今晚只有30分钟，先学什么？")
        self.assertEqual(result["id"], second["id"])
        self.assertEqual(len(self.store.list("turn")), 1)
        self.task(course="操作系统")
        scoped = converse(self.store, None, self.root, conversation="test", client_id="b", course="操作系统", question="今晚只复习数学，只有30分钟，先学什么？")
        self.assertEqual(scoped["result"]["daily_plan"]["course"], "数学")
        self.assertTrue(all(s["course"] == "数学" for s in scoped["result"]["daily_plan"]["steps"]))

    def test_parser_small_budgets_and_explicit_scope(self):
        self.assertTrue(is_daily_question("今晚只有30分钟，先学什么"))
        self.assertFalse(is_daily_question("每天复习30分钟"))
        for text, minutes in [("今晚半小时",30),("三十分钟",30),("20分钟",20),("一个小时",60),("1.5小时",90)]:
            self.assertEqual(requested_minutes(text), minutes)
        self.task()
        for budget in (1, 2, 3, 6, 15, 30, 90):
            value = daily_plan(self.store, budget, now=self.now)
            self.assertLessEqual(sum(x["minutes"] for x in value["steps"]), budget)
        self.assertEqual(daily_plan(self.store, 30, "数学", self.now)["steps"], [])

    def test_mcp_tools_validate_types_and_share_results(self):
        server = Server(self.store)
        reply = server.handle({"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{"protocolVersion":"2024-11-05"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")
        server.handle({"jsonrpc":"2.0", "method":"notifications/initialized"})
        def call(name, args):
            return server.handle({"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":{"name":name,"arguments":args}})
        for args in ({"minutes":True}, {"minutes":0}, {"minutes":"30"}, {"minutes":30,"path":"x"}):
            self.assertEqual(call("plan_today_review",args)["error"]["code"], -32602)
        self.assertFalse(call("plan_today_review", {"minutes":30})["result"]["isError"])
        answer = json.loads(call("find_classroom_mentions", {"course":"操作系统","query":"条件变量"})["result"]["content"][0]["text"])
        self.assertEqual(len(answer["items"]), 2)

    def test_workbuddy_config_and_export_do_not_contain_secrets_or_origins(self):
        cfg = configuration(self.root)
        self.assertEqual(cfg["mcpServers"]["scut-study"]["type"], "stdio")
        self.assertEqual(cfg["mcpServers"]["scut-study"]["command"], str(Path(sys.executable).absolute()))
        self.assertNotIn("env", cfg["mcpServers"]["scut-study"])
        self.assertEqual(export_course(self.root, self.root, "操作系统"), 2)
        folder = self.root / "knowledge-snapshot"
        combined = "\n".join(p.read_text("utf-8") for p in folder.iterdir())
        self.assertNotIn("must-not-copy", combined)
        self.assertNotIn('"origin"', combined)
        self.assertIn('"fingerprint"', combined)
        self.assertIn('"reliability"', combined)
        with self.assertRaises(FileExistsError):
            export_course(self.root, self.root, "操作系统")

    def test_cold_start_offers_one_diagnostic_not_invented_weakness(self):
        plan = daily_plan(self.store, 30, now=self.now)
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["kind"], "diagnostic")
        self.assertEqual(plan["used_minutes"], 5)
        self.assertIn("还没有", plan["warnings"][0])

    def test_new_routes_are_authenticated_and_ui_script_is_served(self):
        settings = Settings(self.root, vault_root=self.root / "vault")
        classroom = Store(self.root)
        class Quiet:
            def close(self): pass
        try:
            with TestClient(create_app(settings, classroom, Quiet(), run_workers=False)) as client:
                self.assertEqual(client.post("/api/study/today", json={"minutes":30}).status_code, 401)
                self.assertEqual(client.post("/api/study/classroom-search", json={"course":"操作系统","query":"条件变量"}).status_code, 401)
                client.headers["Authorization"] = "Bearer " + settings.data["token"]
                self.assertEqual(client.get("/study-specialized.js").status_code, 200)
                self.assertEqual(client.post("/api/study/today", json={"minutes":False}).status_code, 422)
                answer = client.post("/api/study/chat", json={"client_id":"daily-api", "question":"今晚只有30分钟，先学什么？"})
                self.assertEqual(answer.status_code, 200)
                self.assertIn("daily_plan", answer.json()["result"])
                self.assertNotIn(settings.data["token"], answer.text)
        finally:
            classroom.db.close()

    def test_generated_mcp_configuration_runs_from_unrelated_working_directory(self):
        cfg = configuration(self.root)["mcpServers"]["scut-study"]
        messages = [
            {"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{"protocolVersion":"2025-11-25"}},
            {"jsonrpc":"2.0", "method":"notifications/initialized"},
            {"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":{"name":"find_classroom_mentions", "arguments":{"course":"操作系统", "query":"条件变量"}}}]
        result = subprocess.run([cfg["command"], *cfg["args"]], input="\n".join(json.dumps(m) for m in messages)+"\n", text=True,
                                encoding="utf-8", capture_output=True, timeout=15, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(lines), 2)
        answer = json.loads(lines[1]["result"]["content"][0]["text"])
        self.assertEqual(len(answer["items"]), 2)

    def test_today_diagnostic_feedback_immediately_changes_priority(self):
        p = self.store.save("practice", {"course":"操作系统", "question":"条件变量为什么需要循环检查？", "citations":[self.reference()],
            "attempts":[{"rating":1,"at":self.now-60}]})
        result = daily_plan(self.store, 25, now=self.now)
        self.assertEqual(result["steps"][0]["id"], p["id"])
        self.assertIn("刚才", result["steps"][0]["action"])
        self.store.save("practice", {**p, "attempts":[{"rating":3,"at":self.now}]}, "new-attempt")
        self.assertEqual(daily_plan(self.store, 25, now=self.now)["steps"], [])

    def test_paused_review_does_not_reappear_as_a_weak_practice(self):
        p = self.store.save("practice", {"course":"操作系统", "question":"条件变量为什么需要循环检查？", "citations":[self.reference()],
            "attempts":[{"rating":1,"at":self.now-60}]})
        self.review(p["question"], source=p["id"], status="paused")
        self.assertEqual(daily_plan(self.store, 30, now=self.now)["steps"], [])


if __name__ == "__main__":
    unittest.main()
