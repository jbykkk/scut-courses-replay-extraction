"""A small, auditable budget allocator shared by chat and external agents."""
from __future__ import annotations

import re
import time
from datetime import datetime

from .learning import preferences, review_candidates
from .planner import TZ
from .storage import terms
from .tutoring import learning_gaps
from ..telemetry import emit


def is_daily_question(question):
    return bool(re.search(r"(?:今天|今日|今晚|现在|只有|有空).{0,45}(?:复习|先学|先做|学什么|做什么|安排|清单)|(?:复习清单|先学什么|先做什么)", question)) and not bool(re.search(r"每天|每晚|每周|周期", question))


def requested_minutes(question):
    match = re.search(r"(?<!\d)(\d{1,4})\s*(?:分钟|min\b)", question, re.I)
    if match:
        value = int(match[1])
    elif "半小时" in question:
        value = 30
    elif match := re.search(r"([一二两三四五六七八九十]+)\s*分钟", question):
        digits = {c: i for i, c in enumerate("零一二三四五六七八九")}; digits["两"] = 2
        text = match[1]
        if "十" in text:
            a, b = text.split("十", 1)
            value = (digits.get(a, 1) * 10) + digits.get(b, 0)
        else:
            value = digits.get(text, 0)
    elif match := re.search(r"(\d+(?:\.5)?|一|两|二|三|四)\s*(?:个)?小时", question):
        value = round(float({"一": 1, "两": 2, "二": 2, "三": 3, "四": 4}.get(match[1], match[1])) * 60)
    else:
        return None
    if not 1 <= value <= 240:
        raise ValueError("一次安排 1–240 分钟即可；更长的计划可在任务与日历中安排。")
    return value


def _current_citations(store, citations):
    if not citations:
        return False
    for c in citations:
        try:
            e = store.evidence(c["evidence_id"])
            if not e["active"] or e.get("policy", {}).get("excluded"):
                return False
        except KeyError:
            return False
    return True


def _related(a, b):
    x, y = terms(a), terms(b)
    return bool(x and y and len(x & y) / min(len(x), len(y)) >= .5)


