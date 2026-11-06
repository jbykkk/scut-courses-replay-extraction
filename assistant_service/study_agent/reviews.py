"""Durable spaced review, local inbox and the existing encrypted phone outbox."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .planner import TZ, review_interval
from .storage import StudyStore, digest
from .learning import next_review_time, review_candidates


def next_time(now, days, hour=19):
    day = datetime.fromtimestamp(now, TZ) + timedelta(days=days)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()


def create_review(store, *, course, topic, citations, days=1, now=None, source=""):
    now = time.time() if now is None else now
    rid = digest(["review", course, source or topic])[:32]
    try:
        old = store.get(rid, "review")
        if old.get("status") == "active":
            return old
        if old.get("status") == "paused":
            return update_review(store, rid, action="resume", now=now)
    except KeyError:
        pass
    return store.save("review", {"course": course, "topic": topic, "citations": citations, "source": source,
        "status": "active", "due_at": next_review_time(store, now, days, course), "cycle": 0, "attempts": [], "created_at": now}, rid)


def update_review(store, rid, *, action, rating=None, now=None, feedback=None):
    now = time.time() if now is None else now
    item = store.get(rid, "review")
    if action == "pause":
        item["status"] = "paused"
    elif action == "resume":
        if item["status"] == "active":
            return item
        due = item["due_at"]
        if due <= now:
            due = next_review_time(store, now, 0, item["course"])
            if due <= now:
                due = next_review_time(store, now, 1, item["course"])
        item.update(status="active", due_at=due, cycle=item["cycle"] + 1)
    elif action == "snooze":
        item.update(due_at=next_review_time(store, now, 1, item["course"]), cycle=item["cycle"] + 1)
    elif action == "complete":
        if rating not in {1, 2, 3, 4}:
            raise ValueError("请选择这次能回忆到什么程度")
        effective = min(rating, feedback["suggested_rating"]) if feedback and feedback.get("status") == "model_checked" else rating
        prior = [{**a, "rating": a.get("effective_rating", a["rating"])} for a in item["attempts"]]
        interval = review_interval(prior, effective)
        item["attempts"].append({"rating": rating, "effective_rating": effective, "feedback": feedback, "at": now})
        item.update(due_at=next_review_time(store, now, interval, item["course"]), cycle=item["cycle"] + 1, status="active")
    else:
        raise ValueError("无效的复习操作")
    for alert in store.list("study_alert"):
        if alert.get("review_id") == rid and not alert.get("handled"):
            store.save("study_alert", {**alert, "handled": True}, alert["id"])
    return store.save("review", item, rid)


class ReviewScheduler:
    def __init__(self, root):
        self.store = StudyStore(root)
        self.last_scan = 0
        self.last_error = ""

    def delivery_state(self, alert_id, now=None):
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, TZ)
        try:
            alert = self.store.get(alert_id, "study_alert")
            review = self.store.get(alert["review_id"], "review")
        except KeyError:
            return "discard"
        if alert.get("handled") or review["status"] != "active" or review["due_at"] != alert["due_at"]:
            return "discard"
        eligible = next((r for r in review_candidates(self.store, now) if r["id"] == review["id"]), None)
        if not eligible or eligible.get("source_stale"):
            return "discard"
        if not eligible.get("today"):
            return "wait"
        delivered = sum(datetime.fromtimestamp(a.get("delivered_at", 0), TZ).date() == local.date()
                        for a in self.store.list("study_alert") if a.get("delivered_at"))
        return "send" if 8 <= local.hour < 22 and delivered < 2 else "wait"

    def delivered(self, alert_id):
        item = self.store.get(alert_id, "study_alert")
        self.store.save("study_alert", {**item, "delivered_at": time.time()}, alert_id)

    def tick(self, notifier=None, *, now=None):
        now = time.time() if now is None else now
        if now - self.last_scan < 30:
            return
        self.last_scan = now
        today = datetime.fromtimestamp(now, TZ)
        for item in review_candidates(self.store, now):
            if item.get("status") != "active" or item["due_at"] > now:
                continue
            aid = digest(["review-alert", item["id"], item["cycle"]])[:32]
            try:
                alert = self.store.get(aid, "study_alert")
            except KeyError:
                alert = self.store.save("study_alert", {"review_id": item["id"], "course": item["course"],
                    "topic": item["topic"], "created_at": now, "due_at": item["due_at"], "handled": False,
                    "mobile_queued": False}, aid)
            if alert["handled"] or alert["mobile_queued"] or not notifier or not item["today"] or not 8 <= today.hour < 22:
                continue
            # Missed days collapse into this one current review, never one alert per missed day.
            sent_today = sum(datetime.fromtimestamp(a.get("queued_at", 0), TZ).date() == today.date()
                             for a in self.store.list("study_alert") if a.get("mobile_queued"))
            if sent_today >= 2:
                continue
            payload = {"id": aid, "course": item["course"], "lesson": "学习复习", "category": "reminder",
                "label": "复习一下", "priority": "normal", "message": "花几分钟回忆：" + item["topic"][:180],
                "details": {"action": "先回忆，再对照资料。", "requirements": "；".join(item["reasons"]), "estimated_minutes": item["minutes"]},
                "occurred_at": now, "start": 0, "confidence": 1, "source": "study-review"}
            if notifier.queue("classroom-alert", payload, "study-review:" + aid):
                self.store.save("study_alert", {**alert, "mobile_queued": True, "queued_at": now}, aid)
