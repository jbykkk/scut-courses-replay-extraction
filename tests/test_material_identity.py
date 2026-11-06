import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from assistant_service.app import create_app
from assistant_service.manager import Manager
from assistant_service.lecture_materials import selected_materials
from assistant_service.settings import Settings
from assistant_service.store import Store
from assistant_service.study_agent.attachments import prepare_attachments
from assistant_service.study_agent.ingest import import_file
from assistant_service.study_agent.knowledge import set_policy, source_policy
from assistant_service.study_agent.storage import digest


class MaterialIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root, vault_root=self.root / "vault")
        self.classroom = Store(self.root)
        self.addCleanup(self.classroom.db.close)
        self.manager = Manager(self.settings, self.classroom)
        self.addCleanup(self.manager.close)
        self.study = self.manager.courseware.study
        self.app = create_app(self.settings, self.classroom, self.manager, run_workers=False)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.state.study_operations.provider.close)
        self.client.headers["Authorization"] = "Bearer " + self.settings.data["token"]

    def upload(self, text, name="slides.pdf"):
        stream = io.BytesIO()
        pdf = canvas.Canvas(stream, invariant=1)
        pdf.drawString(50, 700, text)
        pdf.save()
        data = stream.getvalue()
        response = self.client.post("/api/study/attachments", json={"name": name, "content": base64.b64encode(data).decode()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"], data

    def import_slides(self, aid, course="OS", **extra):
        return self.client.post("/api/courseware/import", json={"course": course, "attachment_ids": [aid], **extra})

    def test_all_four_entry_points_reuse_the_same_source_in_different_orders(self):
        aid, data = self.upload("Processes own resources.")
        file = self.root / "slides.pdf"
        file.write_bytes(data)
        for index, order in enumerate(("folder chat lecture courseware", "courseware lecture chat folder", "chat folder courseware lecture")):
            course = f"OS-{index}"
            session = self.classroom.create(dict(mode="subtitle", course_title=course, course_id=str(index + 1), sub_id="1"))
            self.classroom.update(session["id"], stopped=True, status="complete")
            source_ids = []
            for entry in order.split():
                if entry == "folder":
                    result = import_file(self.study, self.root, "slides.pdf", course)
                    source_ids.append(result["id"])
                elif entry == "chat":
                    evidence, images = prepare_attachments(self.study, None, self.root, [aid], course, "Explain")
                    self.assertFalse(images)
                    source_ids.append(evidence[0]["source_id"])
                elif entry == "lecture":
                    response = self.client.post(f"/api/sessions/{session['id']}/materials", json={"attachment_ids": [aid]})
                    self.assertEqual(response.status_code, 200, response.text)
                    source_ids.append(response.json()["selected_ids"][0])
                else:
                    response = self.import_slides(aid, course)
                    self.assertEqual(response.status_code, 200, response.text)
                    source_ids.append(response.json()["items"][0]["id"])
            self.assertEqual(len(set(source_ids)), 1, order)
            self.assertEqual(len(self.study.sources(course)), 1, order)

    def test_same_name_defaults_to_keep_both_and_preview_never_mutates(self):
        first, _ = self.upload("First chapter.")
        second, _ = self.upload("Second chapter.")
        old = self.import_slides(first).json()["items"][0]["id"]
        preview = self.client.post("/api/courseware/preview", json={"course": "OS", "attachment_ids": [second]})
        self.assertEqual(preview.json()["items"][0]["conflicts"], [{"id": old, "title": "slides.pdf"}])
        self.assertEqual(len(self.study.sources("OS")), 1)
        response = self.import_slides(second)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({s["title"] for s in self.study.sources("OS")}, {"slides.pdf", "slides（2）.pdf"})
        self.assertTrue(self.study.evidence(old + ":0")["active"])
        self.assertEqual(len(self.import_slides(second).json()["sources"]), 2)

    def test_explicit_replacement_preserves_old_citations_and_is_retryable(self):
        first, _ = self.upload("Old version.")
        second, _ = self.upload("Updated version.")
        old = self.import_slides(first).json()["items"][0]["id"]
        set_policy(self.study, old, role="slides", trusted=True)
        result = self.import_slides(second, replacements={second: old})
        self.assertEqual(result.status_code, 200, result.text)
        new = result.json()["items"][0]["id"]
        self.assertNotEqual(new, old)
        self.assertEqual(len(self.study.sources("OS")), 1)
        self.assertFalse(self.study.evidence(old + ":0")["active"])
        self.assertIn("Old version", self.study.evidence(old + ":0")["text"])
        self.assertTrue(source_policy(self.study, self.study.sources("OS")[0])["trusted"])
        self.assertEqual([s["id"] for s in selected_materials(self.study, {"course_title": "OS", "note_source_ids": [old]})], [new])
        retry = self.import_slides(second, replacements={second: old})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["items"][0]["id"], new)

    def test_legacy_fingerprint_and_exclusion_are_preserved(self):
        aid, data = self.upload("Legacy source.")
        item = self.study.get(aid, "attachment")
        old = self.study.source(origin="attachment:" + aid, course="OS", title="slides.pdf", kind="material",
            fingerprint=digest([hashlib.sha256(data).hexdigest(), item["chunks"]]), chunks=item["chunks"], meta={"attachment_id": aid})
        set_policy(self.study, old["id"], role="reference", excluded=True, trusted=True)
        response = self.import_slides(aid)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["id"], old["id"])
        self.assertEqual(len(self.study.sources("OS")), 1)
        policy = source_policy(self.study, self.study.sources("OS")[0])
        self.assertTrue(policy["excluded"])
        self.assertTrue(policy["trusted"])

    def test_invalid_replacement_does_not_partially_import_the_batch(self):
        first, _ = self.upload("Original.")
        second, _ = self.upload("Replacement.")
        third, _ = self.upload("Other new file.", "other.pdf")
        old = self.import_slides(first, course="Another course").json()["items"][0]["id"]
        response = self.client.post("/api/courseware/import", json={"course": "OS", "attachment_ids": [third, second], "replacements": {second: old}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.study.sources("OS"), [])

    def test_completed_chat_is_available_even_when_model_slot_is_busy(self):
        body = dict(conversation="main", client_id="ask-1", question="Explain processes", course="OS", attachments=[], purpose="ask", review_id=None)
        with patch("assistant_service.study_agent.dialogue.run_agent", return_value={"claims": []}):
            result = self.client.post("/api/study/chat", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        gate = self.app.state.study_operations.gate
        gate.acquire()
        try:
            repeated = self.client.post("/api/study/chat", json=body)
        finally:
            gate.release()
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["id"], result.json()["id"])
