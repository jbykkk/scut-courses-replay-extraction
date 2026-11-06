import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from assistant_service.analysis import Analyzer
from assistant_service.app import create_app
from assistant_service.lecture_materials import available_materials, material_context, selected_materials
from assistant_service.manager import Manager
from assistant_service.settings import Settings
from assistant_service.store import Store
from assistant_service.study_agent.knowledge import set_policy
from assistant_service.study_agent.storage import StudyStore


class LectureMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name))
        self.store = Store(self.settings.root)
        self.addCleanup(self.store.db.close)
        self.study = StudyStore(self.settings.root)
        self.manager = Manager(self.settings, self.store)
        self.addCleanup(self.manager.close)
        self.sid = self.store.create(dict(mode="subtitle", course_title="操作系统", course_id="1", sub_id="2"))["id"]
        self.segments = [dict(id="speech-1", start=20, end=45, text="条件变量必须在互斥锁保护下检查条件，使用 while 循环等待。")]
        self.store.update(self.sid, stopped=True, status="complete", segments=self.segments)
        self.client = TestClient(create_app(self.settings, self.store, self.manager, run_workers=False))
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer " + self.settings.data["token"]

    def source(self, course="操作系统", title="同步.pdf", text="条件变量必须在互斥锁保护下检查条件，使用 while 循环等待。", fingerprint="v1"):
        return self.study.source(origin="test:" + title, course=course, title=title, kind="material", fingerprint=fingerprint,
                                 chunks=[{"text": text, "locator": {"page": 7}}])["id"]

    def test_retrieval_scopes_course_and_exclusion_and_never_matches_numbers_alone(self):
        good = self.source()
        self.source(course="另一门课", title="其他.pdf")
        hidden = self.source(title="停用.pdf", fingerprint="v2")
        set_policy(self.study, hidden, role="slides", excluded=True)
        session = self.store.get(self.sid)
        sources = selected_materials(self.study, session)
        self.assertEqual([s["id"] for s in sources], [good])
        items = material_context(self.study, sources, self.segments)
        self.assertEqual(items[0]["locator"], {"page": 7})
        self.assertEqual(items[0]["source_id"], good)
        self.assertEqual(material_context(self.study, sources, [{"text": "1 2 3 4 5"}]), [])
        self.assertEqual(selected_materials(self.study, session | {"note_source_ids": []}), [])

    def test_model_receives_courseware_and_citations_resolve_to_original_page_and_speech(self):
        sid = self.source()
        materials = material_context(self.study, selected_materials(self.study, self.store.get(self.sid)), self.segments)
        def respond(request):
            payload = json.loads(request.content)
            data = json.loads(payload["messages"][1]["content"])
            self.assertEqual(data["materials"][0]["locator"]["page"], 7)
            self.assertEqual(data["segments"][0]["id"], "s1")
            self.assertNotIn("test:同步.pdf", payload["messages"][1]["content"])
            content = {"events": [], "overview": "条件变量", "topics": [{"title": "条件变量与等待条件", "detail": "课件说明等待条件，老师补充 while 循环。",
                "segment_ids": ["s1"], "material_refs": [{"id": "m1", "quote": "使用 while 循环等待", "page": 999}]}]}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})
        with patch.object(self.settings, "key", return_value="test-key"):
            analyzer = Analyzer(self.settings, httpx.MockTransport(respond))
            self.addCleanup(analyzer.close)
            result = analyzer.analyze(self.segments, summary=True, materials=materials)
        topic = result["topics"][0]
        self.assertEqual(topic["segment_ids"], ["speech-1"])
        self.assertEqual(topic["material_refs"][0]["locator"]["page"], 7)
        self.assertEqual(topic["material_refs"][0]["source_id"], sid)
        for citation in [{"id": "nonexistent", "quote": "使用 while 循环等待"}, {"id": "m1", "quote": "使用 if 即可"}]:
            with self.assertRaises(TypeError):
                Analyzer.validate({"topics": [{"title": "条件变量", "segment_ids": ["speech-1"], "material_refs": [citation]}]}, self.segments, materials={"m1": materials[0]})

    def test_courseware_quotes_allow_pdf_whitespace_changes_and_retain_exact_original(self):
        original = "Condition\nvariables\t require\u00a0a while loop."
        material = {"id": "evidence", "source_id": "source", "title": "slides.pdf", "locator": {"page": 7},
                    "text": "Heading\n" + original + "\nNext paragraph."}
        topic = {"title": "条件变量", "segment_ids": ["speech-1"],
                 "material_refs": [{"id": "m1", "quote": "Condition variables require a while loop."}]}
        result = Analyzer.validate({"topics": [topic]}, self.segments, materials={"m1": material})
        self.assertEqual(result["topics"][0]["material_refs"][0]["quote"], original)
        self.assertIn(result["topics"][0]["material_refs"][0]["quote"], material["text"])
        topic["material_refs"][0]["quote"] = "Condition variables require an if statement."
        with self.assertRaises(TypeError):
            Analyzer.validate({"topics": [topic]}, self.segments, materials={"m1": material})

    def test_summary_cache_changes_with_selected_materials_and_export_keeps_both_sources(self):
        first = self.source()
        note = {"events": [], "overview": "条件变量", "topics": [{"title": "条件变量", "start": 20, "end": 45,
                "detail": "结合课件和讲述", "segment_ids": ["speech-1"], "material_refs": [{"source_id": first,
                "evidence_id": first+":0", "title": "同步.pdf", "locator": {"page": 7}, "quote": "使用 while 循环等待"}]}]}
        with patch.object(self.manager.analyzer, "analyze", return_value=note) as analyze, \
             patch.object(self.manager.analyzer, "synthesize", return_value={"title": "条件变量", "overview": "概览", "groups": []}), \
             patch.object(self.manager.analyzer, "consolidate", return_value=[]):
            for _ in range(2):
                self.store.enqueue(self.sid, "summary", {}, lane="analysis")
                self.manager.execute(self.store.claim("analysis"))
            self.assertEqual(analyze.call_count, 1)
            self.assertEqual(analyze.call_args.kwargs["materials"][0]["source_id"], first)
            self.source(fingerprint="v2", text="条件变量必须在互斥锁保护下检查条件，使用 while 循环等待。新版本")
            self.store.enqueue(self.sid, "summary", {}, lane="analysis")
            self.manager.execute(self.store.claim("analysis"))
            self.assertEqual(analyze.call_count, 2)
        record = self.store.get(self.sid)
        self.assertFalse(record["summary"]["partial"])
        self.store.export(record)
        exported = (self.store.directory(self.sid) / "notes.md").read_text(encoding="utf-8")
        self.assertIn("同步.pdf · 第 7 页", exported)
        self.assertIn("老师原话 [20s]", exported)
        self.assertIn(self.segments[0]["text"], exported)

    def test_api_rejects_other_course_and_busy_changes_and_supports_explicit_empty(self):
        ours = self.source()
        other = self.source(course="另一门课", title="其他.pdf")
        url = f"/api/sessions/{self.sid}/materials"
        listed = self.client.get(url).json()
        self.assertEqual(listed["selected_ids"], [ours])
        self.assertEqual(self.client.post(url, json={"source_ids": [other]}).status_code, 400)
        self.assertEqual(self.client.get(url+f"/{other}/file").status_code, 404)
        self.assertEqual(self.client.post(url, json={"source_ids": []}).json()["selected_ids"], [])
        self.store.enqueue(self.sid, "summary", {}, lane="analysis")
        self.assertEqual(self.client.post(url, json={"source_ids": [ours]}).status_code, 409)

    def test_uploaded_pdf_binds_to_lesson_and_original_preview_requires_auth(self):
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(50, 700, "Condition variables require a while loop.")
        pdf.save()
        content = buffer.getvalue()
        upload = self.client.post("/api/study/attachments", json={"name": "slides.pdf", "content": base64.b64encode(content).decode()})
        self.assertEqual(upload.status_code, 200, upload.text)
        result = self.client.post(f"/api/sessions/{self.sid}/materials", json={"attachment_ids": [upload.json()["id"]]})
        self.assertEqual(result.status_code, 200, result.text)
        source_id = result.json()["selected_ids"][0]
        url = f"/api/sessions/{self.sid}/materials/{source_id}/file"
        response = self.client.get(url)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.content, content)
        page_response = self.client.get(url.replace("/file", "/pages/1"))
        self.assertEqual(page_response.status_code, 200, page_response.text[:100] if page_response.status_code != 200 else "")
        self.assertEqual(page_response.headers["content-type"], "image/png")
        self.assertTrue(page_response.content.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get(url.replace("/file", "/pages/2")).status_code, 404)
        self.assertEqual(self.client.get(url, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        attachment = self.study.get(upload.json()["id"], "attachment")
        (self.settings.root / "study-uploads" / attachment["file"]).write_bytes(b"changed")
        self.assertEqual(self.client.get(url).status_code, 409)


if __name__ == "__main__":
    unittest.main()
