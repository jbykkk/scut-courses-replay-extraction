from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from assistant_service.analysis import Analyzer, merge_events, rule_events
from assistant_service.app import create_app
from assistant_service.asr import validate_wav
from assistant_service.manager import Manager, append_segments
from assistant_service.media import Downloader, validate_media_url
from assistant_service.settings import Settings, protect
from assistant_service.store import Store


def wav_bytes(seconds=1, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * int(seconds * rate))
    return buffer.getvalue()


LESSON = {"course_id": "81526", "sub_id": "686882", "course_title": "测试课", "title": "测试课时",
          "page_url": "https://video.jw.scut.edu.cn/livingroom?course_id=81526&sub_id=686882", "mode": "live"}


class FakeASR:
    def diagnostics(self):
        return {"loaded": True}

    def transcribe(self, path):
        return [{"start": 0, "end": 0.9, "text": "同学们，现在扫码签到。"}]


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name))
        self.store = Store(self.settings.root)
        self.manager = Manager(self.settings, self.store, transcriber=FakeASR())
        self.client = TestClient(create_app(self.settings, self.store, self.manager, run_workers=False))
        self.client.headers["Authorization"] = "Bearer " + self.settings.data["token"]

    def tearDown(self):
        self.manager.close()
        self.client.close()
        self.store.db.close()
        self.temp.cleanup()

    def create(self, **patches):
        response = self.client.post("/api/sessions", json={**LESSON, **patches})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def test_auth_host_origin_and_secret_redaction(self):
        response = self.client.get("/api/settings", headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/api/settings", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/settings", headers={"Host": "evil.example"}).status_code, 403)
        body = self.client.get("/api/settings").text
        self.assertNotIn(self.settings.data["token"], body)
        self.assertNotIn("deepseek_key_encrypted", body)
        self.assertEqual(self.client.get("/health", headers={"Authorization": ""}).status_code, 200)
        self.assertEqual(self.client.get("/api/setup", headers={"Authorization": ""}).status_code, 401)
        setup = self.client.get("/api/setup").json()
        self.assertTrue(Path(setup["extension_path"]).joinpath("manifest.json").is_file())
        self.assertNotIn(self.settings.data["token"], json.dumps(setup))

    def test_static_assets_keep_browser_types_with_incorrect_windows_mime_types(self):
        # FileResponse normally guesses MIME types from Windows registry entries.
        assets = {"/": "text/html", "/study.html": "text/html", "/client.js": "text/javascript",
                  "/study-tools.js": "text/javascript", "/study-conversation.js": "text/javascript",
                  "/options.js": "text/javascript", "/study.css": "text/css", "/brand.png": "image/png"}
        with patch("starlette.responses.guess_type", return_value=("text/plain", None)):
            for path, expected in assets.items():
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["content-type"].split(";")[0], expected)
                    self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_course_list_includes_every_confirmed_event_category(self):
        sid = self.create()
        categories = ("attendance", "qr", "question", "assignment", "quiz", "schedule", "grading", "requirements", "reminder")
        events = [{"id": name, "category": name, "label": name, "source": "deepseek", "message": name,
                   "evidence": name, "start": index * 30, "end": index * 30 + 4} for index, name in enumerate(categories)]
        events.append({"id": "unconfirmed", "category": "assignment", "source": "local_rule", "message": "疑似作业", "evidence": "疑似作业", "start": 300})
        self.store.update(sid, events=events)
        row = next(item for item in self.client.get("/api/sessions").json() if item["id"] == sid)
        self.assertTrue(row["important_events_complete"])
        self.assertEqual({event["category"] for event in row["important_events"]}, set(categories))
        self.assertEqual(len(row["important_events"]), len(categories))
        self.assertEqual(row["important_events"][3]["start"], 90)

    def test_import_failures_are_durable_and_independent_of_the_next_batch(self):
        item = {k: v for k, v in LESSON.items() if k != "mode"}
        item.update(request_id="batch-one-686882", status="pending")
        self.assertEqual(self.client.post("/api/imports", json={"items": [item]}).status_code, 200)
        item.update(status="failed", error="学校接口未授权")
        self.client.post("/api/imports", json={"items": [item]})
        self.client.post("/api/imports", json={"items": [{**item, "request_id": "batch-two-686882", "status": "pending", "error": ""}]})
        reopened = Store(self.settings.root)
        try:
            self.assertEqual(len(reopened.imports()), 2)
            self.assertTrue(any(i["status"] == "failed" and i["error"] == "学校接口未授权" for i in reopened.imports()))
        finally:
            reopened.db.close()

    def test_import_success_requires_matching_session_and_cannot_regress(self):
        item = {k: v for k, v in LESSON.items() if k != "mode"}
        item.update(request_id="batch-686882", status="queued")
        self.assertEqual(self.client.post("/api/imports", json={"items": [item]}).status_code, 400)
        sid = self.create(request_id=item["request_id"])
        item["sid"] = sid
        self.assertEqual(self.client.post("/api/imports", json={"items": [item]}).status_code, 200)
        self.client.post("/api/imports", json={"items": [{**item, "status": "pending", "sid": None}]})
        self.assertEqual(self.client.get("/api/imports").json()[0]["status"], "queued")
        self.assertEqual(self.client.post("/api/imports", json={"items": [{**item, "request_id": "reuse-same-lesson"}]}).status_code, 200)
        self.assertEqual(self.client.post("/api/imports", json={"items": [{**item, "sub_id": "777", "page_url": item["page_url"].replace("686882", "777")}]}).status_code, 400)

    def test_repeated_replay_import_reuses_job_but_explicit_retranscribe_creates_version(self):
        options = dict(mode="replay", source_url="https://video.jw.scut.edu.cn/play/test.mp4")
        one = self.create(**options, request_id="first")
        self.assertEqual(one, self.create(**options, request_id="second"))
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertNotEqual(one, self.create(**options, request_id="third", force_new=True))

    def test_delete_and_restore_keep_covered_duplicates_out_of_the_library(self):
        one = self.create(mode="subtitle")
        two = self.create(mode="subtitle", force_new=True)
        for sid in (one, two):
            self.store.update(sid, stopped=True, status="complete", segments=[{"id": sid+":1", "start": 0, "end": 100, "text": "测试字幕"}])
        self.assertEqual([s["id"] for s in self.client.get("/api/sessions").json()], [two])
        self.assertEqual(self.client.post(f"/api/sessions/{two}/delete").status_code, 200)
        self.assertEqual(self.client.get("/api/sessions").json(), [])
        self.assertEqual(len(self.client.get("/api/sessions?trash=true").json()), 1)
        self.assertTrue((self.store.directory(one)/"subtitles.txt").is_file())
        self.client.post(f"/api/sessions/{two}/restore")
        self.assertEqual([s["id"] for s in self.client.get("/api/sessions").json()], [two])
        active = self.create()
        self.assertEqual(self.client.post(f"/api/sessions/{active}/delete").status_code, 400)

    def test_import_journal_checks_origin_identity_and_does_not_accept_secrets(self):
        item = {k: v for k, v in LESSON.items() if k != "mode"}
        item["request_id"] = "test-import"
        self.assertEqual(self.client.get("/api/imports", headers={"Authorization": ""}).status_code, 401)
        self.assertEqual(self.client.post("/api/imports", json={"items": [{**item, "sub_id": "999"}]}).status_code, 400)
        self.assertEqual(self.client.post("/api/imports", json={"items": [{**item, "source_url": "https://example.com/private.mp4"}]}).status_code, 422)
        self.assertEqual(self.client.get("/api/imports").json(), [])

    def test_local_dashboard_cookie_requires_same_origin(self):
        page = self.client.get("/")
        self.assertIn("HttpOnly", page.headers["set-cookie"])
        self.assertIn("SameSite=strict", page.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/settings", headers={"Authorization": ""}).status_code, 401)
        self.assertEqual(self.client.get("/api/settings", headers={"Authorization": "", "Sec-Fetch-Site": "same-origin"}).status_code, 200)
        self.assertEqual(self.client.get("/api/settings", headers={"Authorization": "", "Sec-Fetch-Site": "cross-site"}).status_code, 401)

    def test_current_class_analysis_switch_is_persisted_and_requires_a_key(self):
        sid = self.create(analysis=False)
        path = f"/api/sessions/{sid}/analysis-preference"
        self.assertEqual(self.client.post(path, json={"enabled": True}).status_code, 400)
        with patch.object(self.settings, "key", return_value="fake-test-key"):
            self.assertTrue(self.client.post(path, json={"enabled": True}).json()["analysis"])
        self.store.enqueue(sid, "analyze", {}, lane="analysis")
        self.assertFalse(self.client.post(path, json={"enabled": False}).json()["analysis"])
        self.assertFalse(self.store.pending(sid, "analysis"))

    def test_audio_repair_preserves_backup_and_replaces_original_chunk_in_order(self):
        sid = self.create()
        self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=wav_bytes())
        job = self.store.claim("asr")
        self.manager.run(job)
        self.store.finish(job["id"])
        response = self.client.post(f"/api/sessions/{sid}/repair")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["repair_count"], 1)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/repair").status_code, 409)
        repair = self.store.claim("asr")
        self.assertEqual(repair["kind"], "repair")
        self.manager.run(repair)
        self.store.finish(repair["id"])
        self.assertEqual(len(self.store.get(sid)["segments"]), 1)
        self.assertEqual(self.store.get(sid)["segments"][0]["chunk"], job["id"])
        self.assertTrue(list(self.store.directory(sid).glob("before-repair-*.json")))

    def test_live_upload_resumes_after_service_restart_without_restarting_capture(self):
        sid = self.create()
        self.store.update(sid, status="interrupted", warning="服务重启")
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=wav_bytes()).status_code, 200)
        self.assertEqual(self.store.get(sid)["status"], "recording")

    def test_live_capture_without_new_audio_is_not_shown_as_recording_forever(self):
        sid = self.create()
        self.store.update(sid, created_at=time.time() - 900, last_received_at=time.time() - 601)
        self.manager.finalize()
        self.assertEqual(self.store.get(sid)["status"], "interrupted")
        self.assertFalse(self.store.get(sid)["stopped"])
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=wav_bytes()).status_code, 200)
        self.assertEqual(self.store.get(sid)["status"], "recording")

    def test_targeted_repair_only_reprocesses_suspect_chunks(self):
        sid = self.create()
        for seq in range(2):
            self.client.post(f"/api/sessions/{sid}/chunks?seq={seq}&start={seq}", content=wav_bytes())
            job = self.store.claim("asr")
            self.manager.run(job)
            self.store.finish(job["id"])
        segments = self.store.get(sid)["segments"]
        segments[0]["text"] = '网络' * 100
        self.store.update(sid, segments=segments)
        response = self.client.post(f"/api/sessions/{sid}/repair?only_suspect=true")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["repair_count"], 1)
        repair = self.store.claim("asr")
        self.assertEqual(repair['payload']['replace_key'], f'{sid}-live-0')
        self.manager.run(repair)
        self.store.finish(repair['id'])
        self.assertEqual(len(self.store.get(sid)['segments']), 2)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/repair?only_suspect=true").json()['repair_count'], 0)

    def test_duplicate_upload_stop_integrity_and_exports(self):
        sid = self.create()
        path = f"/api/sessions/{sid}/chunks?seq=0&start=23"
        self.assertEqual(self.client.post(path, content=wav_bytes()).status_code, 200)
        self.assertTrue(self.client.post(path, content=b"bad").json()["duplicate"])
        self.assertEqual(len(self.store.jobs(sid)), 1)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/stop", json={"last_seq": 1}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/stop", json={"last_seq": 0}).status_code, 200)
        job = self.store.claim("asr")
        self.manager.run(job)
        self.store.finish(job["id"])
        self.manager.finalize()
        session = self.store.get(sid)
        self.assertEqual(session["status"], "complete")
        self.assertEqual(session["segments"][0]["start"], 23)
        self.assertTrue(session["events"])
        self.assertIn("00:00:23,000", (self.store.directory(sid) / "subtitles.srt").read_text(encoding="utf-8"))
        self.assertEqual(self.client.get(f"/api/sessions/{sid}/export").headers["content-type"], "application/zip")
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/chunks?seq=1&start=24", content=wav_bytes()).status_code, 400)

    def test_idempotent_batch_and_import(self):
        sid = self.create(mode="subtitle", request_id="batch-1")
        self.assertEqual(sid, self.create(mode="subtitle", request_id="batch-1"))
        result = self.client.post(f"/api/sessions/{sid}/subtitles", json={"items": [{"BeginSec": 1.5, "EndSec": 3, "Text": "下周交作业"}]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "complete")
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/subtitles", json={"items": []}).status_code, 400)

    def test_malformed_audio_settings_and_course_mismatch(self):
        sid = self.create()
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=b"bad").status_code, 400)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=nan", content=wav_bytes()).status_code, 400)
        self.assertEqual(self.client.post("/api/settings", json={"token": "attacker"}).status_code, 422)
        self.assertEqual(self.client.post("/api/sessions", json={**LESSON, "sub_id": "1"}).status_code, 400)
        self.assertEqual(self.client.post("/api/settings", json={"analysis_window": 0}).status_code, 422)

    @unittest.skipUnless(__import__('os').name == 'nt', "Windows credential vault")
    def test_mobile_pairing_is_local_secret_and_qr_is_generated(self):
        with patch.object(self.settings, "key", return_value="test-key"):
            configured = self.client.post("/api/mobile/setup").json()
        self.assertTrue(configured["pairing_code"].startswith("SCUT1."))
        self.assertTrue(configured["pairing_qr"].startswith("data:image/svg+xml;base64,"))
        self.assertNotIn(configured["pairing_code"], self.client.get("/api/settings").text)
        self.assertNotIn("auth_token", self.client.get("/api/settings").text)
        self.assertEqual(configured["app_id"], "scut-classroom")

    def test_saving_school_subtitles_automatically_queues_notes_without_live_analysis(self):
        sid = self.create(mode="subtitle", analysis=False)
        with patch.object(self.settings, "key", return_value="test-key"):
            response = self.client.post(f"/api/sessions/{sid}/subtitles", json={"items": [
                {"BeginSec": 0, "EndSec": 60, "Text": "周五前完成第二章作业"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.get(sid)["analysis_status"], "queued")
        self.assertFalse(self.store.get(sid)["analysis"])
        self.assertEqual([j["kind"] for j in self.store.jobs(sid)], ["summary"])

    def test_failed_audio_cannot_appear_completed_and_retry_preserves_work(self):
        sid = self.create()
        self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=wav_bytes())
        self.client.post(f"/api/sessions/{sid}/stop", json={"last_seq": 0})
        job = self.store.claim("asr")
        self.store.finish(job["id"], "simulated GPU error")
        self.manager.finalize()
        self.assertEqual(self.store.get(sid)["status"], "failed")
        self.assertTrue(Path(job["payload"]["path"]).exists())
        self.client.post(f"/api/sessions/{sid}/retry")
        self.assertEqual(self.store.jobs(sid)[0]["state"], "pending")

    def test_retry_rebuilds_missing_replay_audio_before_requeueing_asr(self):
        sid = self.create(mode="replay", source_url="https://video.jw.scut.edu.cn/play/test.mp4")
        media = next(item["id"] for item in self.store.jobs(sid) if item["kind"] == "media")
        self.store.finish(media)
        path = self.store.directory(sid) / "media" / "replay-000001.wav"
        jid = self.store.enqueue(sid, "chunk", {"path": str(path), "start": 58, "duration": 60})
        job = self.store.claim("asr")
        with patch.object(self.manager.asr, "transcribe", side_effect=FileNotFoundError()):
            self.manager.execute(job)
        self.assertIn("音频片段已丢失", next(item["error"] for item in self.store.jobs(sid) if item["id"] == jid))
        self.store.update(sid, stopped=True, status="failed", segments=[{"id": "saved", "start": 0, "end": 1, "text": "保留的字幕"}])
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/retry").status_code, 200)
        states = {item["id"]: item["state"] for item in self.store.jobs(sid)}
        self.assertEqual(states[media], "pending", str(states))
        self.assertEqual(states[jid], "waiting_media")
        self.assertEqual(self.store.get(sid)["status"], "downloading")
        self.assertEqual(len(self.store.get(sid)["segments"]), 1)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(wav_bytes())
        self.store.release_waiting_media(sid)
        self.assertEqual({item["id"]: item["state"] for item in self.store.jobs(sid)}[jid], "pending")

    def test_cancel_prevents_queued_processing(self):
        sid = self.create()
        self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=wav_bytes())
        self.client.post(f"/api/sessions/{sid}/cancel")
        self.assertIsNone(self.store.claim("asr"))
        self.assertEqual(self.store.get(sid)["status"], "cancelled")

    def test_asr_lane_scales_with_configured_workers(self):
        self.settings.data["asr_workers"] = 3
        with patch.object(self.manager.mobile, "start"), patch.object(self.manager, "worker"):
            self.manager.start()
        self.assertEqual(sum(t.name.startswith("scut-asr-") for t in self.manager.threads), 3)

    def test_live_audio_has_priority_over_replay(self):
        sid = self.create()
        self.store.enqueue(sid, "chunk", {}, priority=20, key="replay")
        self.store.enqueue(sid, "chunk", {}, priority=0, key="live")
        self.assertEqual(self.store.claim("asr")["id"], "live")

    def test_equal_priority_replays_take_turns_between_courses(self):
        first = self.create(sub_id="686881", page_url="https://video.jw.scut.edu.cn/livingroom?course_id=81526&sub_id=686881")
        second = self.create(sub_id="686880", page_url="https://video.jw.scut.edu.cn/livingroom?course_id=81526&sub_id=686880")
        third = self.create(sub_id="686879", page_url="https://video.jw.scut.edu.cn/livingroom?course_id=81526&sub_id=686879")
        self.store.enqueue(first, "chunk", {}, priority=20, key="first-1")
        self.store.enqueue(first, "chunk", {}, priority=20, key="first-2")
        self.store.enqueue(second, "chunk", {}, priority=20, key="second-1")
        self.store.enqueue(third, "chunk", {}, priority=20, key="third-1")
        seen = set()
        one = self.store.claim("asr", exclude_sessions=seen); seen.add(one["session_id"])
        two = self.store.claim("asr", exclude_sessions=seen); seen.add(two["session_id"])
        three = self.store.claim("asr", exclude_sessions=seen)
        self.assertEqual([one["id"], two["id"], three["id"]], ["first-1", "second-1", "third-1"])

    def test_all_silent_audio_finishes_with_an_explicit_error(self):
        sid = self.create()
        self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=wav_bytes())
        self.client.post(f"/api/sessions/{sid}/stop", json={"last_seq": 0})
        job = self.store.claim("asr")
        with patch.object(self.manager.asr, "transcribe", return_value=[]):
            self.manager.run(job)
        self.store.finish(job["id"])
        self.manager.finalize()
        session = self.store.get(sid)
        self.assertEqual(session["status"], "failed")
        self.assertIn("没有识别出可用语音", session["error"])

    def test_summary_yields_between_packs_to_realtime_analysis(self):
        self.settings.data["analysis_parallelism"] = 1
        archive = self.create(mode="subtitle")
        live = self.create()
        self.store.update(archive, analysis=True, segments=[{"id": f"archive-{i}", "start": i*10, "end": i*10+9, "text": "课程内容"*800} for i in range(4)])
        self.store.update(live, analysis=True, segments=[{"id": "live", "start": 0, "end": 5, "text": "请扫码签到"}])
        calls = []
        def analyze(segments, summary=False):
            calls.append(segments[0]["id"])
            if len(calls) == 1:
                self.store.enqueue(live, "analyze", {"start": 0, "end": 10}, lane="analysis", priority=0)
            return {"overview": "测试", "topics": [], "events": []}
        self.store.enqueue(archive, "summary", {}, lane="analysis")
        with patch.object(self.manager.analyzer, "synthesize", return_value={"title": "测试", "overview": "概览", "groups": []}), patch.object(self.manager.analyzer, "analyze", side_effect=analyze):
            self.manager.execute(self.store.claim("analysis"))
        self.assertEqual(calls[1], "live")
        self.assertGreater(len(calls), 2)

    def test_repeated_analysis_clicks_reuse_job_and_do_not_retranscribe(self):
        sid = self.create(mode="subtitle")
        self.store.update(sid, stopped=True, status="complete", analysis_status="failed", warning="旧错误",
                          segments=[{"id": "s", "start": 0, "end": 2, "text": "课程内容"}])
        with patch.object(self.settings, "key", return_value="test-only-key"):
            for _ in range(2):
                response = self.client.post(f"/api/sessions/{sid}/analyze")
                self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.store.jobs(sid)), 1)
        self.assertEqual(self.store.jobs(sid)[0]["kind"], "summary")
        self.assertEqual(self.store.get(sid)["warning"], "")

    def test_restart_resumes_asr_chunk_and_preserves_received_audio(self):
        sid = self.create()
        self.client.post(f"/api/sessions/{sid}/chunks?seq=0&start=0", content=wav_bytes())
        self.store.claim("asr")
        self.store.db.close()
        self.store = Store(self.settings.root)
        self.assertEqual(self.store.get(sid)["status"], "interrupted")
        self.assertEqual(self.store.jobs(sid)[0]["state"], "pending")


