from __future__ import annotations

import io
import base64
import re
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
import qrcode
import qrcode.image.svg

from config import parse_course_url
from .analysis import CATEGORIES, rule_events
from .asr import validate_wav, repetitive_segments
from .manager import Manager, append_segments
from .media import validate_media_url
from .mobile import make_pairing, pairing_code, registry_entry
from .settings import ROOT, Settings, atomic_json
from .store import Store
from . import connection_check


class Lesson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_id: str = Field(pattern=r"^\d+$", max_length=30)
    sub_id: str = Field(pattern=r"^\d+$", max_length=30)
    course_title: str = Field(default="课程", max_length=200)
    title: str = Field(default="课时", max_length=200)
    page_url: str = Field(max_length=2000)
    start_at: float = Field(default=0, ge=0, allow_inf_nan=False)
    mode: Literal["live", "replay", "subtitle"]
    analysis: bool = False
    force_new: bool = False
    request_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    time_basis: Literal["capture", "video"] = "video"
    source_url: str | None = Field(default=None, max_length=8000)


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal["resident", "xxl", "api"] | None = None
    engine_path: str | None = Field(default=None, max_length=1000)
    model_path: str | None = Field(default=None, max_length=1000)
    ffmpeg_path: str | None = Field(default=None, max_length=1000)
    device: Literal["cuda", "cpu"] | None = None
    compute_type: Literal["int8_float16", "float16", "int8", "float32", "auto"] | None = None
    language: Literal["zh", "en", "yue", "auto"] | None = None
    hotwords: str | None = Field(default=None, max_length=1500)
    deepseek_model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    deepseek_key: str | None = Field(default=None, max_length=500)
    clear_deepseek_key: bool = False
    llm_provider: Literal["deepseek", "openai"] | None = None
    openai_model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    openai_key: str | None = Field(default=None, max_length=500)
    clear_openai_key: bool = False
    asr_api_model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    analysis_window: int | None = Field(default=None, ge=15, le=120)
    asr_workers: int | None = Field(default=None, ge=1, le=8)
    analysis_workers: int | None = Field(default=None, ge=1, le=8)
    analysis_parallelism: int | None = Field(default=None, ge=1, le=4)
    retain_audio: bool | None = None
    mobile_notifications_enabled: bool | None = None
    media_hosts: list[str] | None = Field(default=None, max_length=20)

    @field_validator("media_hosts")
    @classmethod
    def hosts(cls, values):
        if values and any(not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", x) or x in {"localhost", "127.0.0.1"} for x in values):
            raise ValueError("请输入准确域名，不要填写 URL 或通配符")
        return [v.lower() for v in values] if values is not None else values


class MobileEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ImportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    course_id: str = Field(pattern=r"^\d+$", max_length=30)
    sub_id: str = Field(pattern=r"^\d+$", max_length=30)
    course_title: str = Field(default="课程", max_length=200)
    title: str = Field(default="课时", max_length=200)
    page_url: str = Field(max_length=2000)
    start_at: float = Field(default=0, ge=0, allow_inf_nan=False)
    status: Literal["pending", "failed", "queued", "cancelled"] = "pending"
    error: str = Field(default="", max_length=1000)
    sid: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ImportBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ImportRecord] = Field(min_length=1, max_length=200)


class SubtitleItem(BaseModel):
    BeginSec: float = Field(ge=0, le=172800, allow_inf_nan=False)
    EndSec: float = Field(ge=0, le=172800, allow_inf_nan=False)
    Text: str = Field(max_length=10000)


class SubtitleImport(BaseModel):
    items: list[SubtitleItem] = Field(max_length=50000)


class StopRequest(BaseModel):
    last_seq: int | None = Field(default=None, ge=-1, le=100000)
    warning: str = Field(default="", max_length=500)


class AnalysisPreference(BaseModel):
    enabled: bool


