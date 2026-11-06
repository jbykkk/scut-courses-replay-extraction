from __future__ import annotations

import json
import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from assistant_service.app import create_app
from assistant_service.settings import Settings
from assistant_service.store import Store
from assistant_service.study_agent.api import ClassSlot, Decide
from assistant_service.study_agent.ingest import extract, import_file, inventory, sync_sessions, validate_visuals
from assistant_service.study_agent.planner import calendar, make_plan, review_interval
from assistant_service.study_agent.reasoner import Final, check_citations, run_agent
from assistant_service.study_agent.storage import StudyStore
from assistant_service.study_agent.timetable import expand_timetable, week_numbers, extract_timetable
from assistant_service.study_agent.mcp import Server, serve


class FakeProvider:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def complete(self, messages, timeout):
        self.calls.append((messages, timeout))
        return json.dumps(self.replies[min(len(self.calls) - 1, len(self.replies) - 1)], ensure_ascii=False)


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.study = StudyStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, text="Project requirement: DDL: TBD. Submit code and a report.", course="3D", origin="sample", fingerprint="1"):
        value = self.study.source(origin=origin, course=course, title="Project requirement", kind="material", fingerprint=fingerprint,
                                  chunks=[{"text": text, "locator": {"page": 1}}])
        return self.study.evidence(value["id"] + ":0")

    def test_versions_deduplicate_keep_old_citations_and_isolate_courses(self):
        old = self.source()
        self.source(fingerprint="2", text="DDL: now updated")
        self.assertFalse(self.study.evidence(old["id"])["active"])
        self.assertEqual(len(self.study.sources()), 1)
        duplicate = self.study.source(origin="another-copy", course="3D", title="copy", kind="material", fingerprint="2", chunks=[])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.study.search("DDL", "OS"), [])
        self.assertEqual(len(self.study.search("DDL", "3D")), 1)

    def test_ingestion_does_not_walk_environments_or_leave_registered_root(self):
        (self.root / "course.txt").write_text("binary semaphore controls mutual exclusion", encoding="utf-8")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "package.txt").write_text("not a course", encoding="utf-8")
        (self.root / "credentials.txt").write_text("must not import", encoding="utf-8")
        found = inventory(self.root)
        self.assertEqual([f["path"] for f in found["files"]], ["course.txt"])
        with self.assertRaises(ValueError):
            import_file(self.study, self.root, "../outside.txt", "OS")
        with self.assertRaises(ValueError):
            import_file(self.study, self.root, ".venv/package.txt", "OS")
        first = import_file(self.study, self.root, "course.txt", "OS")
        self.assertFalse(first["duplicate"])
        self.assertTrue(import_file(self.study, self.root, "course.txt", "OS")["duplicate"])

    def test_pptx_reads_text_and_preserves_slide_number(self):
        path = self.root / "lesson.pptx"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("ppt/slides/slide2.xml", '<a:r xmlns:a="urn:x"><a:t>Second slide</a:t></a:r>')
            z.writestr("ppt/slides/slide1.xml", '<a:r xmlns:a="urn:x"><a:t>First slide</a:t></a:r>')
        chunks = extract(path)
        self.assertEqual(chunks[0]["text"], "First slide")
        self.assertEqual(chunks[1]["locator"]["slide"], 2)

    def test_empty_text_is_not_marked_success(self):
        path = self.root / "scan.txt"
        path.write_text(" \n")
        with self.assertRaisesRegex(ValueError, "未提取"):
            import_file(self.study, self.root, "scan.txt", "OS")
        self.assertEqual(self.study.sources(), [])

    def test_tbd_stays_unknown_and_confirmation_is_not_automatic(self):
        e = self.source()
        citation = {"evidence_id": e["id"], "quote": "DDL: TBD"}
        output = {"action": "finish", "claims": [{"text": "截止日期尚未确定", "citations": [citation]}],
                  "proposals": [{"kind": "task", "title": "准备项目报告", "detail": "提交代码与报告", "due_text": "TBD", "citations": [citation]}]}
        provider = FakeProvider([output])
        result = run_agent(self.study, provider, course="3D", question="Project DDL")
        self.assertEqual(result["proposals"][0]["status"], "pending")
        self.assertEqual(self.study.list("task"), [])
        self.assertNotIn("deadline", result["proposals"][0])
        pid = result["proposals"][0]["id"]
        self.study.save("proposal", {**result["proposals"][0], "status": "accepted"}, pid)
        again = run_agent(self.study, provider, course="3D", question="Project DDL")
        self.assertEqual(again["proposals"][0]["status"], "accepted")
        self.assertEqual(len(self.study.list("proposal")), 1)

    def test_bad_citations_fail_with_bounded_repair_and_no_writes(self):
        e = self.source()
        provider = FakeProvider([{"action": "finish", "claims": [{"text": "假的截止日期", "citations": [{"evidence_id": e["id"], "quote": "Tomorrow 9am"}]}]}])
        with self.assertRaisesRegex(ValueError, "引文"):
            run_agent(self.study, provider, course="3D", question="Project DDL")
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(self.study.list("proposal"), [])

    def test_correction_needs_two_sources_and_does_not_modify_either(self):
        e = self.source(text="The synaps is a connection.", course="Neuro")
        c = {"evidence_id": e["id"], "quote": "synaps"}
        proposal = {"action": "finish", "proposals": [{"kind": "correction", "title": "术语核对", "detail": "synapse", "citations": [c]}]}
        with self.assertRaises(ValueError):
            check_citations(Final.model_validate(proposal), {e["id"]: e})
        other = self.source(text="A synapse connects neurons.", course="Neuro", origin="book", fingerprint="2")
        proposal["proposals"][0]["citations"].append({"evidence_id": other["id"], "quote": "synapse"})
        check_citations(Final.model_validate(proposal), {e["id"]: e, other["id"]: other})
        self.assertEqual(self.study.evidence(e["id"])["text"], "The synaps is a connection.")

    def test_pdf_wrapping_recovers_the_actual_quote_without_changing_words(self):
        e = self.source(text="They can send\n signals to each other through synapses.")
        value = Final.model_validate({"action": "finish", "claims": [{"text": "通信", "citations": [{"evidence_id": e["id"], "quote": "send signals to each other"}]}]})
        check_citations(value, {e["id"]: e})
        self.assertEqual(value.claims[0].citations[0].quote, "send\n signals to each other")
        value.claims[0].citations[0].quote = "send no signals to each other"
        with self.assertRaises(ValueError):
            check_citations(value, {e["id"]: e})

    def test_mcp_declares_bounded_writes_and_is_framed_as_json_lines(self):
        e = self.source()
        server = Server(self.study)
        self.assertIn("error", server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_course_sources", "arguments": {"course": "3D", "query": "DDL"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "execute", "arguments": {}}}]
        output = io.StringIO()
        serve(self.study, io.StringIO("\n".join(json.dumps(m) for m in messages)), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 4)
        tools = responses[1]["result"]["tools"]
        self.assertEqual({t["name"] for t in tools if not t["annotations"]["readOnlyHint"]},
                         {"search_public_references", "read_public_reference", "open_learning_session", "record_learning_progress", "refresh_course_index"})
        self.assertTrue(all(not t["annotations"]["destructiveHint"] for t in tools))
        self.assertIn(e["id"], responses[2]["result"]["content"][0]["text"])
        self.assertEqual(responses[3]["error"]["code"], -32602)

    def test_arbitrary_tools_are_not_executed_and_search_is_course_bound(self):
        self.source()
        self.source(text="private other course", course="OS", origin="os", fingerprint="other")
        provider = FakeProvider([{"action": "execute", "path": "secrets"}])
        with self.assertRaises(ValueError):
            run_agent(self.study, provider, course="3D", question="Project DDL")
        self.assertNotIn("private other course", str(provider.calls))

    def test_search_loop_and_practice_hide_reference_answer_until_attempt(self):
        e = self.source()
        provider = FakeProvider([{"action": "search", "query": "report"}, {"action": "finish", "exercise": {
            "question": "项目有哪些交付物？", "expected_answer": "code and a report", "citations": [{"evidence_id": e["id"], "quote": "code and a report"}]}}])
        result = run_agent(self.study, provider, course="3D", question="Project", purpose="practice")
        self.assertEqual(len(provider.calls), 2)
        self.assertNotIn("expected_answer", result["exercise"])
        self.assertEqual(len(self.study.list("practice")), 1)

    def test_sync_removes_deleted_and_superseded_sessions_from_retrieval(self):
        s = {"id": "a", "course_title": "OS", "title": "lecture", "mode": "subtitle", "segments": [{"id": "s1", "start": 10, "end": 20, "text": "mutual exclusion"}]}
        sync_sessions(self.study, [s])
        self.assertEqual(self.study.search("mutual", "OS")[0]["locator"]["start"], 10)
        sync_sessions(self.study, [{**s, "deleted_at": 1}])
        self.assertEqual(self.study.search("mutual", "OS"), [])

    def test_plan_respects_busy_time_deadline_and_capacity(self):
        tasks = [{"id": "a", "status": "accepted", "course": "OS", "title": "Exam study", "estimate_min": 90, "estimate_max": 180, "deadline": "2026-10-28"},
                 {"id": "b", "status": "accepted", "course": "3D", "title": "Project", "estimate_max": 60, "deadline": None}]
        plan = make_plan(tasks, start=date(2026, 10, 28), days=2, daily_minutes=90, start_hour=19,
                         busy=[{"start": "2026-10-28T19:00:00+08:00", "end": "2026-10-28T19:45:00+08:00"}])
        self.assertTrue(all(b["start"] >= "2026-10-28T19:50" for b in plan["blocks"]))
        self.assertTrue(all(b["start"].startswith("2026-10-28") for b in plan["blocks"] if b["task_id"] == "a"))
        self.assertTrue(any("截止时间未确认" in r["text"] for r in plan["risks"]))
        self.assertTrue(any("排不完" in r["text"] for r in plan["risks"]))
        with self.assertRaises(ValueError):
            calendar(plan)
        text = calendar({**plan, "id": "p1", "status": "accepted"})
        self.assertIn("BEGIN:VEVENT", text)
        self.assertTrue(all(len(line.encode()) <= 75 for line in text.split("\r\n")))
        self.assertNotIn("BEGIN:VALARM", text)

    def test_heavy_workload_warns_well_before_exam(self):
        t = {"id": "a", "status": "accepted", "course": "OS", "title": "补齐后复习", "estimate_max": 1300, "deadline": "2026-11-18"}
        plan = make_plan([t], start=date(2026, 10, 28), daily_minutes=60)
        self.assertTrue(any("建议本周开始" in r["text"] for r in plan["risks"]))

    def test_timetable_weekdays_parity_and_unconfirmed_start(self):
        self.assertEqual(week_numbers("1-2周,7-8周,11-13周(单)"), [1, 2, 7, 8, 11, 13])
        slot = {"course": "OS", "weekday": 1, "first_period": 2, "last_period": 4, "weeks": [1, 2]}
        periods = [{"number": 2, "start": "09:40", "end": "10:25"}, {"number": 4, "start": "11:30", "end": "12:15"}]
        expanded = expand_timetable([slot], date(2026, 8, 31), periods)
        self.assertEqual(expanded["busy"][0]["start"], "2026-09-01T09:40:00+08:00")
        with self.assertRaises(ValueError):
            expand_timetable([slot], date(2026, 9, 1), periods)
        with self.assertRaises(ValueError):
            ClassSlot.model_validate({**slot, "weeks": [0]})

    def test_wrapped_credits_do_not_relabel_the_next_course(self):
        try:
            import pdfplumber
        except ImportError:
            self.skipTest("study optional dependency not installed")
        cell = "操作系统\n(1-4节)3-4周/校区:测试/学\n分:3.5\n工程数学\n(3-4节)5-8周/校区:测试"
        row = [None, "1", "", "", cell, "", "", "", ""]
        with patch.object(pdfplumber, "open") as mocked:
            mocked.return_value.__enter__.return_value.pages = [type("Page", (), {"extract_tables": lambda _: [[row]]})()]
            result = extract_timetable(self.root / "schedule.pdf")
        self.assertEqual([s["course"] for s in result["candidates"]], ["操作系统", "工程数学"])
        self.assertTrue(all(s["weekday"] == 2 for s in result["candidates"]))

    def test_visual_resource_url_and_time_scope(self):
        page = "https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=2"
        item = {"url": "https://video.jw.scut.edu.cn/play/ai3/ppt/20261027/abc/123.jpg", "seconds": 174}
        self.assertEqual(validate_visuals(page, [item, item]), [item])
        with self.assertRaises(ValueError):
            validate_visuals(page, [{**item, "url": "http://127.0.0.1/private"}])

    def test_self_rating_not_unbounded_mastery(self):
        self.assertEqual(review_interval([], 1), 1)
        self.assertEqual(review_interval([], 4), 2)
        self.assertEqual(review_interval([{"rating": 4}] * 20, 4), 21)
        with self.assertRaises(ValueError):
            Decide(status="accepted", estimate_min=100, estimate_max=10)


class StubManager:
    def close(self):
        pass


class StudyAPITests(unittest.TestCase):
    def test_auth_proposals_calendar_and_hidden_answers(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(Path(root))
            store = Store(settings.root)
            try:
                with TestClient(create_app(settings, store, StubManager(), run_workers=False)) as client:
                    self.assertEqual(client.get("/api/study/overview").status_code, 401)
                    client.headers["Authorization"] = "Bearer " + settings.data["token"]
                    self.assertEqual(client.get("/api/study/overview").status_code, 200)
                    self.assertNotIn(settings.data["token"], client.get("/api/study/overview").text)
                    task = client.post("/api/study/tasks", json={"course": "OS", "title": "复习", "estimate_min": 30, "estimate_max": 40}).json()
                    self.assertEqual(task["status"], "accepted")
                    plan = client.post("/api/study/plans", json={"start": "2026-10-28"}).json()
                    self.assertEqual(client.get(f"/api/study/plans/{plan['id']}/calendar").status_code, 400)
                    self.assertEqual(client.post(f"/api/study/plans/{plan['id']}/confirm").status_code, 200)
                    self.assertIn("VCALENDAR", client.get(f"/api/study/plans/{plan['id']}/calendar").text)
                    study = StudyStore(settings.root)
                    proposal = study.save("proposal", {"course": "OS", "title": "完成大项目", "detail": "需要明确工作量",
                                                        "proposal_kind": "task", "status": "pending", "citations": []})
                    self.assertEqual(client.post(f"/api/study/proposals/{proposal['id']}", json={"status": "accepted"}).status_code, 400)
                    self.assertEqual(study.get(proposal["id"])["status"], "pending")
                    requirement = client.post("/api/study/requirements", json={"course": "OS", "detail": "实验报告在课程平台提交"})
                    self.assertEqual(requirement.status_code, 200)
                    self.assertTrue(study.search("实验报告", "OS"))
                    p = study.save("practice", {"course": "OS", "question": "test", "expected_answer": "secret-answer", "attempts": [], "due_at": 0})
                    self.assertNotIn("secret-answer", client.get("/api/study/overview").text)
                    result = client.post(f"/api/study/practice/{p['id']}/attempt", json={"answer": "my attempt", "rating": 2}).json()
                    self.assertEqual(result["expected_answer"], "secret-answer")
                    self.assertEqual(client.get("/study.html").status_code, 200)
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
