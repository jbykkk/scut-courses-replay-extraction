"""Persistent dialogue; explicit requests enter typed, durable operation jobs."""
from __future__ import annotations

import time
import re
import threading
from functools import wraps
from weakref import WeakValueDictionary

from .attachments import prepare_attachments
from .reasoner import run_agent
from .storage import digest
from .learning import review_candidates, preferences
from .operations import choose_operation
from .daily import is_daily_question, daily_reply
from .classroom_search import is_classroom_question
from ..telemetry import traced, current, emit


_request_locks = WeakValueDictionary()
_request_guard = threading.Lock()


def serialized_request(fn):
    @wraps(fn)
    def call(store, provider, root, **kwargs):
        key = (str(store.path.resolve()), kwargs["conversation"], kwargs["client_id"])
        with _request_guard:
            lock = _request_locks.setdefault(key, threading.Lock())
        with lock:
            return fn(store, provider, root, **kwargs)
    return call


def completed_request(store, payload):
    tid = digest(["turn", payload["conversation"], payload["client_id"]])[:32]
    try:
        old = store.get(tid, "turn")
    except KeyError:
        return None
    fingerprint = digest([payload["conversation"], payload["course"], payload["question"],
                          payload["purpose"], payload["attachments"], payload["review_id"]])
    if old["request"] != fingerprint:
        raise ValueError("同一条消息的内容已改变，请重新发送")
    return public_turn(old, store) if old["status"] == "ready" else None


def public_turn(turn, store=None):
    result = dict(turn.get("result", {}))
    if store and result.get("exercise"):
        practice = store.get(result["exercise"]["id"], "practice")
        if practice.get("attempts"):
            result["exercise"] = {**result["exercise"], "expected_answer": practice["expected_answer"],
                                  "last_attempt": practice["attempts"][-1]}
    return {k: v for k, v in turn.items() if k != "result"} | {"result": {
        k: v for k, v in result.items() if k not in {"evidence"}}}


def turns(store, conversation):
    return sorted([t for t in store.list("turn") if t["conversation"] == conversation], key=lambda t: t["created_at"])


def home(store, conversation="main", now=None):
    now = time.time() if now is None else now
    history = turns(store, conversation)
    courses = sorted({s["course"] for s in store.sources()} | {t["course"] for t in store.list("task")})
    reviews = review_candidates(store, now)
    tasks = [t for t in store.list("task") if t.get("status") == "accepted" and not t.get("completed")]
    return {"turns": [public_turn(t, store) for t in history[-50:]], "courses": courses,
        "course": next((t["course"] for t in reversed(history) if t.get("course")), ""),
        "reviews": reviews, "due": [r for r in reviews if r["today"]], "review_preferences": preferences(store),
        "paused_reviews": [r for r in store.list("review") if r.get("status") == "paused"],
        "operations": store.list("operation")[:30],
        "alerts": [a for a in store.list("study_alert") if not a.get("handled")],
        "task_count": len(tasks), "source_count": len(store.sources()),
        "pending_count": sum(p.get("status") == "pending" for p in store.list("proposal"))}


