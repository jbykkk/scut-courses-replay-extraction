import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from assistant_service.app import create_app
from assistant_service.courseware import course_sources, ordered_path, pages_for, subtitle_windows
from assistant_service.manager import Manager
from assistant_service.settings import Settings
from assistant_service.store import Store
from assistant_service.study_agent.knowledge import set_policy
from assistant_service.study_agent.ingest import extract


class CoursewareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name))
        self.store = Store(self.settings.root)
        self.addCleanup(self.store.db.close)
        self.manager = Manager(self.settings, self.store)
        self.addCleanup(self.manager.close)
        self.alignment = self.manager.courseware
        self.study = self.alignment.study
        self.client = TestClient(create_app(self.settings, self.store, self.manager, run_workers=False))
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer " + self.settings.data["token"]

    def lesson(self, segments=None, course="操作系统", sub="1"):
        session = self.store.create(dict(mode="subtitle", course_title=course, course_id="1", sub_id=sub, title="第一节"))
        self.store.update(session["id"], stopped=True, status="complete", segments=segments or [
            {"id": "s1", "start": 0, "end": 40, "text": "多个任务轮流使用处理器，保存和恢复运行现场。"},
            {"id": "s2", "start": 70, "end": 100, "text": "今天的作业下周交，同学们记住截止时间。"}])
        return session["id"]

    def source(self, title="01-process.pdf", texts=None, course="操作系统", fingerprint="v1"):
        return self.study.source(origin="test:" + title, course=course, title=title, kind="material", fingerprint=fingerprint,
            chunks=[{"text": t, "locator": {"page": i + 1}} for i, t in enumerate(texts or ["Context switching saves registers and restores execution state."])])["id"]

    def execute_alignment(self):
        job = self.store.claim("alignment")
        self.assertIsNotNone(job)
        self.manager.execute(job)
        return job

    def test_windows_respect_pauses_quality_and_original_segment_ids(self):
        windows = subtitle_windows([
            {"id": "c", "start": 90, "end": 95, "text": "后面的讲解"},
            {"id": "a", "start": 0, "end": 10, "text": "原话一"},
            {"id": "b", "start": 11, "end": 20, "text": "原话二"},
            {"id": "noise", "start": 50, "end": 65, "text": "不可靠", "quality": "uncertain"}])
        self.assertEqual([w["segment_ids"] for w in windows], [["a", "b"], ["c"]])
        self.assertEqual(windows[0]["text"], "原话一\n原话二")

    def test_sequence_prefers_neighbor_pages_but_allows_skip_review_and_unmatched(self):
        path = ordered_path([[(10, .99)], [(90, .82), (11, .81)], [], [(12, .95)], [(10, .99)], [(20, .99)]])
        self.assertEqual([p for p, _ in path], [10, 11, None, 12, 10, 20])
        self.assertEqual(ordered_path([]), [])

    def test_catalogue_scopes_course_version_and_policy_and_reassembles_pages(self):
        tenth = self.source("10-chapter.pdf", fingerprint="ten")
        second = self.source("02-chapter.pdf", fingerprint="two")
        self.source("other.pdf", course="其他课", fingerprint="other")
        hidden = self.source("hidden.pdf", fingerprint="hidden")
        set_policy(self.study, hidden, role="slides", excluded=True)
        reference = self.source("reference.pdf", fingerprint="reference")
        set_policy(self.study, reference, role="reference")
        sources = course_sources(self.study, "操作系统")
        self.assertEqual([s["id"] for s in sources], [second, tenth])
        new = self.source("02-chapter.pdf", fingerprint="new")
        self.assertNotIn(second, [s["id"] for s in course_sources(self.study, "操作系统")])
        assembled = self.study.source(origin="test:long", course="long", title="long.pdf", kind="material", fingerprint="long",
            chunks=[{"text": "abcdefgh", "locator": {"page": 3, "offset": 0}},
                    {"text": "fghijk", "locator": {"page": 3, "offset": 5}}])
        page = pages_for(self.study, self.study.sources("long"))[0]
        self.assertEqual(page["text"], "abcdefghijk")
        self.assertEqual(page["source_id"], assembled["id"])
        self.assertIn(new, [s["id"] for s in course_sources(self.study, "操作系统")])

    def test_semantics_match_paraphrase_without_shared_words_and_keep_unmatched_notice(self):
        sid, source = self.lesson(), self.source()
        requests = []

        def complete(messages, timeout):
            payload = json.loads(messages[-1]["content"])
            requests.append(payload)
            if "lecture_samples" in payload:
                return json.dumps({"pages": [0]})
            return json.dumps({"windows": [{"id": w["id"], "matches": [{"page": 0, "score": .96}] if w["id"] == 0 else []}
                                           for w in payload["windows"]]})

        with patch.object(self.alignment, "model_stamp", return_value="semantic-model"), \
             patch.object(self.alignment.provider, "complete", side_effect=complete):
            self.alignment.queue()
            self.execute_alignment()
            result = self.alignment.result(sid)
            self.assertEqual(result["method"], "semantic")
            self.assertEqual(result["rows"][0]["material"]["source_id"], source)
            self.assertEqual(result["rows"][0]["status"], "matched")
            self.assertEqual(result["rows"][1]["status"], "unmatched")
            self.assertEqual(result["rows"][0]["segment_ids"], ["s1"])
            self.assertEqual(result["rows"][0]["end"], 40)
            mentions = self.client.get("/api/courseware/mentions", params={"course": "操作系统", "source_id": source, "page": 1})
            self.assertEqual(mentions.json()["mentions"][0]["segment_id"], "s1")
            context = self.alignment.context(sid, self.store.get(sid)["segments"][:1], course_sources(self.study, "操作系统"))
            self.assertEqual(context[0]["locator"], {"page": 1})
            self.alignment.queue()
            self.assertIsNone(self.store.claim("alignment"))
            self.assertEqual(len(requests), 2)

    def test_new_slides_backfill_existing_lessons_and_new_subtitles_queue_automatically(self):
        sid = self.lesson()
        self.manager.finalize()
        self.assertIsNone(self.store.claim("alignment"))
        self.source()
        self.manager.finalize()
        self.execute_alignment()
        self.assertEqual(self.store.get(sid)["alignment_status"], "complete")
        newer = self.lesson(sub="2")
        self.manager.finalize()
        job = self.execute_alignment()
        self.assertEqual(job["session_id"], newer)
        self.source(fingerprint="v2")
        self.assertEqual(self.alignment.result(sid)["rows"], [])
        self.manager.finalize()
        self.assertTrue(self.store.pending(sid, "alignment"))

    def test_keyless_preview_upgrades_to_semantic_and_failed_requests_are_bounded(self):
        sid = self.lesson()
        self.source()
        with patch.object(self.alignment, "model_stamp", return_value="keyword"):
            self.alignment.queue()
            self.execute_alignment()
            self.assertEqual(self.alignment.result(sid)["method"], "keyword")
        with patch.object(self.alignment, "model_stamp", return_value="model"), \
             patch.object(self.alignment.provider, "complete", return_value='{"pages":[999]}'):
            for attempt in range(3):
                self.store.update(sid, alignment_retry_at=0)
                self.alignment.queue()
                self.execute_alignment()
            self.alignment.queue()
            self.assertIsNone(self.store.claim("alignment"))
            self.assertEqual(self.store.get(sid)["alignment_status"], "failed")
            self.assertEqual(self.store.get(sid)["status"], "complete")
            self.alignment.queue(retry=True)
            self.assertTrue(self.store.pending(sid, "alignment"))

    def test_partial_semantic_work_is_cached_and_source_change_discards_inflight_result(self):
        sid = self.lesson()
        self.source()
        requests = []

        def complete(messages, timeout):
            payload = json.loads(messages[-1]["content"])
            requests.append(payload)
            if "lecture_samples" in payload:
                return '{"pages":[0]}'
            if len(requests) == 2:
                return '{"windows":[]}'
            self.source(fingerprint="changed")
            return json.dumps({"windows": [{"id": w["id"], "matches": []} for w in payload["windows"]]})

        with patch.object(self.alignment, "model_stamp", return_value="model"), \
             patch.object(self.alignment.provider, "complete", side_effect=complete):
            self.alignment.queue()
            self.execute_alignment()
            self.alignment.queue(retry=True)
            self.execute_alignment()
        self.assertEqual(len([p for p in requests if "lecture_samples" in p]), 1)
        self.assertEqual(self.store.get(sid)["alignment_status"], "failed")
        self.assertEqual(self.alignment.result(sid)["rows"], [])

    def test_bulk_api_more_than_six_deduplicates_reorders_and_preserves_originals(self):
        sid = self.lesson()
        attachments = []
        for n in range(7):
            buffer = io.BytesIO()
            pdf = canvas.Canvas(buffer)
            pdf.drawString(50, 700, f"Lecture {n}: Context switching saves registers.")
            pdf.save()
            response = self.client.post("/api/study/attachments", json={"name": f"{n+1}.pdf", "content": base64.b64encode(buffer.getvalue()).decode()})
            self.assertEqual(response.status_code, 200, response.text)
            attachments.append(response.json()["id"])
        body = {"course": "操作系统", "attachment_ids": attachments}
        imported = self.client.post("/api/courseware/import", json=body)
        self.assertEqual(imported.status_code, 200, imported.text)
        sources = imported.json()["sources"]
        self.assertEqual(len(sources), 7)
        self.assertTrue(self.store.pending(sid, "alignment"))
        self.assertEqual(len(self.client.post("/api/courseware/import", json=body).json()["sources"]), 7)
        ids = [s["id"] for s in reversed(sources)]
        result = self.client.post("/api/courseware/order", json={"course": "操作系统", "source_ids": ids})
        self.assertEqual([s["id"] for s in result.json()["sources"]], ids)
        invalid = self.client.post("/api/courseware/order", json={"course": "其他课", "source_ids": ids})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.client.get(f"/api/sessions/{sid}/materials/{ids[0]}/file").status_code, 200)
        self.assertEqual(len(self.client.get("/api/courseware/pages", params={"course": "操作系统", "source_id": ids[0]}).json()["pages"]), 1)
        self.assertEqual(self.client.get("/api/courseware/pages", params={"course": "其他课", "source_id": ids[0]}).status_code, 400)
        self.assertEqual(self.client.get("/api/courseware/courses", headers={"Authorization": "Bearer wrong"}).status_code, 401)

    def test_pptx_alignment_uses_presentation_order_after_slides_are_rearranged(self):
        path = self.settings.root / "reordered.pptx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="urn:p" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="1" r:id="r2"/><p:sldId id="2" r:id="r1"/></p:sldIdLst></p:presentation>')
            archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>')
            archive.writestr("ppt/slides/slide1.xml", '<s xmlns:a="urn:a"><a:t>Second displayed slide</a:t></s>')
            archive.writestr("ppt/slides/slide2.xml", '<s xmlns:a="urn:a"><a:t>First displayed slide</a:t></s>')
        chunks = extract(path)
        self.assertEqual([c["text"] for c in chunks], ["First displayed slide", "Second displayed slide"])
        self.assertEqual([c["locator"]["slide"] for c in chunks], [1, 2])

    def test_deleted_cancelled_and_superseded_sessions_are_not_backfilled(self):
        old = self.lesson()
        self.lesson()
        deleted = self.lesson(sub="deleted")
        cancelled = self.lesson(sub="cancelled")
        self.store.update(deleted, deleted_at=1)
        self.store.update(cancelled, status="cancelled")
        self.source()
        self.alignment.queue()
        for sid in [old, deleted, cancelled]:
            self.assertFalse(self.store.pending(sid, "alignment"))


if __name__ == "__main__":
    unittest.main()