class AnalysisTests(unittest.TestCase):
    segments = [{"id": "s1", "start": 10, "end": 15, "text": "请在周五前提交第二章作业。"}]

    def test_hallucinated_evidence_and_chapters_are_rejected(self):
        body = {"events": [
            {"category": "assignment", "evidence": "周五前提交第二章作业", "segment_ids": ["s1"], "message": "周五前交作业", "confidence": 0.9},
            {"category": "quiz", "evidence": "明天考试", "segment_ids": ["s1"], "message": "明天考试"},
            {"category": "grading", "evidence": "满分", "segment_ids": ["fake"], "message": "满分"}],
            "topics": [{"title": "作业安排", "chapter": "第八章", "segment_ids": ["s1"]}], "overview": "作业安排"}
        result = Analyzer.validate(body, self.segments)
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(result["topics"][0]["chapter"])
        self.assertEqual(result["events"][0]["start"], 10)

    def test_local_rules_negation_and_llm_event_dedup(self):
        self.assertFalse(rule_events([{"id": "x", "start": 0, "end": 1, "text": "今天不用签到"}]))
        rules = rule_events(self.segments)
        incoming = {**rules[0], "source": "deepseek", "message": "周五前提交作业", "confidence": .95}
        merged = merge_events(rules, [incoming, incoming])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "deepseek")

    def test_deepseek_request_json_contract_and_data_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(Path(temp))
            def handler(request):
                body = json.loads(request.content)
                self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
                self.assertEqual(body["response_format"], {"type": "json_object"})
                self.assertNotIn("token", body["messages"][1]["content"])
                self.assertIn("未经信任", body["messages"][0]["content"])
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"events":[],"topics":[],"overview":"测试"}'}}]})
            with patch.object(settings, "key", return_value="test-only-key"):
                result = Analyzer(settings, httpx.MockTransport(handler)).analyze(self.segments)
            self.assertEqual(result["overview"], "测试")

    def test_openai_request_contract_uses_completion_tokens_and_saved_key(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(Path(temp))
            settings.data["llm_provider"] = "openai"
            def handler(request):
                body = json.loads(request.content)
                self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
                self.assertEqual(request.headers["authorization"], "Bearer openai-test-key")
                self.assertEqual(body["model"], "gpt-5-mini")
                self.assertIn("max_completion_tokens", body)
                self.assertNotIn("max_tokens", body)
                # GPT-5 rejects a non-default temperature, so the field is omitted.
                self.assertNotIn("temperature", body)
                self.assertEqual(body["response_format"], {"type": "json_object"})
                self.assertNotIn("thinking", body)
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"events":[],"topics":[],"overview":"测试"}'}}]})
            with patch.object(settings, "openai_key", return_value="openai-test-key"), \
                 patch.object(settings, "key", return_value="wrong-provider-key"):
                result = Analyzer(settings, httpx.MockTransport(handler)).analyze(self.segments)
            self.assertEqual(result["overview"], "测试")

    def test_openai_legacy_models_still_send_temperature(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(Path(temp))
            settings.data.update(llm_provider="openai", openai_model="gpt-4o-mini")
            def handler(request):
                body = json.loads(request.content)
                self.assertEqual(body["temperature"], 0.1)
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"events":[],"topics":[],"overview":""}'}}]})
            with patch.object(settings, "openai_key", return_value="openai-test-key"):
                Analyzer(settings, httpx.MockTransport(handler)).analyze(self.segments)

    def test_openai_newer_models_omit_temperature(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(Path(temp))
            settings.data["llm_provider"] = "openai"
            for model in ("gpt-5-mini", "gpt-6.1-sol", "o3-mini", "gpt-10"):
                settings.data["openai_model"] = model
                self.assertIsNone(settings.llm()["temperature"], model)

    def test_temperature_rejected_by_model_retries_without_it(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(Path(temp))
            settings.data.update(llm_provider="openai", openai_model="gpt-4o-mini")
            sent = []
            def handler(request):
                sent.append("temperature" in json.loads(request.content))
                if len(sent) == 1:
                    return httpx.Response(400, json={"error": {"param": "temperature",
                        "message": "Unsupported value: 'temperature' does not support 0.1 with this model."}})
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"events":[],"topics":[],"overview":"测试"}'}}]})
            with patch.object(settings, "openai_key", return_value="openai-test-key"):
                result = Analyzer(settings, httpx.MockTransport(handler)).analyze(self.segments)
            self.assertEqual(sent, [True, False])
            self.assertEqual(result["overview"], "测试")

    def test_pool_timeout_rebuilds_client_and_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(Path(temp))
            settings.data.update(llm_provider="openai", openai_model="gpt-4o-mini")
            sent = []
            def handler(request):
                sent.append(request)
                if len(sent) == 1:
                    raise httpx.PoolTimeout("pool saturated by dead tunnels")
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"events":[],"topics":[],"overview":"恢复"}'}}]})
            resets, real_reset = [], Analyzer._reset_client
            def counting_reset(analyzer, provider):
                resets.append(provider)
                return real_reset(analyzer, provider)
            with patch.object(settings, "openai_key", return_value="openai-test-key"), \
                 patch.object(Analyzer, "_reset_client", counting_reset):
                analyzer = Analyzer(settings, httpx.MockTransport(handler))
                result = analyzer.analyze(self.segments)
            self.assertEqual(resets, ["openai"])
            self.assertEqual(len(sent), 2)
            self.assertEqual(result["overview"], "恢复")
            self.assertIsNotNone(analyzer.proxy_client)

    def test_overlap_deduplicates_boundary_but_keeps_later_repetition(self):
        previous = [{"id": "a", "chunk": "a", "start": 0, "end": 8, "text": "今天布置课后作业"}]
        result = append_segments(previous, [{"start": 0, "end": 5, "text": "课后作业下周提交"}], "b", 7, 8)
        self.assertEqual(result[-1]["text"], "下周提交")
        self.assertEqual(result[-1]["start"], 8)
        later = append_segments(result, [{"start": 0, "end": 2, "text": "课后作业"}], "c", 30, 8)
        self.assertEqual(later[-1]["text"], "课后作业")