def create_app(settings=None, store=None, manager=None, *, run_workers=True, port=8765):
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Invalid loopback port")
    local_origin = rf"http://(127\.0\.0\.1|localhost):{port}"
    settings = settings or Settings()
    store = store or Store(settings.root)
    manager = manager or Manager(settings, store)
    from .lecture_api import router as lecture_router
    from .courseware import CoursewareAlignment
    courseware = getattr(manager, "courseware", None) or CoursewareAlignment(settings, store)
    connection_result = {}

    @asynccontextmanager
    async def lifespan(app):
        study_api.runtime.recover()
        if run_workers:
            manager.start()
            study_api.operations.start()
        yield
        study_api.operations.close()
        study_api.provider.close()
        manager.close()
        if courseware is not getattr(manager, "courseware", None):
            courseware.close()

    app = FastAPI(title="SCUT Local Assistant", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings, app.state.store, app.state.manager = settings, store, manager
    from .study_agent.api import router as study_router
    study_api = study_router(settings, store)
    app.state.study_operations = study_api.operations
    app.include_router(study_api)
    app.include_router(lecture_router(settings, store))
    from .courseware_api import router as courseware_router
    app.include_router(courseware_router(courseware))
    from .mindmap import router as mindmap_router
    app.include_router(mindmap_router(store))
    from .desktop import router as desktop_router
    app.include_router(desktop_router(settings, store))
    app.add_middleware(CORSMiddleware,
                       allow_origin_regex=rf"chrome-extension://[a-p]{{32}}|{local_origin}",
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}:
            return JSONResponse({"detail": "Invalid local host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not re.fullmatch(rf"chrome-extension://[a-p]{{32}}|{local_origin}", origin):
            return JSONResponse({"detail": "来源不受信任"}, status_code=403)
        if request.url.path.startswith("/api/") and request.method != "OPTIONS":
            bearer = secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + settings.data["token"])
            local_page = request.headers.get("sec-fetch-site") == "same-origin" and secrets.compare_digest(
                request.cookies.get("scut_local", ""), settings.data["token"])
            if not bearer and not local_page:
                return JSONResponse({"detail": "本地连接口令不匹配，请打开设置配对"}, status_code=401)
        length = request.headers.get("content-length", "0")
        try:
            if int(length) > 16 * 1024**2:
                return JSONResponse({"detail": "请求内容过大"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "课时任务不存在"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/health")
    def health():
        return {"app": "scut-local-assistant", "version": "0.14.0"}

    @app.get("/api/health")
    def diagnostics(brief: bool = False):
        return {"ok": True, "asr": manager.asr.diagnostics(), "deepseek_configured": settings.public()["deepseek_configured"],
                "llm_configured": settings.public()["llm_configured"],
                "mobile": manager.mobile.status(),
                **({} if brief else {"jobs": [j for j in store.jobs() if j["state"] in {"pending", "running", "failed"}]})}

    @app.get("/api/settings")
    def configuration():
        cfg = settings.public()
        try:
            cfg["deepseek_check"] = connection_result.get(connection_check.identity(settings))
        except ValueError:
            cfg["deepseek_check"] = None
        return cfg

    @app.post("/api/deepseek/check")
    def check_deepseek():
        try:
            key_id = connection_check.identity(settings)
            result = connection_check.check(settings, getattr(manager.analyzer, "transport", None))
        except ValueError:
            return {"ok": False, "message": "本机 Key 无法解密，请在设置中重新保存", "checked_at": time.time()}
        connection_result.clear()
        connection_result[key_id] = result
        return result

    @app.get("/api/pairing-token")
    def pairing_token():
        return {"token": settings.data["token"]}

    @app.get("/api/setup")
    def setup_info():
        launcher = str(ROOT / "start-assistant.ps1").replace("'", "''")
        return {"extension_path": str(ROOT / "extension"), "start_command": f"& '{launcher}'"}

    @app.get("/api/models")
    def available_models():
        current = Path(settings.data["model_path"])
        parents = {current.parent, Path(settings.data["engine_path"]).parent.parent.parent / "Model"}
        installed = {str(current): current.name} if (current / "model.bin").is_file() else {}
        for parent in parents:
            if parent.is_dir():
                for folder in parent.iterdir():
                    if folder.is_dir() and (folder / "model.bin").is_file():
                        installed[str(folder)] = folder.name
        return {"speech": [{"path": p, "name": n} for p, n in sorted(installed.items())],
                "deepseek": ["deepseek-flash", "deepseek-v4-pro"],
                "openai": ["gpt-5-mini", "gpt-5", "gpt-4o-mini", "gpt-4o"],
                "asr_api": ["gpt-transcribe", "whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"],
                "selected": settings.data["deepseek_model"], "selected_openai": settings.data["openai_model"],
                "selected_asr_api": settings.data["asr_api_model"]}

    @app.post("/api/settings")
    def update_configuration(patch: ConfigPatch):
        settings.update(patch.model_dump(exclude_none=True))
        manager.configure_workers()
        return configuration()

    def mobile_configuration(include_secret=False):
        value = manager.mobile.status()
        pairing = settings.mobile_pairing()
        if pairing and include_secret:
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
            qr.add_data(pairing_code(pairing)); qr.make(fit=True)
            image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
            svg = io.BytesIO(); image.save(svg)
            value.update(pairing_code=pairing_code(pairing), server_registration=registry_entry(pairing),
                         pairing_qr="data:image/svg+xml;base64," + base64.b64encode(svg.getvalue()).decode("ascii"),
                         app_id=pairing["app_id"], relay_url=pairing["relay_url"])
        return value

    @app.get("/api/mobile")
    def mobile_status():
        return mobile_configuration(include_secret=True)

    @app.post("/api/mobile/setup")
    def mobile_setup():
        if not settings.llm()["key"]:
            raise ValueError(f"请先配置 {settings.llm()['label']} Key，再开启手机课堂提醒")
        pairing = settings.mobile_pairing()
        if not pairing:
            settings.save_mobile_pairing(make_pairing())
        return mobile_configuration(include_secret=True)

    @app.post("/api/mobile/enabled")
    def mobile_enabled(body: MobileEnabled):
        if body.enabled and not settings.mobile_pairing():
            raise ValueError("请先生成手机配对码")
        if body.enabled and not settings.llm()["key"]:
            raise ValueError(f"手机提醒需要 {settings.llm()['label']} Key 才能判断课堂事件")
        settings.update({"mobile_notifications_enabled": body.enabled})
        return mobile_configuration()

    @app.post("/api/mobile/test")
    def mobile_test():
        if not settings.mobile_enabled():
            raise ValueError("请先配对并开启手机提醒")
        manager.mobile.test()
        return mobile_configuration()

    @app.get("/api/mobile/qr")
    def mobile_qr():
        pairing = settings.mobile_pairing()
        if not pairing:
            raise ValueError("请先生成手机配对码")
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
        qr.add_data(pairing_code(pairing)); qr.make(fit=True)
        image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        output = io.BytesIO(); image.save(output)
        return Response(output.getvalue(), media_type="image/svg+xml")

    @app.post("/api/warmup")
    def warmup():
        with manager.asr.lock:
            manager.asr.load()
        return manager.asr.diagnostics()

    @app.get("/api/sessions")
    def sessions(trash: bool = False):
        def progress(s):
            audio = [j for j in store.jobs(s["id"]) if j["kind"] in {"chunk", "repair"}]
            return {"transcription_total": len(audio),
                    "transcription_done": sum(j["state"] == "done" for j in audio),
                    "transcription_failed": sum(j["state"] == "failed" for j in audio)}
        return [{k: v for k, v in s.items() if k not in {"segments", "events", "rule_candidates", "mindmap"}} |
                {"segment_count": len(s["segments"]), "event_count": len(s["events"]), "important_events_complete": True,
                 "important_events": [e for e in s["events"] if e.get("source") == "deepseek" and e.get("category") in CATEGORIES]} | progress(s) for s in (store.list() if trash else store.records())
                if (bool(s.get("deleted_at")) and not s.get("deleted_with") if trash else not s.get("superseded_by"))]

    @app.get("/api/imports")
    def imports():
        return store.imports()

    @app.post("/api/imports")
    def save_imports(batch: ImportBatch):
        for item in batch.items:
            course = parse_course_url(item.page_url)
            if course.course_id != item.course_id or course.sub_id != item.sub_id:
                raise ValueError("课时信息与页面 URL 不匹配")
            if item.status == "queued" and not item.sid:
                raise ValueError("尚未创建本地课时，不能标记为已导入")
            if item.sid:
                session = store.get(item.sid)
                if any(session.get(key) != getattr(item, key) for key in ("course_id", "sub_id")):
                    raise ValueError("导入记录与本地课时不匹配")
        store.save_imports([item.model_dump() for item in batch.items])
        return {"ok": True}

    @app.post("/api/sessions")
    def create(lesson: Lesson):
        with store.lock:
            return create_session(lesson)

    def create_session(lesson: Lesson):
        course = parse_course_url(lesson.page_url)
        if course.course_id != lesson.course_id or course.sub_id != lesson.sub_id:
            raise ValueError("课时信息与页面 URL 不匹配")
        if lesson.request_id:
            existing = next((s for s in store.list() if s.get("request_id") == lesson.request_id), None)
            if existing and not existing.get("deleted_at"):
                return existing
        if lesson.mode != "live" and not lesson.force_new:
            existing = next((s for s in store.records() if not s.get("superseded_by")
                             and s["course_id"] == lesson.course_id and s["sub_id"] == lesson.sub_id
                             and s["mode"] == lesson.mode and s["status"] not in {"failed", "cancelled", "interrupted"}), None)
            if existing:
                return existing | {"reused": True}
        if lesson.mode == "replay":
            if not lesson.source_url:
                raise ValueError("此课时未发现回放源，请刷新课程页面或使用标签页识别")
            validate_media_url(lesson.source_url, settings.data["media_hosts"])
        if lesson.analysis and not settings.llm()["key"]:
            raise ValueError(f"请先在设置填写 {settings.llm()['label']} API Key，或取消勾选智能分析")
        value = store.create(lesson.model_dump(exclude={"source_url", "force_new"}))
        if lesson.mode == "replay":
            store.enqueue(value["id"], "media", {"url": lesson.source_url}, lane="media")
        return value

    @app.get("/api/sessions/{sid}")
    def detail(sid: str):
        record = store.get(sid)
        versions = [s for s in store.records() if s["course_id"] == record["course_id"] and s["sub_id"] == record["sub_id"]]
        current = next((s for s in versions if s["id"] == sid), record)
        return {**current, "jobs": store.jobs(sid), "versions": [{k: s.get(k) for k in
                ("id", "title", "source_kind", "source_label", "coverage", "coverage_seconds", "time_basis", "status", "created_at", "superseded_by")}
                for s in versions if not s.get("superseded_by")]}

    @app.post("/api/sessions/{sid}/delete")
    def delete_record(sid: str):
        return store.trash(sid)

    @app.post("/api/sessions/{sid}/restore")
    def restore_record(sid: str):
        return store.restore(sid)

    @app.post("/api/sessions/{sid}/analysis-preference")
    def analysis_preference(sid: str, body: AnalysisPreference):
        with store.lock:
            session = store.get(sid)
            if body.enabled and not settings.llm()["key"]:
                raise ValueError(f"请先配置 {settings.llm()['label']} API Key")
            if not body.enabled:
                store.cancel_pending_analysis(sid, realtime_only=True)
            value = store.update(sid, analysis=body.enabled)
            if body.enabled and not session["stopped"]:
                manager.maybe_analyze(sid)
            return value

    @app.post("/api/sessions/{sid}/repair")
    def repair_transcription(sid: str, only_suspect: bool = False):
        with store.lock:
            session = store.get(sid)
            if any(j["kind"] == "repair" and j["state"] in {"pending", "running"} for j in store.jobs(sid)):
                raise HTTPException(409, "这节课正在重新转写")
            directory = store.directory(sid).resolve()
            inputs = [p for p in store.audio_inputs(sid) if Path(p["path"]).resolve().is_relative_to(directory) and Path(p["path"]).is_file()]
            if not inputs:
                raise ValueError("本课音频已清理，请从学校回放重新创建转写任务")
            if only_suspect:
                groups = {}
                for segment in session["segments"]:
                    groups.setdefault(segment["chunk"], []).append(segment)
                suspect = {key for key, rows in groups.items() if repetitive_segments(rows)
                           or any(s.get("quality") == "uncertain" or (s.get("quality") == "retried"
                                  and s.get("language_probability", 1) < 0.8) for s in rows)}
                inputs = [p for p in inputs if p["id"] in suspect]
                if not inputs:
                    return session | {"repair_count": 0}
            stamp = time.time()
            atomic_json(directory / f"before-repair-{int(stamp)}.json", session)
            store.cancel_pending_analysis(sid)
            for item in inputs:
                store.enqueue(sid, "repair", {"replace_key": item["id"], "path": item["path"], "start": item["start"], "duration": item["duration"]}, priority=15)
            return store.update(sid, repair_status="queued", repair_count=len(inputs), analysis_reset_at=stamp,
                summary=None, analysis_status="idle", last_analysis_end=max((s["end"] for s in session["segments"]), default=0),
                **({"status": "processing", "error": ""} if session["stopped"] else {}))

    @app.post("/api/sessions/{sid}/chunks")
    async def chunk(sid: str, request: Request, seq: int, start: float):
        if not 0 <= seq <= 100000 or not 0 <= start <= 172800:
            raise ValueError("无效音频序号或时间")
        jid = f"{sid}-live-{seq}"
        if any(j["id"] == jid for j in store.jobs(sid)):
            return {"accepted": True, "duplicate": True, "seq": seq}
        if sum(j["state"] in {"pending", "running"} for j in store.jobs(sid)) > 200:
            raise HTTPException(429, "识别积压过多，音频暂存在浏览器，请等待")
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 4 * 1024**2:
                raise HTTPException(413, "单段音频过大")
        with store.lock:
            session = store.get(sid)
            if session["mode"] != "live" or session["stopped"] or session["status"] == "cancelled":
                raise ValueError("录制已结束，不能继续接收音频")
            folder = store.directory(sid) / "audio"
            folder.mkdir(exist_ok=True)
            path = folder / f"{seq:06d}.wav"
            path.write_bytes(body)
            try:
                duration = validate_wav(path)
            except ValueError:
                path.unlink(missing_ok=True)
                raise
            store.enqueue(sid, "chunk", {"path": str(path), "start": start, "duration": duration}, priority=0, key=jid)
            store.update(sid, last_received_at=time.time(), last_seq=seq,
                         **({"status": "recording", "warning": ""} if session["status"] == "interrupted" else {}))
        return {"accepted": True, "seq": seq}

    @app.post("/api/sessions/{sid}/subtitles")
    def import_subtitles(sid: str, data: SubtitleImport):
        session = store.get(sid)
        if session["mode"] != "subtitle" or session["stopped"]:
            raise ValueError("此任务不能重复导入字幕")
        valid = [s for s in data.items if s.Text.strip() and s.EndSec > s.BeginSec]
        if not valid:
            raise ValueError("没有有效的带时间戳字幕")
        segments = append_segments([], [{"start": s.BeginSec, "end": s.EndSec, "text": s.Text} for s in valid],
                                   sid + "-school", 0, 172800)
        store.update(sid, segments=segments, events=rule_events(segments), stopped=True, status="processing")
        manager.finalize()
        return store.get(sid)

    @app.post("/api/sessions/{sid}/stop")
    def stop(sid: str, body: StopRequest):
        session = store.get(sid)
        if body.last_seq is not None:
            received = {j["id"] for j in store.jobs(sid)}
            if any(f"{sid}-live-{n}" not in received for n in range(body.last_seq + 1)):
                raise HTTPException(409, "仍有音频未送达本地服务，请在扩展中恢复上传后再结束")
        if session["status"] not in {"complete", "cancelled"}:
            store.update(sid, stopped=True, status="stopping", warning=body.warning or session["warning"])
        manager.finalize()
        return store.get(sid)

    @app.post("/api/sessions/{sid}/cancel")
    def cancel(sid: str):
        store.cancel(sid)
        return store.get(sid)

    @app.post("/api/sessions/{sid}/retry")
    def retry(sid: str):
        session = store.get(sid)
        if session["status"] == "cancelled":
            raise ValueError("已取消的任务请重新创建")
        rebuilding = session["mode"] == "replay" and store.recover_missing_replay_audio(sid)
        if not rebuilding:
            store.retry(sid)
        store.update(sid, status=("downloading" if rebuilding else "recording" if not session["stopped"] and session["mode"] == "live" else "processing"),
                     progress="重新获取缺失音轨" if rebuilding else session.get("progress", ""), error="", warning="")
        return store.get(sid)

    @app.post("/api/sessions/{sid}/analyze")
    def reanalyze(sid: str, restart: bool = False):
        with store.lock:
            session = store.get(sid)
            if session["status"] == "cancelled":
                raise ValueError("此任务已取消，请重新导入课时")
            if any(j["lane"] in {"asr", "media"} and j["state"] in {"pending", "running"} for j in store.jobs(sid)):
                raise HTTPException(409, "请等字幕处理完成后再生成整课笔记")
            if not settings.llm()["key"]:
                raise ValueError(f"请先配置 {settings.llm()['label']} API Key")
            if not session["segments"]:
                raise ValueError("此课时尚无字幕")
            if store.pending(sid, "analysis"):
                return session  # Repeated clicks reuse the existing task.
            changes = {"analysis_status": "queued", "warning": "", "analysis": True}
            if restart:
                changes.update(analysis_reset_at=time.time(), analysis_done=0, analysis_progress="")
            store.enqueue(sid, "summary", {}, lane="analysis")
            return store.update(sid, **changes)

    @app.get("/api/sessions/{sid}/export")
    def export(sid: str):
        buffer = io.BytesIO()
        with store.lock:
            store.export(store.get(sid))
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in ("session.json", "subtitles.json", "subtitles.srt", "subtitles.txt", "events.json", "notes.md"):
                    archive.write(store.directory(sid) / name, name)
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="lesson-{sid[:8]}.zip"'})

    # Loopback HTML pairs through an HttpOnly same-origin cookie; extension pages use a bearer token.
    @app.get("/")
    def index():
        response = FileResponse(ROOT / "extension" / "study.html", media_type="text/html")
        response.set_cookie("scut_local", settings.data["token"], httponly=True, samesite="strict", path="/")
        return response

    @app.get("/{name}")
    def asset(name: str):
        desktop_assets = {"desktop-entry.js", "desktop-entry.css", "start.html", "start.js"}
        mindmap_assets = {"mindmap.js", "mindmap.css", "markmap-vendor.min.js", "markmap-vendor.css"}
        if name not in desktop_assets | mindmap_assets | {"study.html", "study.js", "study.css", "study-tools.js", "study-actions.js", "study-specialized.js", "study-conversation.js", "study-conversation.css", "study-runtime.js", "study-runtime.css", "dashboard.html", "dashboard.js", "lecture-compare.js", "lecture-compare.css", "courseware.js", "courseware.css", "library.css", "library-core.js", "assistant.css", "client.js", "options.html", "options.js", "ui.js", "welcome.html", "welcome.js", "study-popup.html", "study-popup.js", "study-popup.css", "popup-state.js", "classroom.html", "classroom.js", "classroom.css", "session-view.js", "brand.png"}:
            raise HTTPException(404)
        # Windows registry MIME associations can label .js as text/plain, which
        # browsers refuse to execute with nosniff. Keep our asset types explicit.
        media_types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png"}
        response = FileResponse(ROOT / "extension" / name, media_type=media_types[Path(name).suffix])
        if name.endswith(".html"):
            response.set_cookie("scut_local", settings.data["token"], httponly=True, samesite="strict", path="/")
        return response

    return app