@serialized_request
@traced("chat")
def converse(store, provider, root, *, conversation, client_id, course, question, purpose="auto", attachments=None, review_id=None, operations=None):
    attachments = attachments or []
    request_input = dict(conversation=conversation, client_id=client_id, course=course, question=question,
                         purpose=purpose, attachments=attachments, review_id=review_id)
    request = digest([conversation, course, question, purpose, attachments, review_id])
    tid = digest(["turn", conversation, client_id])[:32]
    try:
        old = store.get(tid, "turn")
        if old["request"] != request:
            raise ValueError("同一条消息的内容已改变，请重新发送")
        if old["status"] == "ready":
            emit("cache.hit", "消息已处理，返回原有回答", {"cache":"request_id", "original_trace":old.get("trace_id")})
            return public_turn(old)
    except KeyError:
        old = None
    history = turns(store, conversation)
    available = sorted({s["course"] for s in store.sources()} | {t["course"] for kind in ("task", "review", "practice") for t in store.list(kind)}, key=len, reverse=True)
    mentioned = [name for name in available if name in question]
    mentioned = [name for name in mentioned if not any(name != other and name in other for other in mentioned)]
    daily_request = purpose == "today" or purpose == "auto" and is_daily_question(question)
    plain_plan = choose_operation(None, question, course) if purpose == "auto" else None
    batch_request = plain_plan and plain_plan["tool"] == "process_missing_lessons"
    ambiguous = bool(batch_request and len(mentioned) > 1)
    if batch_request and not mentioned:
        # An explicitly named new course need not already have imported sources.
        match = re.search(r'(?:处理|转译|转写|整理|下载|把|将)\s*(?:一下)?\s*[《“\"]?(.{1,60}?)\s*[》”\"]?(?:这门课程|这门课|课程的|尚未|还没|未处理|未整理|所有|全部|每一?节)', question)
        if match:
            named = match[1].strip(' 的《》“”"')
            if named and named not in {"本课", "本门课", "这门", "本门", "当前", "当前课", "当前课程", "这门课", "本", "这", "的"}:
                course = named
    if ambiguous:
        course = ""
    if len(mentioned) == 1:
        course = mentioned[0]
    if not course and history and not ambiguous:
        course = next((t["course"] for t in reversed(history) if t.get("course")), "")
    selected_attachments = [store.get(aid, "attachment") for aid in attachments]
    turn = {"conversation": conversation, "request": request, "request_input": request_input, "course": course, "question": question,
        "purpose": purpose, "review_id": review_id, "status": "running", "trace_id":current().id if current() else None, "created_at": old["created_at"] if old else time.time(),
        "attachments": [{k: a[k] for k in ("id", "name", "image")} for a in selected_attachments]}
    store.save("turn", turn, tid)
    if daily_request and not attachments:
        try:
            # A general evening budget covers all courses, regardless of the last selected course.
            scope = mentioned[0] if len(mentioned) == 1 else ""
            result = daily_reply(store, question, scope)
            return public_turn(store.save("turn", {**turn, "course": scope, "purpose": "today", "status": "ready", "result": result}, tid))
        except Exception as exc:
            store.save("turn", {**turn, "status": "failed", "error": str(exc)[:240]}, tid)
            raise
    if not course:
        result = {"reply": "这条消息提到了多门课，请先选一门要处理的课程。" if ambiguous else "先告诉我这是哪门课就好。你发来的问题和附件已经留在这里，选好后我接着看。",
                  "choose_course": available, "claims": [], "proposals": [], "uncertainties": [], "status": "needs_course"}
        return public_turn(store.save("turn", {**turn, "status": "ready", "result": result}, tid))
    try:
        if operations and purpose == "auto":
            plan = choose_operation(provider, question, course)
            if plan:
                emit("tool.selected", "选择课程操作", {"tool":plan["tool"], "arguments":plan.get("arguments",{})})
                job = operations.submit(plan, course=course, question=question, turn_id=tid)
                result = {"reply": job["message"], "operation_id": job["id"], "claims": [], "proposals": [], "uncertainties": [], "status": "operation"}
                return public_turn(store.save("turn", {**turn, "status": "ready", "result": result}, tid))
        focus, images = prepare_attachments(store, provider, root, attachments, course, question)
        if any(text in question for text in ("老师说", "老师要求", "记住", "作业要求", "考勤要求")):
            source = store.source(origin="student-message:" + tid, course=course, title="你补充的课堂信息", kind="student_report",
                                  fingerprint=digest(question), chunks=[{"text": question, "locator": {"message": tid}}])
            focus.insert(0, store.evidence(source["id"] + ":0"))
        related = [t for t in history if t.get("course") == course and t.get("status") == "ready"][-6:]
        previous = [{"question": t["question"], "answer": "\n".join(c["text"] for c in t.get("result", {}).get("claims", []))[:1600],
                     "next_question": t.get("result", {}).get("next_question", "")} for t in related]
        # Preserve references across short follow-ups such as “为什么” or “换个例子”.
        for t in related[-2:]:
            for claim in t.get("result", {}).get("claims", [])[:3]:
                for c in claim["citations"][:1]:
                    item = store.evidence(c["evidence_id"])
                    if item["course"] == course and item["active"]:
                        focus.append(item)
        if review_id:
            review = store.get(review_id, "review")
            if review["course"] != course:
                raise ValueError("这条复习属于另一门课，请先切换课程")
            for c in review.get("citations", [])[:4]:
                item = store.evidence(c["evidence_id"])
                if item["active"]:
                    focus.insert(0, item)
        if purpose == "auto":
            purpose = "classroom" if is_classroom_question(question) else "practice" if any(s in question for s in ("出道题", "出一道", "考考我", "检验一下")) else (
                "connect" if any(s in question for s in ("串联", "联系", "关联", "前置知识")) else (
                "summary" if any(s in question for s in ("深入总结", "完整总结", "梳理整章")) else "ask"))
        result = run_agent(store, provider, course=course, question=question, purpose=purpose,
                           history=previous, focus=focus, max_steps=2 if images else 3)
        result["input_images"] = images
        result.pop("evidence", None)
        return public_turn(store.save("turn", {**turn, "purpose": purpose, "status": "ready", "result": result}, tid))
    except Exception as exc:
        store.save("turn", {**turn, "status": "failed", "error": str(exc)[:240]}, tid)
        raise
