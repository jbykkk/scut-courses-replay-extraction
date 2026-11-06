"""Course-level slide imports and independent subtitle/page navigation."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .courseware import course_sources, pages_for
from .study_agent.materials import material_lock, material_choices, replacement_target, register_material, mark_as_slides
from .study_agent.storage import digest


class CourseImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course: str = Field(min_length=1, max_length=160)
    attachment_ids: list[str] = Field(min_length=1, max_length=300)
    replacements: dict[str, str] = Field(default_factory=dict, max_length=300)


class CourseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course: str = Field(min_length=1, max_length=160)
    source_ids: list[str] = Field(max_length=300)


def router(alignment):
    api = APIRouter(prefix="/api/courseware")
    study, classroom = alignment.study, alignment.classroom

    @api.get("/courses")
    def courses():
        return {"courses": sorted({s["course"] for s in study.sources() if s["kind"] == "material"} |
                                  {s.get("course_title", "") for s in classroom.records()} - {""})}

    @api.get("")
    def catalogue(course: str):
        sources = course_sources(study, course)
        sessions = [s for s in classroom.records() if s.get("course_title") == course and not s.get("superseded_by")]
        return {"course": course, "sources": [{k: s[k] for k in ("id", "title")} for s in sources],
                "sessions": [{"id": s["id"], "title": s.get("title", "课堂"),
                              "status": s.get("alignment_status", "waiting_subtitles"),
                              "error": s.get("alignment_error", "")} for s in sessions]}

    def selection(body):
        course = body.course.strip()
        if not course:
            raise ValueError("请选择或填写课程名称")
        attachments = [study.get(aid, "attachment") for aid in dict.fromkeys(body.attachment_ids)]
        # Validate the whole selection before binding it to a course.
        if any(a["image"] or Path(a["name"]).suffix.lower() not in {".pdf", ".pptx"} for a in attachments):
            raise ValueError("课程课件支持 PDF 和 PPTX；扫描版需先做文字识别")
        if set(body.replacements) - {a["id"] for a in attachments} or len(set(body.replacements.values())) != len(body.replacements):
            raise ValueError("请为所选文件分别选择要更新的课件，同一份课件不能重复替换")
        for item in attachments:
            replacement_target(study, course, item["fingerprint"], item["chunks"], body.replacements.get(item["id"]))
        return course, attachments

    @api.post("/preview")
    def preview(body: CourseImport):
        with material_lock:
            course, attachments = selection(body)
            return {"items": [material_choices(study, course, item) for item in attachments]}

    @api.post("/import")
    def import_files(body: CourseImport):
        values = []
        with classroom.lock, material_lock:
            course, attachments = selection(body)
            for item in attachments:
                source = register_material(study, course=course, title=item["name"], fingerprint=item["fingerprint"],
                    chunks=item["chunks"], attachment_id=item["id"], replace_source_id=body.replacements.get(item["id"]))
                mark_as_slides(study, source["id"])
                values.append(source)
            alignment.queue(course)
        return {**catalogue(course), "items": values}

    @api.post("/order")
    def reorder(body: CourseOrder):
        with classroom.lock:
            sources = course_sources(study, body.course)
            by_id = {s["id"]: s for s in sources}
            if len(body.source_ids) != len(by_id) or set(body.source_ids) != set(by_id):
                raise ValueError("课件列表已变化，请刷新后按完整列表调整顺序")
            study.save("courseware_order", {"course": body.course,
                "origins": [by_id[sid]["origin"] for sid in body.source_ids]}, "courseware-" + digest(body.course)[:32])
            alignment.queue(body.course)
        return catalogue(body.course)

    @api.get("/pages")
    def pages(course: str, source_id: str):
        sources = [s for s in course_sources(study, course) if s["id"] == source_id]
        if not sources:
            raise ValueError("这门课没有这份可用课件")
        return {"pages": pages_for(study, sources)}

    @api.get("/sessions/{sid}")
    def result(sid: str):
        return alignment.result(sid)

    @api.get("/mentions")
    def mentions(course: str, source_id: str, page: int):
        sources = [s for s in course_sources(study, course) if s["id"] == source_id]
        selected = next((p for p in pages_for(study, sources) if page in p["locator"].values()), None)
        if not selected:
            raise ValueError("课件没有这一页可读取的文字")
        found = []
        for session in classroom.records():
            if session.get("course_title") != course or session.get("superseded_by"):
                continue
            for row in alignment.result(session["id"])["rows"]:
                if (row.get("material") or {}).get("id") == selected["id"]:
                    found.append({"session_id": session["id"], "title": session.get("title", "课堂"),
                                  "start": row["start"], "end": row["end"], "status": row["status"],
                                  "segment_id": row["segment_ids"][0], "time_basis": session.get("time_basis", "video")})
        return {"page": selected, "mentions": found}

    @api.post("/retry")
    def retry(course: str):
        alignment.queue(course, retry=True)
        return catalogue(course)

    return api