class MediaTests(unittest.TestCase):
    def test_source_validation(self):
        for url in ["file:///etc/passwd", "http://127.0.0.1/private", "https://video.jw.scut.edu.cn.evil.test/a.mp4", "https://user:pass@video.jw.scut.edu.cn/a.mp4"]:
            with self.assertRaises(ValueError):
                validate_media_url(url)
        self.assertTrue(validate_media_url("https://video.jw.scut.edu.cn/play/a.mp4"))

    def test_invalid_wav_and_hls_nested_url(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "bad.wav"
            path.write_bytes(wav_bytes(rate=48000))
            with self.assertRaises(ValueError):
                validate_wav(path)
            settings = Settings(root)
            downloader = Downloader(settings)
            def download(url, dest, **kwargs):
                validate_media_url(url)
                dest.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1/private\n#EXT-X-ENDLIST", encoding="utf-8")
                return url
            with patch.object(downloader, "download", side_effect=download):
                with self.assertRaises(ValueError):
                    downloader.fetch("https://video.jw.scut.edu.cn/a.m3u8", root)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI only")
    def test_key_is_encrypted_for_current_windows_user(self):
        key = "synthetic-test-value-not-a-real-api-key"
        encrypted = protect(key)
        self.assertNotIn(key, encrypted)
        self.assertEqual(protect(encrypted, decrypt=True), key)


if __name__ == "__main__":
    unittest.main()
