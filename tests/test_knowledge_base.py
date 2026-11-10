from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_service.study_agent.storage import StudyStore
from assistant_service.study_agent.concepts import expand_query, register_concept
from assistant_service.study_agent.knowledge_base import search_knowledge_base, prepare_quiz
from assistant_service.study_agent.mcp import Server, TOOLS


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StudyStore(Path(self.temp.name))
        self.slides = self.source("private/slides.pdf", "slides-v1", [
            "A condition variable waits inside a while loop to recheck the predicate.",
            "A spurious wakeup can return without a signal."], course="OS")
        self.speech = self.source("private/lesson1", "speech-v1", [
            "条件变量必须重新检查谓词，防止虚假唤醒。"], course="OS", kind="school_subtitle")
        self.other = self.source("other", "other-v1", [
            "Operating systems also use semaphores."], course="Algorithms")

    def tearDown(self):
        self.temp.cleanup()

    def source(self, origin, fingerprint, chunks, course="OS", kind="material"):
        return self.store.source(origin=origin, course=course,
            title="Lecture slides" if kind == "material" else "Lecture transcript",
            kind=kind, fingerprint=fingerprint,
            chunks=[{"text": text, "locator": {"page": i + 1}} for i, text in enumerate(chunks)])

    def test_search_is_scoped_to_one_course(self):
        found = search_knowledge_base(self.store, "semaphores", course="Algorithms")
        self.assertEqual(found["course"], "Algorithms")
        self.assertEqual(found["retrieval"], "keyword_fts5")
        self.assertTrue(found["items"])
        self.assertTrue(all(item["course"] == "Algorithms" for item in found["items"]))

    def test_search_without_course_spans_courses(self):
        found = search_knowledge_base(self.store, "Lecture", limit=12)
        self.assertEqual(found["course"], "")
        self.assertEqual(set(found["courses_searched"]), {"OS", "Algorithms"})
        self.assertEqual({item["course"] for item in found["items"]}, {"OS", "Algorithms"})

    def test_results_keep_evidence_ids_and_locators(self):
        item = search_knowledge_base(self.store, "condition variable", course="OS")["items"][0]
        self.assertIn(":", item["id"])
        self.assertEqual(item["locator"], {"page": 1})
        self.assertIn("reliability", item)
        self.assertIn("policy", item)
        self.assertIn("location", item)

    def test_terminology_expansion_reaches_english_chunks(self):
        """The retrieval used by reasoner.retrieve() must reach English chunks via Chinese aliases."""
        english_id = self.slides["id"] + ":0"
        self.assertNotIn(english_id, {e["id"] for e in self.store.search("条件变量", "OS", 12)})
        register_concept(self.store, course="OS", canonical="condition variable", zh="条件变量",
                         aliases=["CV"], evidence_ids=[english_id])
        expanded = expand_query(self.store, "OS", "条件变量")["expanded_query"]
        self.assertIn("condition variable", expanded)
        self.assertIn(english_id, {e["id"] for e in self.store.search(expanded, "OS", 12)})

    def test_reasoner_retrieval_uses_terminology_expansion(self):
        """The knowledge-base query expansion must actually reach run_agent's retrieve()."""
        from assistant_service.study_agent.reasoner import run_agent
        english_id = self.slides["id"] + ":0"
        register_concept(self.store, course="OS", canonical="condition variable", zh="条件变量",
                         aliases=["CV"], evidence_ids=[english_id])
        queries, original = [], self.store.search

        def spy(query, course, limit=8, **kwargs):
            queries.append(query)
            return original(query, course, limit, **kwargs)

        class Fake:
            def complete(self, messages, timeout):
                return json.dumps({"action": "finish", "claims": [], "reply": "ok"})

        with patch.object(self.store, "search", side_effect=spy):
            run_agent(self.store, Fake(), course="OS", question="条件变量")
        self.assertTrue(any("condition variable" in q for q in queries),
                        "retrieve() 应把术语别名扩展后的查询传给知识库检索")

    def test_prepare_quiz_returns_evidence_backed_slots(self):
        quiz = prepare_quiz(self.store, "OS", "condition variable", count=2)
        self.assertEqual(quiz["status"], "ready")
        self.assertEqual(len(quiz["slots"]), 2)
        ids = {item["id"] for item in quiz["evidence"]}
        self.assertTrue(ids)
        for slot in quiz["slots"]:
            self.assertTrue(set(slot["evidence_ids"]) <= ids)
            self.assertIn(slot["angle"], quiz["quiz_contract"]["angles"])
        self.assertIn("不自动判分", quiz["quiz_contract"]["grading"])
        self.assertIn("不写入已确认知识", quiz["quiz_contract"]["knowledge"])

    def test_prepare_quiz_does_not_invent_questions_without_sources(self):
        empty = StudyStore(Path(self.temp.name) / "empty")
        quiz = prepare_quiz(empty, "OS", "anything")
        self.assertEqual(quiz["status"], "needs_sources")
        self.assertEqual(quiz["slots"], [])
        self.assertEqual(quiz["evidence"], [])

    def test_prepare_quiz_validates_scope(self):
        with self.assertRaises(ValueError):
            prepare_quiz(self.store, "", "topic")
        with self.assertRaises(ValueError):
            prepare_quiz(self.store, "OS", "")

    def test_mcp_exposes_knowledge_base_and_quiz_tools(self):
        server = Server(self.store)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(name, args):
            return server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})

        self.assertEqual(len(TOOLS), 22)
        names = {tool["name"] for tool in TOOLS}
        self.assertIn("search_knowledge_base", names)
        self.assertIn("prepare_quiz", names)
        response = call("prepare_quiz", {"course": "OS", "topic": "condition variable", "count": 2})
        self.assertFalse(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(len(payload["slots"]), 2)
        status = json.loads(call("study_connection_status", {})["result"]["content"][0]["text"])
        self.assertTrue(status["capabilities"]["knowledge_base"])
        self.assertTrue(status["capabilities"]["quiz_preparation"])
        self.assertFalse(status["capabilities"]["automatic_grading"])
