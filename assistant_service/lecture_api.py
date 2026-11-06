"""Attach student-selected slides and serve verified originals for comparison."""
from __future__ import annotations

import hashlib
import io
import math
import threading
import json
import time
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .lecture_materials import available_materials, selected_materials
from .study_agent.attachments import attachment_path
from .study_agent.ingest import safe_file
from .study_agent.knowledge import source_policy
from .study_agent.materials import material_lock, register_material, mark_as_slides
from .study_agent.storage import StudyStore


PDF_RENDER_LOCK = threading.Lock()


class LectureMaterials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ids: list[str] = Field(default_factory=list, max_length=300)
    attachment_ids: list[str] = Field(default_factory=list, max_length=300)


def router(settings, classroom):
    study = StudyStore(settings.root)
    api = APIRouter(prefix="/api/sessions")

    @api.get("/{sid}/materials")
    def materials(sid: str):
        session = classroom.get(sid)
        return {"sources": [{k: s[k] for k in ("id", "title")} for s in
                            available_materials(study, session.get("course_title", ""))],
                "selected_ids": [s["id"] for s in selected_materials(study, session)],
                "explicit": session.get("note_source_ids") is not None}

    @api.post("/{sid}/materials")
    def attach(sid: str, body: LectureMaterials):
        with classroom.lock, material_lock:
            session = classroom.get(sid)
            if session.get("deleted_at") or session["status"] == "cancelled":
                raise ValueError("请先恢复这节课，再选择课件")
            if classroom.pending(sid, "analysis"):
                raise HTTPException(409, "正在生成笔记，请完成后再更换课件")
            course = session.get("course_title", "")
            if not course:
                raise ValueError("这节课还没有课程名称，无法关联课件")
            allowed = {s["id"] for s in available_materials(study, course)}
            ids = list(dict.fromkeys(body.source_ids))
            if any(sid not in allowed for sid in ids) or len(ids) + len(set(body.attachment_ids)) > 300:
                raise ValueError("请选择本门课程中可用的课件，每次最多 300 份")
            # Validate every attachment before changing the course catalogue.
            attachments = [study.get(aid, "attachment") for aid in dict.fromkeys(body.attachment_ids)]
            if any(item["image"] or Path(item["name"]).suffix.lower() not in {".pdf", ".pptx"} for item in attachments):
                raise ValueError("课堂对照请选择 PDF 或 PPTX 课件")
            for item in attachments:
                value = register_material(study, course=course, title=item["name"], fingerprint=item["fingerprint"],
                                          chunks=item["chunks"], attachment_id=item["id"])
                mark_as_slides(study, value["id"])
                ids.append(value["id"])
            ids = list(dict.fromkeys(ids))
            classroom.update(sid, note_source_ids=ids, analysis_reset_at=time.time(),
                             notes_auto_attempts=0, notes_retry_at=0)
            return materials(sid)

    def original_bytes(sid: str, source_id: str):
        session = classroom.get(sid)
        with study.connect() as db:
            row = db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "课件不存在")
        source = json.loads(row[0])
        if (source["course"] != session.get("course_title") or source["kind"] != "material"
                or source_policy(study, source).get("excluded")):
            raise HTTPException(404, "本课无法读取这份课件")
        if Path(source["title"]).suffix.lower() != ".pdf":
            raise ValueError("PPTX 请对照摘录；原页预览目前支持 PDF")
        aid = source.get("meta", {}).get("attachment_id")
        if aid:
            item = study.get(aid, "attachment")
            path, fingerprint = attachment_path(settings.root, item), item["fingerprint"]
        else:
            path = Path(source["origin"])
            path = safe_file(path.parent, path)
            fingerprint = source["fingerprint"]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != fingerprint:
            raise HTTPException(409, "原文件已更改，请重新导入；笔记中的旧摘录仍保留")
        return data

    @api.get("/{sid}/materials/{source_id}/file")
    def original(sid: str, source_id: str):
        return Response(original_bytes(sid, source_id), media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @api.get("/{sid}/materials/{source_id}/pages/{page}")
    def page_image(sid: str, source_id: str, page: int):
        data = original_bytes(sid, source_id)
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise ValueError("PDF 原页预览组件未安装，请安装 study 可选依赖") from exc
        # PDFium is not thread-safe; bound image dimensions even for oversized pages.
        with PDF_RENDER_LOCK, closing(pdfium.PdfDocument(data)) as document:
            if not 1 <= page <= len(document):
                raise HTTPException(404, "课件没有这一页")
            with closing(document[page - 1]) as sheet:
                width, height = sheet.get_size()
                if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                    raise ValueError("这一页的尺寸无法读取")
                with closing(sheet.render(scale=min(1.5, 1800 / max(width, height)))) as bitmap:
                    output = io.BytesIO()
                    bitmap.to_pil().save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

    return api
