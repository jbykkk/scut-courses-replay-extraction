import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from assistant_service.study_agent.dialogue import converse, completed_request, home
from assistant_service.study_agent.reviews import create_review, update_review, ReviewScheduler
from assistant_service.study_agent.storage import StudyStore


class StudyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StudyStore(self.root)
        self.store.source(origin="book", course="OS", title="Book", kind="material", fingerprint="book",
                          chunks=[{"text": "Processes own resources.", "locator": {"page": 1}}])
        self.payload = dict(conversation="main", client_id="message-1", question="OS processes", course="",
                            purpose="auto", attachments=[], review_id=None)

    def test_failed_retry_keeps_original_input_and_reuses_completed_result(self):
        with patch("assistant_service.study_agent.dialogue.run_agent", side_effect=[RuntimeError("offline"), {"claims": []}]) as run:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                converse(self.store, None, self.root, **self.payload)
            failed = home(self.store)["turns"][0]
            self.assertEqual(failed["course"], "OS")
            self.assertEqual(failed["request_input"], self.payload)
            result = converse(self.store, None, self.root, **failed["request_input"])
            cached = completed_request(self.store, self.payload)
            self.assertEqual(result["id"], failed["id"])
            self.assertEqual(cached["id"], result["id"])
            converse(self.store, None, self.root, **self.payload)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(len(home(self.store)["turns"]), 1)
            with self.assertRaises(ValueError):
                completed_request(self.store, {**self.payload, "question": "Changed"})

    def test_concurrent_duplicate_requests_execute_once(self):
        entered, release, duplicate_entered = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def run(*args, **kwargs):
            calls.append(kwargs)
            (entered if len(calls) == 1 else duplicate_entered).set()
            self.assertTrue(release.wait(5))
            return {"claims": []}

        with patch("assistant_service.study_agent.dialogue.run_agent", side_effect=run), ThreadPoolExecutor(2) as pool:
            first = pool.submit(converse, self.store, None, self.root, **self.payload)
            self.assertTrue(entered.wait(5))
            second = pool.submit(converse, StudyStore(self.root), None, self.root, **self.payload)
            try:
                self.assertFalse(duplicate_entered.wait(.1))
            finally:
                release.set()
            self.assertEqual(first.result(5)["id"], second.result(5)["id"])
        self.assertEqual(len(calls), 1)

    def test_resume_and_readd_preserve_history_and_do_not_replay_old_alerts(self):
        now = 1793000000
        review = create_review(self.store, course="OS", topic="Processes", citations=[], source="turn-1", now=now)
        learned = update_review(self.store, review["id"], action="complete", rating=3, now=now)
        alert = self.store.save("study_alert", {"review_id": review["id"], "due_at": learned["due_at"], "handled": False})
        update_review(self.store, review["id"], action="pause", now=now)
        state = home(self.store, now=now)
        self.assertEqual(state["reviews"], [])
        self.assertEqual(state["paused_reviews"][0]["id"], review["id"])
        later = learned["due_at"] + 86400
        resumed = update_review(self.store, review["id"], action="resume", now=later)
        for key in ("id", "attempts", "citations", "source", "created_at"):
            self.assertEqual(resumed[key], learned[key])
        self.assertGreater(resumed["due_at"], later)
        self.assertEqual(ReviewScheduler(self.root).delivery_state(alert["id"], now=later), "discard")
        again = update_review(self.store, review["id"], action="resume", now=later + 60)
        self.assertEqual(again["cycle"], resumed["cycle"])
        self.assertEqual(again["due_at"], resumed["due_at"])
        update_review(self.store, review["id"], action="pause", now=later)
        readded = create_review(self.store, course="OS", topic="Processes", citations=[], source="turn-1", now=later)
        self.assertEqual(readded["id"], review["id"])
        self.assertEqual(readded["attempts"], learned["attempts"])
        self.assertEqual(readded["created_at"], learned["created_at"])

    def test_future_review_time_is_preserved_on_resume(self):
        review = create_review(self.store, course="OS", topic="Threads", citations=[], now=1793000000)
        update_review(self.store, review["id"], action="pause", now=1793000000)
        resumed = update_review(self.store, review["id"], action="resume", now=1793000001)
        self.assertEqual(resumed["due_at"], review["due_at"])
