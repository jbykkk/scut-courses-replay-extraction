from __future__ import annotations

import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path

from assistant_service.study_agent.storage import StudyStore
from assistant_service.study_agent.knowledge import set_policy
from assistant_service.study_agent.mcp import Server, TOOLS
from assistant_service.study_agent.daily import daily_plan
from assistant_service.study_agent.tutoring import (
    evidence_context, prepare_context, list_materials, open_session, get_session, record_progress)
from scripts.prepare_workbuddy import install_local, configuration


class WorkBuddyLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StudyStore(self.root)
        self.doc = self.source("private/slides.pdf", "slides-v1", [
            "LIF has a passive membrane and a threshold rule.",
            "HH gating variables describe activation and inactivation.",
            "Compare fixed points and the assumptions of the models."])
        self.speech = self.source("private/recording", "speech-v1", [
            "The teacher says we may need to draw a figure for an exam.",
            "Now another group presents. A student says: I will draw it on the board."], kind="school_subtitle")
        self.other = self.source("other", "other-v1", ["LIF from a different course"], course="Other")
        self.eid = self.doc["id"] + ":1"

    def tearDown(self):
        self.temp.cleanup()

    def source(self, origin, fingerprint, chunks, kind="material", course="Neuroscience"):
        return self.store.source(origin=origin, course=course, title="Lecture slides" if kind=="material" else "Lecture transcript",
             kind=kind, fingerprint=fingerprint, chunks=[{"text": t, "locator": {"page": i+1}} for i,t in enumerate(chunks)],
             meta={"secret": "must-not-leak", "file": "private/file.pdf"})

    def session(self):
        return open_session(self.store, "Neuroscience", "Understand LIF and HH", "start-1")

    def step(self, session, request="step-1", **extra):
        return record_progress(self.store, session["id"], request, session["revision"], "LIF vs HH", [self.eid],
                               question="Which states are retained?", **extra)

    def test_neighbor_context_preserves_transition_without_visual_claims(self):
        value = evidence_context(self.store, self.speech["id"]+":0")
        self.assertEqual(len(value["items"]), 2)
        self.assertIn("student", value["items"][1]["text"])
        self.assertNotIn("must-not-leak", json.dumps(value))
        self.assertNotIn('"origin"', json.dumps(value))
        self.assertEqual([e["locator"]["page"] for e in evidence_context(self.store, self.eid)["items"]], [1,2,3])

    def test_context_is_diverse_bounded_and_does_not_call_or_store_a_model(self):
        result = prepare_context(self.store, "Neuroscience", "LIF HH teacher figure")
        self.assertEqual({e["course"] for e in result["evidence"]}, {"Neuroscience"})
        self.assertGreaterEqual(len({e["source_id"] for e in result["evidence"]}), 2)
        self.assertLessEqual(result["budget"]["characters"], 16000)
        self.assertEqual(result["teaching_contract"]["figure_axes"]["board_image"], "not_viewed")
        self.assertIn("不代表", result["coverage"]["scope"])
        self.assertEqual(self.store.list("knowledge"), [])
        self.assertEqual(self.store.list("learning_session"), [])

    def test_material_catalog_paginates_without_private_paths(self):
        for i in range(21): self.source(f"private/doc-{i}", f"fp-{i}", ["text"])
        first = list_materials(self.store, "Neuroscience")
        second = list_materials(self.store, "Neuroscience", first["next_offset"])
        self.assertEqual(len(first["items"]), 20)
        self.assertEqual(len(first["items"])+len(second["items"]), 23)
        self.assertIsNone(second["next_offset"])
        self.assertNotIn("private/", json.dumps(first))
        self.assertTrue(all(x["first_evidence_id"] for x in first["items"]))

    def test_context_defers_a_large_chunk_without_silently_slicing_it(self):
        big = self.source("big", "big-v1", ["Long evidence "*1500])
        result = evidence_context(self.store, big["id"]+":0")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["budget"]["deferred"][0]["id"], big["id"]+":0")

    def test_context_reads_accepted_requirements_from_real_proposal_memory(self):
        self.store.save("proposal", {"course":"Neuroscience", "proposal_kind":"requirement",
            "status":"accepted", "title":"LIF exam format", "detail":"Explain LIF in English", "source":"student_report"})
        self.store.save("proposal", {"course":"Neuroscience", "proposal_kind":"requirement",
            "status":"pending", "title":"LIF uncertain deadline", "detail":"not accepted"})
        result=prepare_context(self.store,"Neuroscience","LIF")
        self.assertEqual(len(result["confirmed_requirements"]),1)
        self.assertEqual(result["confirmed_requirements"][0]["source"],"student_report")

    def test_session_open_is_idempotent_and_scoped(self):
        s = self.session()
        self.assertEqual(self.session()["id"], s["id"])
        self.assertEqual(len(self.store.list("learning_session")), 1)
        with self.assertRaises(ValueError): open_session(self.store, "Other", "different", "start-1")
        with self.assertRaises(ValueError): get_session(self.store, "Other", s["id"])
        with self.assertRaises(ValueError): prepare_context(self.store, "Other", "LIF", s["id"])
        self.assertEqual(get_session(self.store, "Neuroscience")["sessions"][0]["id"], s["id"])

    def test_retry_does_not_duplicate_or_overwrite_student_input(self):
        s = self.session()
        value = self.step(s, student_answer="HH retains channel state", self_rating=2)
        again = self.step(s, student_answer="HH retains channel state", self_rating=2)
        self.assertEqual((value["revision"], again["revision"]), (1,1))
        self.assertEqual(len(self.store.list("learning_step")), 1)
        with self.assertRaises(ValueError): self.step(s, student_answer="different")
        with self.assertRaises(ValueError): self.step(s, request="late", student_answer="late answer")
        self.assertFalse(value["knowledge_confirmed"])
        self.assertEqual(self.store.list("knowledge"), [])
        self.assertEqual(self.store.list("review"), [])
        self.assertEqual(self.store.list("study_alert"), [])

    def test_pause_resume_and_source_refresh(self):
        s = self.session();paused = self.step(s, action="pause")
        with self.assertRaises(ValueError): self.step(paused, request="while-paused")
        resumed = self.step(paused, request="resume", action="resume")
        self.assertEqual(resumed["status"], "active")
        self.source("private/slides.pdf", "slides-v2", ["Revised HH explanation"])
        restored = get_session(self.store, "Neuroscience", s["id"])
        self.assertTrue(restored["needs_source_refresh"])
        self.assertIn(self.eid, restored["stale_evidence_ids"])
        with self.assertRaises(ValueError): self.step(restored, request="old-citation")
        with self.assertRaises(ValueError): evidence_context(self.store, self.eid)

    def test_excluded_and_wrong_course_evidence_cannot_enter_progress(self):
        s = self.session()
        with self.assertRaises(ValueError): record_progress(self.store, s["id"], "bad", 0, "x", [self.other["id"]+":0"])
        set_policy(self.store, self.doc["id"], role="slides", excluded=True)
        with self.assertRaises(ValueError): self.step(s)
        self.assertEqual(get_session(self.store, "Neuroscience", s["id"])["revision"], 0)

    def test_concurrent_progress_uses_revision_instead_of_last_writer_wins(self):
        s = self.session()
        def write(i):
            try:return self.step(s, request=f"parallel-{i}")["revision"]
            except ValueError:return "conflict"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(write, [1,2]))
        self.assertCountEqual(result, [1,"conflict"])
        self.assertEqual(len(self.store.list("learning_step")), 1)

    def test_self_reported_gap_affects_daily_plan_and_later_rating_supersedes(self):
        s=self.session()
        s=self.step(s, student_answer="I still confuse the state variables", self_rating=2)
        plan=daily_plan(self.store,10,"Neuroscience",now=time.time()+1)
        self.assertEqual(plan["steps"][0]["kind"],"learning_gap")
        self.assertIn("自评",plan["steps"][0]["reasons"][0])
        self.assertLessEqual(plan["used_minutes"],10)
        self.assertEqual(self.store.list("review"),[])
        self.step(s,request="better",student_answer="I can now explain the differences",self_rating=3)
        self.assertEqual(len(self.store.list("learning_topic")),1)
        self.assertEqual(len(self.store.list("learning_step")),2)
        plan=daily_plan(self.store,10,"Neuroscience",now=time.time()+1)
        self.assertFalse(any(x["kind"]=="learning_gap" for x in plan["steps"]))
        self.assertFalse(any(x["kind"]=="diagnostic" for x in plan["steps"]))
        self.assertEqual(daily_plan(self.store,10,"Other",now=time.time()+1)["steps"][0]["kind"],"diagnostic")

    def test_gap_respects_snoozed_review_and_source_changes(self):
        self.step(self.session(), self_rating=1)
        review=self.store.save("review", {"course":"Neuroscience", "topic":"LIF vs HH", "due_at":time.time()+86400,
            "citations":[{"evidence_id":self.eid}], "status":"active", "mode":"recall", "minutes":8, "attempts":[]})
        plan=daily_plan(self.store,10,"Neuroscience",now=time.time()+1)
        self.assertFalse(any(x["kind"]=="learning_gap" for x in plan["steps"]))
        self.source("private/slides.pdf","slides-v2",["Revised states"])
        from assistant_service.study_agent.tutoring import learning_gaps
        self.assertEqual(learning_gaps(self.store,time.time()+1),[])

    def test_mcp_schemas_reject_bool_revision_and_excess_context(self):
        server=Server(self.store)
        server.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
        server.handle({"jsonrpc":"2.0","method":"notifications/initialized"})
        def call(name,args):return server.handle({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}})
        self.assertEqual(len(TOOLS),16)
        self.assertEqual(call("read_study_evidence_context",{"evidence_id":self.eid,"radius":3})["error"]["code"],-32602)
        s=self.session()
        args={"session_id":s["id"],"request_id":"bad","expected_revision":True,"topic":"x","evidence_ids":[]}
        self.assertEqual(call("record_learning_progress",args)["error"]["code"],-32602)
        status=json.loads(call("study_connection_status",{})["result"]["content"][0]["text"])
        self.assertEqual(status["server_version"],"0.4.0")
        self.assertFalse(status["capabilities"]["automatic_grading"])

    def test_mcp_learning_flow_can_resume_and_plan_without_a_second_model(self):
        server=Server(self.store)
        server.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
        server.handle({"jsonrpc":"2.0","method":"notifications/initialized"})
        def call(name,args):
            response=server.handle({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}})["result"]
            self.assertFalse(response["isError"], response)
            return json.loads(response["content"][0]["text"])
        s=call("open_learning_session",{"course":"Neuroscience","goal":"LIF vs HH","request_id":"protocol-1"})
        context=call("prepare_learning_context",{"course":"Neuroscience","topic":"HH LIF","session_id":s["id"]})
        self.assertTrue(context["evidence"])
        args={"session_id":s["id"],"request_id":"protocol-answer-1","expected_revision":0,"topic":"LIF vs HH",
              "evidence_ids":[self.eid],"student_answer":"I need help comparing the states","self_rating":2}
        call("record_learning_progress",args)
        restored=call("get_learning_session",{"course":"Neuroscience","session_id":s["id"]})
        self.assertEqual(restored["revision"],1)
        self.assertEqual(restored["recent_steps"][0]["answer_origin"],"student_report")
        self.assertEqual(call("plan_today_review",{"minutes":10,"course":"Neuroscience"})["steps"][0]["kind"],"learning_gap")

    def test_install_includes_references_and_preserves_other_connectors(self):
        skill=self.root/'test-skill';(skill/'references').mkdir(parents=True)
        (skill/'SKILL.md').write_text('---\nname: scut-study\ndescription: test\n---\nRead `references/guide.md`.',encoding='utf-8')
        (skill/'references/guide.md').write_text('Protocol v1',encoding='utf-8')
        target=self.root/'workbuddy';target.mkdir()
        (target/'mcp.json').write_text(json.dumps({'mcpServers':{'other':{'command':'keep'}}}),encoding='utf-8')
        cfg=configuration(self.root)
        result=install_local(cfg,self.root/'output',target,skill_root=skill)
        self.assertIn('references/guide.md',result['skill_files_changed'])
        self.assertEqual((target/'skills/scut-study/references/guide.md').read_text(),'Protocol v1')
        self.assertEqual(json.loads((target/'mcp.json').read_text())['mcpServers']['other']['command'],'keep')
        again=install_local(cfg,self.root/'output',target,skill_root=skill)
        self.assertFalse(again['changed']);self.assertEqual(again['skill_files_changed'],[])
        (skill/'SKILL.md').write_text('Read `references/missing.md`.',encoding='utf-8')
        previous=(target/'mcp.json').read_bytes()
        with self.assertRaises(ValueError):install_local(cfg,self.root/'output',target,skill_root=skill)
        self.assertEqual((target/'mcp.json').read_bytes(),previous)


if __name__ == '__main__':unittest.main()
