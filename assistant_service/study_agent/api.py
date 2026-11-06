from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, FileResponse
from pydantic import Field, field_validator, model_validator

from . import ingest
from .planner import calendar, make_plan, review_interval, stamp
from .reasoner import Provider, Strict, run_agent
from .storage import StudyStore, digest
from .attachments import accept_attachment, attachment_path
from .dialogue import converse, home, completed_request
from .reviews import create_review, update_review
from .operations import OperationQueue, choose_operation
from .knowledge import catalog, set_policy, remember_claim
from .learning import answer_feedback
from .daily import daily_plan, is_daily_question
from .classroom_search import find_classroom_mentions, location
from .timetable import expand_timetable, extract_timetable


class Library(Strict):
    root: str = Field(min_length=3, max_length=1000)
    course: str = Field(min_length=1, max_length=160)


class ImportFiles(Strict):
    library_id: str = Field(max_length=80)
    files: list[str] = Field(min_length=1, max_length=30)


class Ask(Strict):
    course: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=2, max_length=1200)
    purpose: Literal["ask", "verify", "requirements", "practice"] = "ask"


class Chat(Strict):
    conversation: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]{1,80}$")
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    course: str = Field(default="", max_length=160)
    question: str = Field(min_length=1, max_length=3000)
    purpose: Literal["auto", "ask", "verify", "requirements", "practice", "connect", "summary", "classroom", "today"] = "auto"
    attachments: list[str] = Field(default_factory=list, max_length=4)
    review_id: str | None = Field(default=None, max_length=80)


class ReviewCreate(Strict):
    turn_id: str = Field(max_length=80)
    days: int = Field(default=1, ge=1, le=30)


class Today(Strict):
    minutes: int = Field(default=30, ge=1, le=240, strict=True)
    course: str = Field(default="", max_length=160)


class ClassroomQuery(Strict):
    course: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=500)


class ReviewUpdate(Strict):
    action: Literal["complete", "snooze", "pause", "resume"]
    rating: int | None = Field(default=None, ge=1, le=4)


class Decide(Strict):
    status: Literal["accepted", "rejected"]
    deadline: date | None = None
    estimate_min: int | None = Field(default=None, ge=5, le=6000)
    estimate_max: int | None = Field(default=None, ge=5, le=12000)
    difficulty: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def bounds(self):
        if self.estimate_min is not None and self.estimate_max is not None and self.estimate_max < self.estimate_min:
            raise ValueError("耗时上限不能低于下限")
        return self


class Task(Strict):
    course: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(default="", max_length=2000)
    deadline: date | None = None
    estimate_min: int = Field(default=30, ge=5, le=6000)
    estimate_max: int = Field(default=60, ge=5, le=12000)
    difficulty: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def bounds(self):
        if self.estimate_max < self.estimate_min:
            raise ValueError("耗时上限不能低于下限")
        return self


class Progress(Strict):
    spent_minutes: int = Field(ge=0, le=12000)
    completed: bool = False


class Requirement(Strict):
    course: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=2, max_length=2000)


class Busy(Strict):
    start: str = Field(max_length=40)
    end: str = Field(max_length=40)

    @model_validator(mode="after")
    def times(self):
        if not all("T" in v and ("+" in v[10:] or v.endswith("Z")) for v in (self.start, self.end)):
            raise ValueError("时间必须包含时区")
        if stamp(self.end) <= stamp(self.start):
            raise ValueError("结束时间必须晚于开始时间")
        return self


class Plan(Strict):
    start: date
    days: int = Field(default=7, ge=1, le=30)
    daily_minutes: int = Field(default=90, ge=15, le=240)
    start_hour: int = Field(default=19, ge=6, le=20)
    busy: list[Busy] = Field(default_factory=list, max_length=500)


class Attempt(Strict):
    answer: str = Field(min_length=1, max_length=3000)
    rating: int = Field(ge=1, le=4)
    review_id: str | None = Field(default=None, max_length=80)