def daily_plan(store, minutes=30, course="", now=None):
    """Read-only. Estimates and a date-only deadline never become guarantees."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 240:
        raise ValueError("可用时间须为 1–240 分钟的整数")
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, TZ)
    emit("tool.start", "安排今日复习", {"tool": "plan_today_review", "arguments": {"minutes": minutes, "course": course}})
    candidates, warnings, blocked = [], [], []
    tasks = [t for t in store.list("task") if t.get("status") == "accepted" and not t.get("completed") and (not course or t["course"] == course)]
    for task in tasks:
        reasons, days = [], None
        deadline = task.get("deadline")
        if deadline:
            try:
                days = (datetime.fromisoformat(deadline).date() - local.date()).days
            except (ValueError, TypeError):
                warnings.append(task["title"] + "的截止日期无法读取，先核对日期。")
        if days is not None:
            reasons.append("已过截止日期，先确认能否补交" if days < 0 else "今天截止" if days == 0 else f"还有 {days} 天截止")
        else:
            reasons.append("截止日期未确认")
        maximum = task.get("estimate_max")
        remaining = max(0, (maximum or 30) - (task.get("spent_minutes") or 0))
        if remaining == 0:
            remaining = 5
            reasons.append("已达到预计用时，先检查完成情况")
        score = 110 if days is not None and days <= 1 else 85 if days is not None and days <= 3 else 65 if days is not None and days <= 7 else 35
        reasons.append(f"按你的估计还需约 {remaining} 分钟" if maximum else "总用时未知，先试做一小段")
        candidates.append({"kind": "task", "id": task["id"], "course": task["course"], "title": task["title"],
            "deadline": deadline, "priority": score, "minutes": min(15, remaining), "remaining_estimate": remaining,
            "reasons": reasons, "citations": task.get("citations", []),
            "action": "先检查提交要求和已有进度，再做下一小步；时间到时记录卡点。" if days is None or days >= 0 else "先核对补交安排，再决定是否继续投入。"})
    reviews = [r for r in review_candidates(store, now) if not course or r["course"] == course]
    for review in reviews:
        if review.get("source_stale") or not _current_citations(store, review.get("citations", [])):
            blocked.append({"course": review["course"], "title": review["topic"], "reason": "原文已更新、停用或缺失，先核对来源。"})
            continue
        attempts = review.get("attempts", [])
        rating = attempts[-1].get("effective_rating", attempts[-1].get("rating", 2)) if attempts else None
        due = review["due_at"] <= now
        if not due:
            continue  # A snoozed review is not silently brought back into today's list.
        reasons = ["上次作答仍有卡点" if rating is not None and rating <= 2 else "已到间隔复习时间"]
        related = [t for t in tasks if t["course"] == review["course"] and _related(t["title"], review["topic"])]
        score = 76 if rating is not None and rating <= 2 else 55
        if related:
            score += 12; reasons.append("与待完成任务“" + related[0]["title"] + "”有关")
        score += min(6, max(0, int((now - review["due_at"]) / 86400)))
        candidates.append({"kind": "review", "id": review["id"], "review_id": review["id"], "course": review["course"],
            "title": review["topic"], "priority": score, "minutes": review["minutes"], "reasons": reasons,
            "citations": review.get("citations", []), "mode": review["mode"],
            "action": "先不看资料解释关键条件，再做一道题，最后对照原文检查。"})
    # Weak answers also count before the student explicitly creates a spaced-review card.
    practices, all_reviews = store.list("practice"), store.list("review")
    for p in practices:
        if course and p["course"] != course or not p.get("attempts"):
            continue
        last = p["attempts"][-1]
        feedback = last.get("feedback") or {}
        rating = min(last["rating"], feedback.get("suggested_rating", 4)) if feedback.get("status") == "model_checked" else last["rating"]
        if rating > 2 or now - last.get("at", now) > 30 * 86400:
            continue
        if any(r.get("source") == p["id"] or (r["course"] == p["course"] and _related(r["topic"], p["question"])) for r in all_reviews):
            continue
        # A later attempt on the same topic supersedes an older weak answer for
        # prioritisation. This is not a claim of objective mastery.
        if any(other["id"] != p["id"] and other["course"] == p["course"] and other.get("attempts")
               and other["attempts"][-1].get("at", 0) > last.get("at", 0)
               and _related(other["question"], p["question"])
               and {c["evidence_id"] for c in other.get("citations", [])} & {c["evidence_id"] for c in p.get("citations", [])}
               for other in practices):
            continue
        if not _current_citations(store, p.get("citations", [])):
            continue
        candidates.append({"kind": "practice", "id": p["id"], "course": p["course"], "title": p["question"],
            "priority": 74, "minutes": 8, "reasons": ["这道题上次还没弄清楚（依据作答反馈或自评）"],
            "citations": p["citations"], "action": "针对刚才没讲清的条件换一道基础题，再回到原问题。" if local.date() == datetime.fromtimestamp(last.get("at", now), TZ).date() else "重述题目条件，试着独立作答，再核对上次遗漏的地方。"})
    # WorkBuddy checkpoints carry student self-report, never an automatic grade.
    # Existing spaced reviews own their schedule (including snoozes); do not add
    # the same topic back through a second channel.
    for gap in learning_gaps(store, now, course):
        if any(r["course"] == gap["course"] and _related(r["topic"], gap["topic"]) for r in all_reviews):
            continue
        if any(c["course"] == gap["course"] and _related(c["title"], gap["topic"]) for c in candidates):
            continue
        candidates.append({"kind": "learning_gap", "id": gap["id"], "session_id": gap["session_id"],
            "course": gap["course"], "title": gap["topic"], "priority": 74, "minutes": 8,
            "reasons": ["你在最近的学习会话中表示仍有卡点（自评，不是自动判分）"],
            "citations": gap["citations"], "action": "接着上次的问题试讲关键条件，再对照原文做一道变式题。"})
    calibration = False
    has_learning_feedback = any(not course or t["course"] == course for t in store.list("learning_topic"))
    if not candidates and not reviews and not store.list("practice") and not has_learning_feedback:
        # Cold start: do not label a random recent topic as a weakness. One short
        # diagnostic gives the learner something to do without a setup questionnaire.
        sources = store.sources(course)
        sources.sort(key=lambda s: (s["title"][:10] if re.match(r"20\d\d-\d\d-\d\d", s["title"]) else "", s["created_at"]), reverse=True)
        for s in sources:
            try:
                e = store.evidence(s["id"] + ":0")
            except KeyError:
                continue
            if e.get("policy", {}).get("excluded"):
                continue
            candidates.append({"kind":"diagnostic", "id":s["id"], "course":s["course"], "title":"回忆“" + s["title"] + "”中的一个概念",
                "priority":0, "minutes":5, "reasons":["还没有可用于排序的复习反馈，先用一道题确认当前状态"], "citations":[],
                "action":"先做一道简短的回忆题。反馈后再决定剩余时间花在哪里。"})
            calibration = True
            break
    candidates.sort(key=lambda c: (-c["priority"], c.get("deadline") or "9999", c["course"], c["id"]))
    steps, deferred, used = [], [], 0
    for item in candidates:
        remaining = minutes - used
        if remaining < 3:
            deferred.append(item); continue
        length = min(item["minutes"], remaining)
        steps.append({**item, "minutes": length, "from_minute": used, "to_minute": used + length,
                      "partial": length < item["minutes"] or item["kind"] == "task" and length < item["remaining_estimate"]})
        used += length
    for step in steps:
        if step["kind"] == "task" and step["partial"]:
            step["reasons"] = [*step["reasons"], "这里只安排今天的一段，不代表能完成整项任务"]
    if tasks and not any(t.get("deadline") for t in tasks):
        warnings.append("任务还没有确认的截止日期，暂按复习反馈与任务估计排序。")
    if calibration:
        warnings.append("还没有确认的任务期限或薄弱点记录。这是一轮试做，不代表这门课最难；剩余时间留到作答后再排。")
    if not candidates:
        warnings.append("还没有可排的到期任务或复习记录。选一门课做一道题，或补充最近要交的作业即可。")
    if minutes < 3:
        warnings.append("这次不足 3 分钟，先看一条原文或核对一个截止日期，不安排完整练习。")
    # Record conflicts as facts without interpreting '30 minutes' as a wall-clock window.
    busy = [b for t in store.list("timetable") if t.get("status") == "accepted" for b in t.get("busy", []) if b.get("start", "").startswith(local.date().isoformat())]
    result = {"date": local.date().isoformat(), "course": course, "budget_minutes": minutes, "used_minutes": used,
        "buffer_minutes": minutes - used, "steps": steps, "deferred": [{k: c[k] for k in ("id", "course", "title", "reasons")} for c in deferred[:8]],
        "needs_source_check": blocked[:6], "warnings": warnings,
        "busy_today": [{k: b.get(k) for k in ("start", "end")} for b in busy[:12]],
        "basis": "只用已确认任务、到期复习和最近作答；分钟数是建议。未指定开始时刻，不写入日历。",
        "generated_at": now}
    emit("tool.finish", "今日清单已排好", {"tool": "plan_today_review", "budget": minutes, "used": used,
         "steps": [{k: s[k] for k in ("id", "kind", "minutes", "reasons")} for s in steps]})
    return result


def daily_reply(store, question, course="", now=None):
    requested = requested_minutes(question)
    budget = requested if requested is not None else preferences(store)["daily_minutes"]
    plan = daily_plan(store, budget, course, now)
    return {"status": "ready", "reply": (f"先按你设定的 {budget} 分钟安排。" if requested is None else f"这 {budget} 分钟可以这样用。"),
            "daily_plan": plan, "claims": [], "proposals": [], "uncertainties": []}
