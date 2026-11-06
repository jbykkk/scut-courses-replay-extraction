from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from PIL import Image
from fastapi.testclient import TestClient

from assistant_service.app import create_app
from assistant_service.settings import Settings
from assistant_service.store import Store
from assistant_service.mobile import MobileNotifier, make_pairing
from assistant_service.study_agent.attachments import accept_attachment, prepare_attachments
from assistant_service.study_agent.dialogue import converse, home
from assistant_service.study_agent.planner import TZ
from assistant_service.study_agent.reasoner import Final, check_citations, run_agent
from assistant_service.study_agent.reviews import ReviewScheduler, create_review, update_review
from assistant_service.study_agent.storage import StudyStore


class Provider:
    def __init__(self):
        self.calls = []

    def public(self):
        return {"configured": True, "vision": True}

    def complete(self, messages, timeout):
        self.calls.append(json.loads(json.dumps(messages)))
        if isinstance(messages[-1]["content"], list):
            return json.dumps({"readings": [{"text": "A process owns resources. Threads share process resources.", "uncertainty": ""}]})
        content = json.loads(messages[1]["content"])
        source = content["source_data"][0]
        citation = {"evidence_id": source["id"], "quote": source["text"][:80]}
        return json.dumps({"action": "finish", "reply": "我们接着看。", "claims": [{"text": "进程拥有资源，线程共享进程资源。", "citations": [citation]}],
            "next_question": "你更想比较资源归属还是调度？"})


class Phone:
    def __init__(self, enabled=True):
        self.calls, self.enabled = [], enabled

    def queue(self, kind, payload, dedupe):
        if self.enabled:
            self.calls.append((kind, payload, dedupe))
        return self.enabled


class StudyDialogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StudyStore(self.root)
        self.provider = Provider()
        self.source = self.store.source(origin="book", course="操作系统", title="进程与线程", kind="material", fingerprint="1",
            chunks=[{"text": "A process owns resources. Threads share process resources.", "locator": {"page": 1}},
                    {"text": "Scheduling chooses the next thread to execute.", "locator": {"page": 2}}])

    def tearDown(self):
        self.tmp.cleanup()

    def chat(self, text, cid="one", **kwargs):
        values = dict(conversation="main", client_id=cid, course="操作系统", question=text)
        values.update(kwargs)
        return converse(self.store, self.provider, self.root, **values)

    def test_conversation_survives_restart_and_followup_references_prior_question(self):
        self.chat("进程和线程有什么关系？")
        self.store = StudyStore(self.root)
        self.chat("为什么呢？", "two")
        context = json.loads(self.provider.calls[-1][1]["content"])
        self.assertEqual(context["conversation"][0]["question"], "进程和线程有什么关系？")
        self.assertEqual(len(home(self.store)["turns"]), 2)
        self.assertTrue(context["source_data"])

    def test_duplicate_request_does_not_repeat_model_or_task_creation(self):
        first = self.chat("进程和线程")
        second = self.chat("进程和线程")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.provider.calls), 1)
        with self.assertRaises(ValueError):
            self.chat("different question")

    def test_asks_only_course_and_keeps_input_when_context_missing(self):
        result = self.chat("我有一段没听懂", course="")
        self.assertEqual(result["result"]["status"], "needs_course")
        self.assertEqual(len(self.provider.calls), 0)
        self.assertEqual(result["question"], "我有一段没听懂")

    def test_explicit_course_switch_does_not_leak_previous_course_context(self):
        self.chat("进程和线程")
        self.store.source(origin="neuro", course="神经科学", title="Neuron", kind="material", fingerprint="2",
                          chunks=[{"text": "A neuron can transmit signals through synapses.", "locator": {"page": 1}}])
        self.chat("神经科学 neuron", "switch", course="")
        context = json.loads(self.provider.calls[-1][1]["content"])
        self.assertEqual(context["course"], "神经科学")
        self.assertEqual(context["conversation"], [])
        self.assertTrue(all(s["title"] == "Neuron" for s in context["source_data"]))

    def test_images_are_read_as_images_and_not_promoted_to_verified_original(self):
        stream = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(stream, format="PNG")
        item = accept_attachment(self.store, self.root, name="../../slide.png", content=base64.b64encode(stream.getvalue()).decode())
        result = self.chat("这张图讲了什么？", attachments=[item["id"]])
        self.assertTrue(result["result"]["input_images"])
        self.assertEqual(self.provider.calls[0][1]["content"][1]["type"], "image_url")
        self.assertTrue(any("图片内容由模型识读" in u for u in result["result"]["uncertainties"]))
        self.assertEqual(item["name"], "slide.png")
        self.assertNotIn("data:image", json.dumps(result))
        self.assertEqual(self.store.sources()[0]["kind"], "image_reading")

    def test_text_only_model_does_not_silently_ignore_image(self):
        stream = io.BytesIO()
        Image.new("RGB", (8, 8)).save(stream, format="PNG")
        item = accept_attachment(self.store, self.root, name="a.png", content=base64.b64encode(stream.getvalue()).decode())
        self.provider.public = lambda: {"vision": False}
        with self.assertRaisesRegex(ValueError, "不能读图"):
            prepare_attachments(self.store, self.provider, self.root, [item["id"]], "操作系统", "解释")
        self.assertEqual(len(self.provider.calls), 0)

    def test_upload_rejects_executable_and_invalid_bytes(self):
        with self.assertRaises(ValueError):
            accept_attachment(self.store, self.root, name="x.exe", content="AA==")
        with self.assertRaises(ValueError):
            accept_attachment(self.store, self.root, name="x.pdf", content="not base64")

    def test_practice_does_not_leak_answer_in_intro_or_create_a_homework_task(self):
        e = self.store.evidence(self.source["id"] + ":0")
        cite = [{"evidence_id": e["id"], "quote": "A process owns resources."}]
        class Leaky:
            def complete(inner, messages, timeout):
                return json.dumps({"action": "finish", "reply": "答案就是进程拥有资源", "claims": [{"text": "answer", "citations": cite}],
                    "proposals": [{"kind": "task", "title": "做题", "detail": "做这道题", "citations": cite}],
                    "exercise": {"question": "谁拥有资源？", "expected_answer": "secret-answer", "citations": cite}})
        value = run_agent(self.store, Leaky(), course="操作系统", question="process", purpose="practice")
        self.assertEqual(value["claims"], [])
        self.assertEqual(value["proposals"], [])
        self.assertNotIn("secret-answer", json.dumps(value))
        self.assertNotIn("答案就是", value["reply"])
        self.store.save("turn", {"conversation": "main", "course": "操作系统", "question": "test", "created_at": 1, "status": "ready", "result": value})
        self.assertNotIn("secret-answer", json.dumps(home(self.store)))
        practice = self.store.get(value["exercise"]["id"], "practice")
        self.store.save("practice", {**practice, "attempts": [{"answer": "my answer", "rating": 2}]}, practice["id"])
        self.assertIn("secret-answer", json.dumps(home(self.store)))

    def test_knowledge_connection_requires_distinct_evidence(self):
        e = self.store.evidence(self.source["id"] + ":0")
        citation = {"evidence_id": e["id"], "quote": "A process owns resources."}
        value = Final.model_validate({"action": "finish", "connections": [{"concept_a": "进程", "concept_b": "线程",
            "relationship": "资源的归属与共享", "citations": [citation, citation]}]})
        with self.assertRaisesRegex(ValueError, "两处"):
            check_citations(value, {e["id"]: e})

    def test_read_surrounding_context_is_scoped_to_provided_source(self):
        eid = self.source["id"] + ":0"
        outer = self.provider
        class Reader:
            def complete(inner, messages, timeout):
                if len(messages) == 2:
                    return json.dumps({"action": "read", "evidence_id": eid})
                return outer.complete(messages, timeout)
        result = run_agent(self.store, Reader(), course="操作系统", question="process")
        self.assertTrue(any(e["locator"]["page"] == 2 for e in result["evidence"]))
        self.assertTrue(any(t.get("tool") == "read_surrounding_context" for t in result["trace"]))

    def test_review_persists_deduplicates_and_respects_quiet_hours(self):
        now = datetime(2026, 10, 28, 23, tzinfo=TZ).timestamp()
        for i in range(3):
            r = create_review(self.store, course="操作系统", topic=f"topic {i}", citations=[], now=now - 172800)
            self.store.save("review", {**r, "due_at": now - 1}, r["id"])
        phone = Phone()
        scheduler = ReviewScheduler(self.root)
        scheduler.tick(phone, now=now)
        self.assertEqual(len(self.store.list("study_alert")), 3)
        self.assertEqual(phone.calls, [])
        morning = datetime(2026, 10, 29, 9, tzinfo=TZ).timestamp()
        scheduler.tick(phone, now=morning)
        self.assertEqual(len(phone.calls), 2)
        ReviewScheduler(self.root).tick(phone, now=morning + 60)
        self.assertEqual(len(phone.calls), 2)
        self.assertEqual(len(self.store.list("study_alert")), 3)

    def test_local_review_works_without_phone_and_pause_stops_next_cycle(self):
        now = datetime(2026, 10, 28, 10, tzinfo=TZ).timestamp()
        review = create_review(self.store, course="操作系统", topic="资源与线程", citations=[], now=now - 172800)
        phone = Phone(False)
        ReviewScheduler(self.root).tick(phone, now=now)
        self.assertEqual(len(home(self.store, now=now)["alerts"]), 1)
        result = update_review(self.store, review["id"], action="complete", rating=1, now=now)
        self.assertGreater(result["due_at"], now)
        self.assertEqual(home(self.store, now=now)["alerts"], [])
        update_review(self.store, review["id"], action="pause", now=now)
        self.assertEqual(home(self.store, now=now + 999999)["due"], [])

    def test_confirm_task_preserves_proposal_and_progress(self):
        settings = Settings(self.root, vault_root=self.root / "vault")
        classroom = Store(self.root)
        class Quiet:
            def close(self): classroom.db.close()
        app = create_app(settings, classroom, Quiet(), run_workers=False)
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer " + settings.data["token"]
            proposal = self.store.save("proposal", {"course": "操作系统", "title": "复习", "detail": "进程线程", "proposal_kind": "task", "status": "pending"})
            path = "/api/study/proposals/" + proposal["id"]
            self.assertEqual(client.post(path, json={"status": "accepted", "estimate_min": 20, "estimate_max": 40}).status_code, 200)
            self.assertEqual(self.store.get(proposal["id"], "proposal")["status"], "accepted")
            task = self.store.list("task")[0]
            self.store.save("task", {**task, "spent_minutes": 15, "completed": True}, task["id"])
            client.post(path, json={"status": "accepted", "estimate_min": 20, "estimate_max": 40})
            self.assertEqual(self.store.get(task["id"], "task")["spent_minutes"], 15)
            self.assertTrue(self.store.get(task["id"], "task")["completed"])

    def test_quiet_review_does_not_block_urgent_classroom_alert(self):
        settings = Settings(self.root, vault_root=self.root / "vault")
        classroom = Store(self.root)
        settings.data["mobile_notifications_enabled"] = True
        try:
            with patch.object(settings, "mobile_pairing", return_value=make_pairing()):
                notifier = MobileNotifier(settings, classroom, transport=httpx.MockTransport(lambda req: httpx.Response(201, json={"ok": True})))
                notifier.queue("classroom-alert", {"source": "study-review", "id": "review", "message": "回忆"}, "r")
                notifier.queue("classroom-alert", {"message": "现在点名"}, "c")
                with patch.object(notifier.study_reviews, "delivery_state", return_value="wait"):
                    self.assertTrue(notifier.flush())
                    self.assertEqual(len(notifier.state["outbox"]), 1)
                    self.assertFalse(notifier.flush())
                with patch.object(notifier.study_reviews, "delivery_state", return_value="discard"):
                    self.assertFalse(notifier.flush())
                    self.assertEqual(notifier.state["outbox"], [])
        finally:
            classroom.db.close()

    def test_paused_review_cannot_be_delivered_from_an_old_outbox(self):
        now = datetime(2026, 10, 28, 10, tzinfo=TZ).timestamp()
        review = create_review(self.store, course="操作系统", topic="线程", citations=[], now=now - 172800)
        scheduler = ReviewScheduler(self.root)
        scheduler.tick(now=now)
        alert = self.store.list("study_alert")[0]
        self.assertEqual(scheduler.delivery_state(alert["id"], now), "send")
        update_review(self.store, review["id"], action="pause", now=now)
        self.assertEqual(scheduler.delivery_state(alert["id"], now), "discard")


if __name__ == "__main__":
    unittest.main()