class SourcePolicy(Strict):
    role: Literal["reference", "exercise", "slides", "transcript", "external", "other"]
    trusted: bool = False
    excluded: bool = False


class Remember(Strict):
    turn_id: str = Field(max_length=80)
    claim_index: int = Field(ge=0, le=9)
    status: Literal["pending", "confirmed", "dismissed"] = "pending"


class BrowserLesson(Strict):
    sub_id: str = Field(pattern=r"^\d{1,30}$")
    title: str = Field(max_length=200)
    status: str = Field(max_length=10)
    start_at: float = Field(default=0, ge=0)


class BrowserCatalogue(Strict):
    course_id: str = Field(pattern=r"^\d{1,30}$")
    course_title: str = Field(max_length=160)
    page_url: str = Field(max_length=2000)
    items: list[BrowserLesson] = Field(max_length=1000)
    client: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class BrowserResult(Strict):
    sub_id: str = Field(pattern=r"^\d{1,30}$")
    status: Literal["queued", "failed"]
    sid: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    error: str = Field(default="", max_length=300)
    client: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class BrowserLease(Strict):
    client: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class Visual(Strict):
    url: str = Field(max_length=1600)
    seconds: int = Field(ge=0, le=172800)


class Visuals(Strict):
    course: str = Field(min_length=1, max_length=160)
    course_url: str = Field(max_length=2000)
    title: str = Field(max_length=200)
    items: list[Visual] = Field(min_length=1, max_length=600)


class TimetableFile(Strict):
    library_id: str = Field(max_length=80)
    file: str = Field(max_length=1000)


class Period(Strict):
    number: int = Field(ge=1, le=16)
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ClassSlot(Strict):
    course: str = Field(min_length=1, max_length=160)
    weekday: int = Field(ge=0, le=6)
    first_period: int = Field(ge=1, le=16)
    last_period: int = Field(ge=1, le=16)
    weeks: list[int] = Field(min_length=1, max_length=30)
    weeks_text: str = Field(default="", max_length=200)
    source_page: int = Field(default=0, ge=0, le=10)

    @field_validator("weeks")
    @classmethod
    def weeks_valid(cls, value):
        if any(w < 1 or w > 30 for w in value):
            raise ValueError("周次须在 1–30 之间")
        return sorted(set(value))


class TimetableConfirm(Strict):
    semester_monday: date
    periods: list[Period] = Field(min_length=1, max_length=16)
    candidates: list[ClassSlot] = Field(min_length=1, max_length=100)


