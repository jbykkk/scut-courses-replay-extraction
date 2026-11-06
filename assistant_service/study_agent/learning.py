"""Transparent review prioritisation, source freshness and grounded answer feedback."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from pydantic import Field

from .planner import TZ, stamp
from .prompt_catalog import read as read_prompt
from .context import evidence_payload
from ..telemetry import traced, phase, compact
from .reasoner import Strict, Citation, Claim, Final, check_citations


class Feedback(Strict):
    summary: str = Field(min_length=1, max_length=1000)
    missing: list[str] = Field(default_factory=list, max_length=4)
    suggested_rating: int = Field(ge=1, le=4)
    citations: list[Citation] = Field(min_length=1, max_length=4)


@traced("feedback")
def answer_feedback(store, provider, practice, answer):
    try:
        evidence = [store.evidence(c["evidence_id"]) for c in practice.get("citations", [])]
    except KeyError:
        evidence = []
    if not any(e["kind"] not in {"asr", "school_subtitle", "image_reading", "student_report"} for e in evidence):
        return {"status": "unverified", "summary": "参考答案目前也只有转写依据，暂不据此判断对错。请补充题目或教材。", "missing": []}
    if any(not e["active"] or e.get("policy", {}).get("excluded") for e in evidence):
        return {"status": "stale", "summary": "这道题的来源已更新或停用，需要先核对新版本。", "missing": []}
    try:
        with phase("feedback", 1500):
            raw = provider.complete([{"role": "system", "content":
            read_prompt("feedback-v1")},
            {"role": "user", "content": json.dumps({"question": practice["question"], "student_answer": answer,
                "source_data": [evidence_payload(e) for e in evidence]}, ensure_ascii=False, separators=(",", ":"))}], 25)
        value = Feedback.model_validate_json(raw)
        wrapper = Final(action="finish", claims=[Claim(text=value.summary, citations=value.citations)])
        check_citations(wrapper, {e["id"]: e for e in evidence})
        return {**value.model_dump(), "status": "model_checked", "label": "依据资料的模型反馈，可有误，不代表考试评分"}
    except Exception:
        return {"status": "unavailable", "summary": "本次没有得到可靠反馈，已保存作答，暂按你的自评安排复习。", "missing": []}


def preferences(store):
    try: return store.get("review-preferences", "review_preferences")
    except KeyError: return {"daily_minutes": 15, "preferred_hour": 19}


def review_candidates(store, now=None):
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, TZ)
    tasks = [t for t in store.list("task") if t.get("status") == "accepted" and not t.get("completed")]
    result = []
    for review in store.list("review"):
        if review.get("status") != "active": continue
        stale = False
        for c in review.get("citations", []):
            try:
                e = store.evidence(c["evidence_id"])
                stale |= not e["active"] or e.get("policy", {}).get("excluded", False)
            except KeyError: stale = True
        attempts = review.get("attempts", [])
        rating = attempts[-1].get("effective_rating", attempts[-1]["rating"]) if attempts else 2
        age = max(0, (now - review["due_at"]) / 86400)
        score = min(age, 20) * 2 + (5-rating) * 3
        reasons = ["上次还缺关键理解" if rating <= 2 else "到间隔复习时间了"]
        if not attempts: reasons = ["还没有做过回忆练习"]
        for task in tasks:
            if task.get("course") != review["course"] or not task.get("deadline"): continue
            days = (datetime.fromisoformat(task["deadline"]).date() - local.date()).days
            if 0 <= days <= 14:
                score += 15 - days
                reasons.append(f"本课已确认任务还有 {days} 天截止")
                break
        minutes = 8 if rating <= 2 and attempts else 5
        mode = "prerequisite" if rating <= 2 and attempts else "application" if rating == 3 else "connection" if rating == 4 else "recall"
        result.append({**review, "priority": round(score, 1), "minutes": minutes, "reasons": reasons,
                       "mode": mode, "source_stale": stale})
    result.sort(key=lambda r: (r["source_stale"], -r["priority"], r["due_at"]))
    budget = preferences(store)["daily_minutes"]
    spent_today = sum(5 for r in result for a in r.get("attempts", []) if datetime.fromtimestamp(a["at"], TZ).date() == local.date())
    remaining = max(0, budget - spent_today)
    for r in result:
        due = r["due_at"] <= now and not r["source_stale"]
        r["today"] = due and r["minutes"] <= remaining
        if r["today"]: remaining -= r["minutes"]
        if r["source_stale"]: r["reasons"] = ["来源已更新或停用，先核对新资料"]
    return result


def next_review_time(store, now, days, course):
    prefs = preferences(store)
    when = (datetime.fromtimestamp(now, TZ) + timedelta(days=days)).replace(hour=prefs["preferred_hour"], minute=0, second=0, microsecond=0)
    # Bring a review forward before a confirmed deadline; never invent an exam date.
    for task in store.list("task"):
        if task.get("course") != course or task.get("status") != "accepted" or task.get("completed") or not task.get("deadline"): continue
        deadline = datetime.fromisoformat(task["deadline"]).replace(tzinfo=TZ, hour=prefs["preferred_hour"]) - timedelta(days=1)
        if now + 3600 < deadline.timestamp() < when.timestamp(): when = deadline
    busy = [b for t in store.list("timetable") if t.get("status") == "accepted" for b in t.get("busy", [])]
    for _ in range(20):
        overlaps = [stamp(b["end"]) for b in busy if stamp(b["start"]) <= when < stamp(b["end"])]
        if not overlaps: break
        when = max(overlaps) + timedelta(minutes=10)
        if when.hour >= 22:
            when = (when + timedelta(days=1)).replace(hour=prefs["preferred_hour"], minute=0)
    return when.timestamp()