def router(settings, classroom_store):
    study, provider = StudyStore(settings.root), Provider(settings)
    api = APIRouter(prefix="/api/study")
    model_gate = threading.BoundedSemaphore(1)
    operations = OperationQueue(study, provider, settings, classroom_store, model_gate)
    api.operations = operations
    api.provider = provider
    from ..telemetry import RuntimeLog
    runtime = RuntimeLog(settings.root)
    api.runtime = runtime

    @api.get("/runtime")
    def runtime_list(limit: int = 40):
        return {"items":runtime.list(max(1,min(limit,100))), "retention_days":30, "max_runs":300}

    @api.get("/runtime/catalog")
    def runtime_catalog():
        from .prompt_catalog import catalog as prompts, PURPOSES
        from .operations import tool_schemas
        return {"prompts":prompts(), "purpose_rules":PURPOSES, "tools":tool_schemas(),
            "retrieval_tools":[{"name":"search_sources", "arguments":{"query":"本课关键词"}, "scope":"只检索当前课程，排除停用资料"},
                {"name":"find_classroom_mentions", "arguments":{"course":"课程名称", "query":"主题术语"}, "scope":"跨课时查原句、核验时间，字幕不自动成为确认知识"},
                {"name":"plan_today_review", "arguments":{"minutes":30, "course":"可选，留空为全部课程"}, "scope":"只读任务与作答记录；总用时不超过预算，不调用模型"},
                {"name":"search_public_references", "arguments":{"concept":"公开概念词", "discipline":"general / life_sciences"}, "scope":"Crossref / Europe PMC 与教材入口；仅发关键词，24小时缓存，候选不是确认知识"},
                {"name":"read_public_reference", "arguments":{"course":"课程", "reference_id":"已检索候选ID"}, "scope":"有界读取正文或摘要，注明覆盖范围，存为未核验参考"},
                {"name":"prepare_evidence_comparison", "arguments":{"course":"课程", "question":"具体主张", "evidence_ids":[]}, "scope":"2–6段当前原文；检查同源、摘要边界，保留分歧，不自动写确认记忆"},
                {"name":"read_surrounding_context", "arguments":{"evidence_id":"已读片段 ID"}, "scope":"读取同一资料的前后段落"}],
            "budgets":{"source_characters":24000,"history_characters":2000,"max_model_steps":3,
                       "support_cache_days":7,"support_cache_entries":256},
            "storage":{"trace":".local/agent-runtime.sqlite3","memory":".local/study-agent.sqlite3"}}

    @api.get("/runtime/{rid}")
    def runtime_detail(rid: str, after: int = 0):
        return runtime.detail(rid, max(0, after))


    @api.get("/sources")
    def sources():
        return {"items": catalog(study), "knowledge": study.list("knowledge")}

    @api.post("/sources/{sid}")
    def source_settings(sid: str, body: SourcePolicy):
        return set_policy(study, sid, **body.model_dump())

    @api.post("/knowledge")
    def knowledge(body: Remember):
        turn = study.get(body.turn_id, "turn")
        claims = turn.get("result", {}).get("claims", [])
        if body.claim_index >= len(claims):
            raise ValueError("该回答没有这条结论")
        claim = claims[body.claim_index]
        if body.status == "confirmed" and turn.get("result", {}).get("support_status") != "checked":
            raise ValueError("这条回答尚未完成出处检查，请重新核对后再保存为确认知识")
        return remember_claim(study, course=turn["course"], text=claim["text"], citations=claim["citations"], status=body.status)

    @api.post("/knowledge/{mid}/dismiss")
    def dismiss_memory(mid: str):
        item = study.get(mid, "knowledge")
        return study.save("knowledge", {**item, "status": "dismissed"}, mid)

    @api.get("/operations")
    def operation_list():
        operations.refresh_browser_jobs()
        return study.list("operation")[:50]

    @api.post("/operations/{jid}/cancel")
    def cancel_operation(jid: str):
        return operations.cancel(jid)

    @api.post("/operations/{jid}/retry")
    def retry_operation(jid: str):
        job = study.get(jid, "operation")
        if job["status"] not in {"failed", "completed"}:
            raise ValueError("任务仍在执行，或已取消")
        items = []
        for item in job["items"]:
            if item["status"] == "failed":
                item = {**item, "status": "pending"}
                if item.get("sid"):
                    session = classroom_store.get(item["sid"])
                    item["action"] = "analyze" if session.get("segments") and session.get("status") != "failed" else "retry"
            items.append(item)
        value = operations.update(job, items=items, status="waiting_browser" if job["tool"] == "process_missing_lessons" else "queued", message="准备重试未完成部分")
        operations.wake.set()
        return value

    @api.get("/exports/{jid}/{fmt}")
    def export_pack(jid: str, fmt: str):
        job = study.get(jid, "operation")
        entry = next((f for f in job.get("files", []) if f["format"] == fmt), None)
        if not entry or entry["file"] != jid + "." + fmt or fmt not in {"pdf", "md"}:
            raise HTTPException(404, "此导出尚未完成")
        path = settings.root / "study-exports" / entry["file"]
        return FileResponse(path, filename=entry["name"], media_type="application/pdf" if fmt == "pdf" else "text/markdown; charset=utf-8")

    @api.get("/browser/work")
    def browser_work():
        return [{k: j[k] for k in ("id", "course", "status", "items")} for j in study.list("operation")
            if j["tool"] == "process_missing_lessons" and j["status"] in {"waiting_browser", "running"}]

    def require_lease(jid, client):
        job = study.get(jid, "operation")
        if job.get("browser_client") != client or job.get("lease_until", 0) < time.time():
            raise HTTPException(409, "浏览器任务租约过期，请重新领取")
        return job

    @api.post("/browser/{jid}/claim")
    def browser_claim(jid: str, body: BrowserLease):
        with operations.lock:
            job = study.get(jid, "operation")
            if job["tool"] != "process_missing_lessons" or job["status"] not in {"waiting_browser", "running"}:
                raise HTTPException(409, "任务已经结束")
            if job.get("browser_client") not in {None, body.client} and job.get("lease_until", 0) > time.time():
                raise HTTPException(409, "另一浏览器正在领取此任务")
            return operations.update(job, browser_client=body.client, lease_until=time.time()+90)

    @api.post("/browser/{jid}/catalogue")
    def browser_catalogue(jid: str, body: BrowserCatalogue):
        with operations.lock:
            require_lease(jid, body.client)
            return operations.select_lessons(jid, **body.model_dump(exclude={"client"}))

    @api.post("/browser/{jid}/result")
    def browser_result(jid: str, body: BrowserResult):
        with operations.lock:
            require_lease(jid, body.client)
            return operations.item_result(jid, **body.model_dump(exclude={"client"}))

    @api.get("/home")
    def dialogue_home(conversation: str = "main"):
        return home(study, conversation)

    @api.post("/chat")
    def chat(body: Chat):
        cached = completed_request(study, body.model_dump())
        if cached is not None:
            return cached
        if not body.attachments and (body.purpose == "today" or body.purpose == "auto" and is_daily_question(body.question)):
            return converse(study, provider, settings.root, operations=operations, **body.model_dump())
        if not model_gate.acquire(blocking=False):
            # Explicit operations only enqueue; they need not wait for a long export.
            if body.purpose == "auto" and choose_operation(None, body.question, body.course):
                return converse(study, provider, settings.root, operations=operations, **body.model_dump())
            raise HTTPException(409, "正在接着读上一条消息，稍后再发；内容会留在输入框中")
        try:
            ingest.sync_sessions(study, classroom_store.records())
            return converse(study, provider, settings.root, operations=operations, **body.model_dump())
        finally:
            model_gate.release()

    @api.post("/attachments")
    async def upload(request: Request):
        import json
        from starlette.concurrency import run_in_threadpool
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > 17 * 1024 * 1024:
                raise HTTPException(413, "附件太大，请缩小到 12 MB 内")
        value = json.loads(data)
        item = await run_in_threadpool(accept_attachment, study, settings.root, name=str(value.get("name", "")), content=value.get("content", ""))
        return {k: item[k] for k in ("id", "name", "image", "bytes")}

    @api.get("/attachments/{aid}/image")
    def attachment_image(aid: str):
        item = study.get(aid, "attachment")
        if not item["image"]:
            raise HTTPException(404)
        return FileResponse(attachment_path(settings.root, item), media_type="image/png")

    @api.post("/reviews")
    def add_review(body: ReviewCreate):
        turn = study.get(body.turn_id, "turn")
        result = turn.get("result", {})
        if turn["status"] != "ready" or not (result.get("claims") or result.get("exercise")):
            raise ValueError("先完成一次有资料依据的交流，再安排这段内容的复习")
        citations = [c for claim in result.get("claims", []) for c in claim["citations"]][:6]
        topic = turn["question"][:180]
        if result.get("exercise"):
            citations += result["exercise"]["citations"]
            topic = result["exercise"]["question"][:180]
        elif len(topic) > 100 or "刚才" in topic:
            topic = result["claims"][0]["text"][:150]
        return create_review(study, course=turn["course"], topic=topic, citations=citations[:6],
                             days=body.days, source=turn["id"])

    @api.post("/reviews/{rid}")
    def change_review(rid: str, body: ReviewUpdate):
        return update_review(study, rid, **body.model_dump())

    @api.get("/overview")
    def overview():
        sources = study.sources()
        tasks = study.list("task")
        courses = sorted({s["course"] for s in sources} | {t["course"] for t in tasks})
        practices = [{k: v for k, v in p.items() if k not in {"expected_answer", "attempts"}} for p in study.list("practice")]
        return {"courses": courses, "sources": sources, "tasks": tasks, "proposals": study.list("proposal"),
                "practice": practices, "plans": study.list("plan")[:5], "libraries": study.list("library"),
                "timetables": study.list("timetable"), "provider": provider.public(),
                "onboarding": "先添加一门课的资料或同步已有课堂记录" if not sources else "选择一门课，问一个今天最想弄明白的问题"}

    @api.post("/libraries")
    def add_library(body: Library):
        root = Path(body.root).expanduser().resolve()
        preview = ingest.inventory(root)
        library = study.save("library", {"root": str(root), "course": body.course}, digest([str(root), body.course])[:32])
        return {**library, **preview}

    @api.post("/import")
    def import_files(body: ImportFiles):
        library = study.get(body.library_id, "library")
        result = []
        for relative in body.files:
            try:
                value = ingest.import_file(study, Path(library["root"]), relative, library["course"])
                result.append({"file": relative, **value, "status": "ready"})
            except (ValueError, OSError, UnicodeError, EOFError) as exc:
                result.append({"file": relative, "status": "failed", "error": str(exc)[:200]})
        return {"items": result}

    @api.post("/sync")
    def sync():
        return ingest.sync_sessions(study, classroom_store.records())

    @api.get("/search")
    def search(course: str, q: str):
        if not course or len(q) > 1200:
            raise ValueError("请选择课程并缩短检索内容")
        return {"items": study.search(q, course)}

    @api.get("/evidence/{eid}")
    def evidence(eid: str):
        e = study.evidence(eid)
        return {**e, "location": location(e)}

    @api.post("/classroom-search")
    def classroom_search(body: ClassroomQuery):
        ingest.sync_sessions(study, classroom_store.records())
        return find_classroom_mentions(study, body.course, body.query)

    @api.post("/today")
    def today_plan(body: Today):
        return daily_plan(study, body.minutes, body.course)

    @api.post("/run")
    def run(body: Ask):
        if not model_gate.acquire(blocking=False):
            raise HTTPException(409, "正在整理上一条问题，请稍候")
        try:
            ingest.sync_sessions(study, classroom_store.records())
            return run_agent(study, provider, **body.model_dump())
        finally:
            model_gate.release()

    @api.post("/proposals/{pid}")
    def decide(pid: str, body: Decide):
        proposal = study.get(pid, "proposal")
        if proposal["proposal_kind"] == "task" and body.status == "accepted" and (body.estimate_min is None or body.estimate_max is None):
            raise ValueError("请先估计任务用时；较大的项目可以先拆成一个具体步骤")
        # Only this explicit user action turns model suggestions into tasks/memory.
        value = study.save("proposal", {**proposal, "status": body.status}, pid)
        if proposal["proposal_kind"] == "task" and body.status == "accepted":
            task_id = "task-" + pid
            try:
                existing = study.get(task_id, "task")
            except KeyError:
                existing = {}
            study.save("task", {**proposal, **body.model_dump(mode="json"), "source": "confirmed_proposal", "proposal_id": pid,
                               "completed": existing.get("completed", False), "spent_minutes": existing.get("spent_minutes", 0)}, task_id)
        return value

    @api.post("/tasks")
    def task(body: Task):
        return study.save("task", {**body.model_dump(mode="json"), "status": "accepted", "source": "student", "completed": False})

    @api.post("/requirements")
    def requirement(body: Requirement):
        value = study.save("proposal", {"course": body.course, "title": "学生补充的课程要求", "detail": body.detail,
                                        "proposal_kind": "requirement", "status": "accepted", "source": "student_report", "citations": []})
        study.source(origin="requirement:" + value["id"], course=body.course, title="学生补充的课程要求", kind="student_report",
                     fingerprint=digest(body.detail), chunks=[{"text": body.detail, "locator": {"section": 1}}])
        return value

    @api.post("/tasks/{tid}/progress")
    def progress(tid: str, body: Progress):
        return study.save("task", {**study.get(tid, "task"), **body.model_dump()}, tid)

    @api.post("/plans")
    def plan(body: Plan):
        busy = [b.model_dump() for b in body.busy]
        for timetable in study.list("timetable"):
            if timetable.get("status") == "accepted":
                busy += timetable.get("busy", [])
        value = make_plan(study.list("task"), **body.model_dump(exclude={"busy"}), busy=busy)
        return study.save("plan", value)

    @api.post("/plans/{pid}/confirm")
    def confirm_plan(pid: str):
        return study.save("plan", {**study.get(pid, "plan"), "status": "accepted"}, pid)

    @api.get("/plans/{pid}/calendar")
    def export_calendar(pid: str):
        return Response(calendar(study.get(pid, "plan")), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="study-plan.ics"'})

    @api.post("/practice/{pid}/attempt")
    def practice(pid: str, body: Attempt):
        item = study.get(pid, "practice")
        review = study.get(body.review_id, "review") if body.review_id else None
        if review and review["course"] != item["course"]:
            raise ValueError("这次作答与复习课程不匹配")
        feedback = {"status": "unavailable", "summary": "模型正忙，先保存本次思路与自评。", "missing": []}
        if model_gate.acquire(blocking=False):
            try: feedback = answer_feedback(study, provider, item, body.answer)
            finally: model_gate.release()
        days = review_interval(item["attempts"], body.rating)
        item["attempts"].append({**body.model_dump(), "at": time.time(), "feedback": feedback})
        item.update(due_at=time.time() + days * 86400, assessment="学生自评，尚非客观掌握度")
        result = study.save("practice", item, pid)
        if review:
            result["review"] = update_review(study, review["id"], action="complete", rating=body.rating, feedback=feedback)
        return result

    @api.post("/visuals")
    def visuals(body: Visuals):
        items = ingest.validate_visuals(body.course_url, [x.model_dump() for x in body.items])
        return study.save("visual", {**body.model_dump(), "items": items, "status": "references_only", "ocr_status": "not_run"}, digest([body.course_url, items])[:32])

    @api.get("/visuals")
    def list_visuals():
        return study.list("visual")

    @api.post("/timetable/preview")
    def timetable(body: TimetableFile):
        library = study.get(body.library_id, "library")
        path = ingest.safe_file(Path(library["root"]), Path(library["root"]) / body.file)
        if path.suffix.lower() != ".pdf":
            raise ValueError("请使用学校导出的 PDF 课表")
        value = extract_timetable(path)
        return study.save("timetable", {**value, "source": str(path)})

    @api.post("/timetable/{tid}/confirm")
    def confirm_timetable(tid: str, body: TimetableConfirm):
        original = study.get(tid, "timetable")
        expanded = expand_timetable([c.model_dump() for c in body.candidates], body.semester_monday,
                                    [p.model_dump() for p in body.periods])
        # Replaces the confirmed schedule; multiple imported snapshots must not accumulate busy times.
        for old in study.list("timetable"):
            if old["id"] != tid and old.get("status") == "accepted":
                study.save("timetable", {**old, "status": "superseded"}, old["id"])
        return study.save("timetable", {**original, **body.model_dump(mode="json"), **expanded, "status": "accepted"}, tid)

    return api
