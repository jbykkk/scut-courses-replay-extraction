"""Typed local tools and a durable queue. No shell tool and no arbitrary writes."""
from __future__ import annotations

import re
import sqlite3
import base64
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from . import ingest
from .exports import save_pack
from .knowledge import align_class, read_aligned_visuals
from .reasoner import Strict, run_agent
from .references import read_reference, search_reference
from .research import search_public_references, read_public_reference, prepare_evidence_comparison, validate_concept
from .storage import digest
from ..telemetry import current, emit, traced, RuntimeLog
from .attachments import IMAGE_FORMATS, MAX_FILE, accept_attachment, prepare_attachments


class LocalRead(Strict):
    path: str = Field(min_length=3, max_length=1000)


class Batch(Strict):
    only_missing: Literal[True] = True


class Export(Strict):
    topic: str = Field(default="", max_length=1000)
    kind: Literal["summary", "practice"] = "summary"
    formats: list[Literal["pdf", "md"]] = Field(default_factory=lambda: ["pdf", "md"], min_length=1, max_length=2)


class Reference(Strict):
    concept: str = Field(default="", max_length=120)
    url: str = Field(default="", max_length=1600)


class Reconstruct(Strict):
    topic: str = Field(min_length=2, max_length=500)


class Research(Strict):
    concept: str = Field(min_length=2, max_length=100)
    discipline: Literal["general", "life_sciences"] = "general"


class ReviewPreference(Strict):
    daily_minutes: int = Field(ge=5, le=60)
    preferred_hour: int = Field(default=19, ge=8, le=21)


REGISTRY = {
    "read_local_path": (LocalRead, "读取学生这条消息里明确给出的文件或资料文件夹，索引内容并报告成功与失败"),
    "process_missing_lessons": (Batch, "将当前课尚未处理的回放交给浏览器批量获取；已经有字幕但缺笔记时仅补分析"),
    "export_study_pack": (Export, "生成有来源的复习资料或练习题，导出 PDF / Markdown；参考答案放在最后"),
    "lookup_reference": (Reference, "用简短公开概念检索外部百科，或读取学生明确提供的公开高校/教材链接，作为未核验参考"),
    "research_topic": (Research, "检索公开教材和论文，读取最多两份有正文/摘要的资料，与本课原文对照，标明证据不足、条件差异和冲突。只发送简短概念词，不发送课堂原话。"),
    "reconstruct_class": (Reconstruct, "查找同课字幕时间段、本地题目页与屏幕截图的关联候选，再结合原文解释"),
    "set_review_preferences": (ReviewPreference, "按学生明确给出的每天复习分钟数、时间修改复习预算")}


def tool_schemas():
    return [{"type": "function", "function": {"name": name, "description": description,
        "parameters": model.model_json_schema()}} for name, (model, description) in REGISTRY.items()]


def literal_path(text):
    match = re.search(r'["“]([A-Za-z]:[\\/][^"”\r\n]+)["”]|(?<![A-Za-z0-9])([A-Za-z]:[\\/][^\r\n，；]+)', text)
    return (match[1] or match[2]).strip().rstrip('。"”') if match else ""


def choose_operation(provider, question, course):
    if re.match(r"^\s*(?:请)?(?:不要|先别|别帮|不需要)\s*(?:帮我)?(?:处理|转译|转写|整理|下载|读|生成|导出|查|修改|调整)", question) or re.match(r"^\s*(?:假如|假设)", question):
        return None
    path = literal_path(question)
    if path:
        return {"tool": "read_local_path", "arguments": {"path": path}}
    if re.search(r"全部|所有|每[一]?节|未处理|未整理|没处理|还没", question) and re.search(r"处理|转译|转写|整理|下载", question) and re.search(r"课|回放|音频|视频", question):
        return {"tool": "process_missing_lessons", "arguments": {}}
    if re.search(r"导出|生成|做[一份套]", question) and re.search(r"pdf|markdown|\bmd\b", question, re.I):
        formats = [fmt for fmt, pattern in (("pdf", r"pdf"), ("md", r"markdown|\bmd\b")) if re.search(pattern, question, re.I)]
        return {"tool": "export_study_pack", "arguments": {"topic": question, "kind": "practice" if re.search(r"题|练习", question) else "summary", "formats": formats}}
    url = re.search(r"https://[^\s<>\"，；]+", question)
    if url and re.search(r"读|参考|核对|查证|教材", question):
        return {"tool": "lookup_reference", "arguments": {"url": url[0].rstrip("。")}}
    if hasattr(provider, "choose_tool") and re.search(r"文献|论文|外部.*(?:核对|查证|比较)|(?:核对|查证).*外部|公开教材", question):
        selected = provider.choose_tool(question, course, [t for t in tool_schemas() if t["function"]["name"] == "research_topic"])
        if selected: return selected
    if re.search(r"结合|对照|拼|还原|匹配", question) and re.search(r"题目|课堂|字幕|截图|音频", question):
        return {"tool": "reconstruct_class", "arguments": {"topic": question[:500]}}
    preference = re.fullmatch(r"\s*(?:请|帮我|以后|设为|改成|调整为|我想)?\s*每天\s*复习\s*(\d{1,2})\s*分钟(?:[，,、\s]+(晚上|下午|上午|早上)?\s*(\d{1,2})\s*(?:点|:00))?[。！!\s]*", question)
    if preference:
        hour = int(preference[3]) if preference[3] else 19
        if preference[2] in {"晚上", "下午"} and hour < 12:hour += 12
        return {"tool":"set_review_preferences", "arguments":{"daily_minutes":int(preference[1]),"preferred_hour":hour}}
    # Native tool calling is only exposed to a fresh user request, not source text.
    if hasattr(provider, "choose_tool") and re.search(r"导出|生成|批量|查证|外部|百科|每天.*复习|复习.*分钟|读取|资料文件夹", question):
        return provider.choose_tool(question, course, tool_schemas())
    return None


def validate_operation(plan, question):
    name = plan.get("tool")
    if name not in REGISTRY:
        raise ValueError("此工具不在允许范围")
    value = REGISTRY[name][0].model_validate(plan.get("arguments", {})).model_dump()
    if name == "read_local_path" and value["path"] not in question:
        raise ValueError("路径必须由你在本条消息中给出")
    if name == "lookup_reference" and value["url"] and value["url"] not in question:
        raise ValueError("外部链接必须由你提供，不能由模型猜测")
    if name == "research_topic":
        if not re.search(r"文献|论文|外部|公开教材|查证", question): raise ValueError("外部检索需要你明确提出查证或文献请求")
        value["concept"] = validate_concept(value["concept"])
    if name == "process_missing_lessons" and not (re.search(r"全部|所有|每[一]?节|未处理|未整理|没处理|还没", question) and re.search(r"处理|转译|转写|整理|下载", question)):
        raise ValueError("批量处理需要你明确指定尚未处理的课时")
    if name == "set_review_preferences" and str(value["daily_minutes"]) not in question:
        raise ValueError("请明确每天想复习多少分钟，例如：每天复习 15 分钟")
    return name, value


def explicit_local_path(value):
    if value.startswith(("\\\\", "//")):
        raise ValueError("请使用本机资料路径，不读取网络共享或设备路径")
    path = Path(value).expanduser().resolve()
    if any(p.lower() in ingest.SKIP | {".ssh", "appdata", "windows", "programdata"} for p in path.parts):
        raise ValueError("此路径属于程序、凭据或系统目录，请指定课程资料文件夹")
    if not path.exists():
        raise ValueError("这个本地路径不存在，请检查地址")
    return path


class OperationQueue:
    def __init__(self, store, provider, settings, classroom_store, gate):
        self.store, self.provider, self.settings, self.classroom = store, provider, settings, classroom_store
        self.gate = gate; self.stop = threading.Event(); self.wake = threading.Event(); self.thread = None
        self.lock = threading.RLock()

    def submit(self, plan, *, course, question, turn_id):
        name, args = validate_operation(plan, question)
        jid = digest(["operation", turn_id, name, args])[:32]
        with self.lock:
            try:
                return self.store.get(jid, "operation")
            except KeyError:
                pass
            if name == "process_missing_lessons":
                existing = next((j for j in self.store.list("operation") if j["course"] == course and j["tool"] == name and j["status"] not in {"completed", "failed", "cancelled"}), None)
                if existing: return existing
            if name == "read_local_path": explicit_local_path(args["path"])
            job = self.store.save("operation", {"tool": name, "arguments": args, "course": course, "question": question,
                "turn_id": turn_id, "parent_trace":current().id if current() else None, "status": "waiting_browser" if name == "process_missing_lessons" else "queued",
                "message": "等待同一门课的学校播放页和扩展响应" if name == "process_missing_lessons" else "已排队",
                "items": [], "created_at": time.time(), "trace": [{"tool": name, "stage": "validated", "at": time.time()}]}, jid)
            if name=="process_missing_lessons":
                job=self.update(job, message=job["message"], trace_id=None)
        self.wake.set()
        return job

    def update(self, job, **changes):
        with self.lock:
            current = self.store.get(job["id"], "operation")
            if current["status"] == "cancelled": return current
            if all(current.get(k)==v for k,v in changes.items()) and not (job["tool"]=="process_missing_lessons" and not current.get("trace_id")):
                return current
            data={k:v for k,v in changes.items() if k in {"status","message","files","trace_id"}}
            if job["tool"]=="process_missing_lessons":
                try:
                    log=RuntimeLog(self.settings.root);rid=current.get("trace_id")
                    if not rid or log.state(rid)!="running":
                        rid=log.begin("operation",job["question"],job["course"],job.get("parent_trace"))
                        log.emit(rid,"tool.start","等待浏览器接收任务",{"tool":job["tool"],"arguments":job["arguments"]})
                        changes["trace_id"]=rid
                    if "items" in changes:
                        old={i.get("sub_id"):i for i in current.get("items",[])}
                        changed=[i for i in changes["items"] if i!=old.get(i.get("sub_id"))]
                        data["changed_items"]=[{k:i[k] for k in ("sub_id","title","status","action","error") if k in i} for i in changed]
                    log.emit(rid,"tool.progress",changes.get("message","课时状态更新"),data)
                    end=changes.get("status")
                    if end in {"completed","failed","cancelled"}:
                        log.emit(rid,"tool.finish","批次结束",{"status":end})
                        log.finish(rid,"failed" if end=="completed" and any(i.get("status")=="failed" for i in changes.get("items",[])) else end)
                except (sqlite3.Error,OSError):
                    changes.pop("trace_id",None)
            else:
                emit("tool.progress", changes.get("message", "更新进度"), data)
            return self.store.save("operation", {**current, **changes}, job["id"])

    def cancel(self, jid):
        with self.lock:
            job = self.store.get(jid, "operation")
            return self.update(job, status="cancelled", message="已取消后续步骤；已提交的字幕任务保留，可在课堂记录中管理")

    def start(self):
        for job in self.store.list("operation"):
            if job["status"] == "running" and job["tool"] != "process_missing_lessons":
                self.update(job, status="queued", message="服务已恢复，继续未完成步骤")
        self.thread = threading.Thread(target=self.loop, daemon=True, name="study-operations"); self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread: self.thread.join(timeout=2)

    def loop(self):
        while not self.stop.is_set():
            jobs = [j for j in reversed(self.store.list("operation")) if j["status"] == "queued"]
            for job in jobs:
                if self.stop.is_set(): break
                self.execute(job)
            self.refresh_browser_jobs()
            self.wake.wait(3); self.wake.clear()

    @traced("operation")
    def execute(self, job):
        emit("tool.start", "执行课程操作", {"tool":job["tool"],"arguments":job["arguments"]})
        job = self.update(job, status="running", message="处理中", trace_id=current().id if current() else None)
        if job["status"] == "cancelled": return job
        try:
            args, course = job["arguments"], job["course"]
            if job["tool"] == "read_local_path":
                path = explicit_local_path(args["path"])
                if path.is_file() and path.suffix.lower() in IMAGE_FORMATS:
                    if path.stat().st_size > MAX_FILE:
                        raise ValueError("图片请控制在 12 MB 内")
                    item = accept_attachment(self.store, self.settings.root, name=path.name,
                        content=base64.b64encode(path.read_bytes()).decode())
                    with self.gate:
                        if self.store.get(job["id"], "operation")["status"] == "cancelled": return
                        evidence, _ = prepare_attachments(self.store, self.provider, self.settings.root,
                            [item["id"]], course, job["question"])
                    return self.update(job, status="completed", items=[{"file": path.name, "status": "ready"}],
                        message="图片已读取，识读结果保留疑问；可以继续问其中的题目或对照课堂。")
                root = path if path.is_dir() else path.parent
                preview = ingest.inventory(path) if path.is_dir() else {"files": [{"path": path.name}], "limited": False}
                self.store.save("library", {"root": str(root), "course": course}, digest([str(root), course])[:32])
                items = list(job.get("items", [])); done = {i["file"] for i in items if i["status"] == "ready"}
                for f in preview["files"]:
                    if self.stop.is_set(): return self.update(job, status="queued", items=items)
                    if self.store.get(job["id"], "operation")["status"] == "cancelled": return
                    if f["path"] in done: continue
                    try:
                        value = ingest.import_file(self.store, root, f["path"], course)
                        items = [i for i in items if i["file"] != f["path"]]
                        items.append({"file": f["path"], "status": "ready", **value})
                    except (ValueError, OSError, UnicodeError) as exc:
                        items = [i for i in items if i["file"] != f["path"]]
                        items.append({"file": f["path"], "status": "failed", "error": str(exc)[:200]})
                    self.update(job, items=items, message=f"已检查 {len(items)} / {len(preview['files'])} 份资料")
                failures = sum(i["status"] == "failed" for i in items)
                return self.update(job, status="completed", items=items, message=f"读取 {len(items)-failures} 份，失败 {failures} 份" + ("；达到本次 300 份上限" if preview.get("limited") else ""))
            if job["tool"] == "lookup_reference":
                result = [read_reference(self.store, course, args["url"])] if args["url"] else search_reference(self.store, course, args["concept"])
                return self.update(job, status="completed", references=result, message=f"找到 {len(result)} 份外部参考，尚未独立核验；可继续问与课堂说法的差异")
            if job["tool"] == "research_topic":
                found = search_public_references(self.store, **args)
                refs, warnings = [], list(found["warnings"])
                candidates = [i for i in found["items"] if i["coverage"] != "metadata_only" and i["publication_status"] not in {"update_notice", "retraction_notice"}][:2]
                for item in candidates:
                    if self.stop.is_set(): return self.update(job, status="queued")
                    if self.store.get(job["id"], "operation")["status"] == "cancelled": return
                    try:
                        refs.append(read_public_reference(self.store, course, item["id"]))
                    except (ValueError, OSError, KeyError) as exc:
                        warnings.append(str(exc)[:180])
                    self.update(job, message=f"已读取 {len(refs)} 份外部资料，正在核对适用条件")
                if not refs:
                    return self.update(job, status="completed", references=found["items"], warnings=warnings,
                        message="只找到候选或暂时无法读取正文；没有据此生成确定结论。可提供原论文或教材文件。")
                local = [e for e in self.store.search(job["question"], course, 24) if e["kind"] != "external"][:3]
                if not local:
                    warnings.append("没有检索到对应的课堂或本地资料原文；以下只能解释外部资料，不能判断老师是否讲错。")
                external = []
                for ref in refs:
                    allowed_ids = set(ref.get("evidence_ids", []))
                    ranked = [e for e in self.store.search(args["concept"], course, 80) if e["id"] in allowed_ids][:2]
                    external.extend(ranked or [self.store.evidence(eid) for eid in ref.get("evidence_ids", [])[:1]])
                focus = (local + external)[:6]
                comparison = prepare_evidence_comparison(self.store, course, job["question"], [e["id"] for e in focus]) if len(focus) >= 2 else {}
                with self.gate:
                    if self.store.get(job["id"], "operation")["status"] == "cancelled": return
                    result = run_agent(self.store, self.provider, course=course, question=job["question"] +
                        "\n对照课堂和外部资料，先说明是否讲的是同一条件下的同一主张。逐项说明一致、条件不同、冲突或证据不足；只读摘要不得假装读过全文。不要把学术文献当作课程要求。",
                        purpose="verify", focus=focus, max_steps=2)
                result.setdefault("uncertainties", []).extend(warnings + comparison.get("notices", [])[2:])
                return self.update(job, status="completed", references=refs, result=result, comparison=comparison,
                    message=("已对照课堂与外部资料；结论附出处，分歧保留，尚未写入已确认知识。" if local else
                        "已整理外部参考，缺少对应课堂原文，暂不能完成课堂核对。"))
            if job["tool"] == "set_review_preferences":
                self.store.save("review_preferences", args, "review-preferences")
                return self.update(job, status="completed", message=f"每天复习预算 {args['daily_minutes']} 分钟，优先安排在 {args['preferred_hour']}:00")
            with self.gate:
                if self.store.get(job["id"], "operation")["status"] == "cancelled": return
                if job["tool"] == "reconstruct_class":
                    links = align_class(self.store, course, args["topic"])
                    if not links:
                        return self.update(job, status="completed", links=[], message="没有足够依据对应到题目页。请给题号、题目文件或课堂时间，暂不猜测是哪一道题。")
                    images, warnings = read_aligned_visuals(self.store, self.provider, self.settings.root, links, args["topic"])
                    focus = [e for link in links[:2] for e in (link["speech"], link["material"])] + images[:2]
                    result = run_agent(self.store, self.provider, course=course, question=args["topic"] + "\n对照课堂与题目文件，明确题目条件、老师的推导、无法确定的对应关系。", purpose="verify", focus=focus, max_steps=2 if images else 3)
                    result.setdefault("uncertainties", []).extend(warnings)
                    return self.update(job, status="completed", links=links, result=result, message="已整理关联候选；时间段和题目页可回查，尚未确认自动匹配正确")
                if job["tool"] == "export_study_pack":
                    topic = args["topic"] or job["question"]
                    prior = [t for t in self.store.list("turn") if t.get("course") == course and t.get("status") == "ready" and t.get("result", {}).get("claims")]
                    if prior: topic += "\n最近讨论：" + prior[0]["question"][:500]
                    results = []
                    count = 3 if args["kind"] == "practice" else 1
                    for index in range(count):
                        if self.stop.is_set(): return self.update(job, status="queued")
                        if self.store.get(job["id"], "operation")["status"] == "cancelled": return
                        prompt = topic + "\n本步只负责教学内容，本地导出器另行生成 PDF/Markdown，不要谈文件生成能力或声称学生需自行导出。" + (f"\n生成第 {index+1} 道练习，角度分别为概念辨析、应用推导、知识联系。此前题目：" + str([r.get("exercise", {}).get("question", "") for r in results]) if count > 1 else "\n整理核心问题、推导、适用条件。不出练习题。原话冲突和缺证据的部分单列，不能写成确定知识。")
                        result = run_agent(self.store, self.provider, course=course, question=prompt, purpose="practice" if count > 1 else "summary")
                        if result.get("support_status") != "checked":
                            raise ValueError("出处支持度检查尚未完成，暂不导出为复习资料。原文和已有结果保留，可以重试。")
                        if not result.get("claims") and not result.get("exercise"):
                            raise ValueError("缺少可引用的内容，暂不生成空资料。请先导入相关教材或课件。")
                        results.append(result); self.update(job, message=f"已整理 {index+1} / {count} 部分")
                    if self.store.get(job["id"], "operation")["status"] == "cancelled": return
                    files = save_pack(self.store, self.settings.root, job["id"], course, args["topic"], results, args["formats"])
                    return self.update(job, status="completed", files=files, message="资料已生成，附原文来源与待核对项")
            raise ValueError("工具没有可执行的处理器")
        except Exception as exc:
            return self.update(job, status="failed", message=str(exc)[:350])

    def select_lessons(self, jid, *, course_id, course_title, page_url, items):
        job = self.store.get(jid, "operation")
        if job["tool"] != "process_missing_lessons" or job["status"] not in {"waiting_browser", "running"}:
            raise ValueError("此批次不再接收课时")
        from config import parse_course_url
        page = parse_course_url(page_url)
        if page.course_id != course_id or job["course"] != course_title:
            raise ValueError("浏览器课时属于另一门课，不予导入")
        existing = self.classroom.records()
        selected = {i["sub_id"]: i for i in job["items"]}
        for item in items:
            sid = item["sub_id"]
            if sid in selected or item["status"] in {"1", "2", "3", "5", "9"}: continue
            records = [s for s in existing if s["course_id"] == course_id and s["sub_id"] == sid and not s.get("deleted_at") and not s.get("superseded_by") and s.get("time_basis") != "capture"]
            valid = [s for s in records if s.get("segments") and s.get("mode") in {"replay", "subtitle"} and s.get("status") != "failed"]
            active = next((s for s in records if not s.get("stopped") and s.get("status") not in {"failed", "cancelled", "interrupted"}), None)
            if active:
                selected[sid] = {**item, "status": "queued", "sid": active["id"], "action": "existing"}
            elif valid:
                session = max(valid, key=lambda s: len(s["segments"]))
                ready = session.get("analysis_status") == "complete" and not (session.get("summary") or {}).get("partial")
                selected[sid] = {**item, "status": "skipped" if ready else "pending", "sid": session["id"], "action": "skip" if ready else "analyze"}
            elif records:
                selected[sid] = {**item, "status": "pending", "sid": records[0]["id"], "action": "retry"}
            else:
                selected[sid] = {**item, "status": "pending", "action": "import"}
        return self.update(job, status="running", course_id=course_id, page_url=page_url, items=list(selected.values()), message=f"已核对 {len(selected)} 节回放，跳过已有完整结果")

    def item_result(self, jid, sub_id, status, sid=None, error=""):
        with self.lock:
            job = self.store.get(jid, "operation")
            items = list(job["items"])
            item = next((x for x in items if x["sub_id"] == sub_id), None)
            if not item: raise ValueError("课时不在本次批处理中")
            if sid:
                session = self.classroom.get(sid)
                if session["course_id"] != job.get("course_id") or session["sub_id"] != sub_id:
                    raise ValueError("处理结果与课时不匹配")
            if item["status"] in {"done", "skipped"}: return job
            item.update(status=status, sid=sid or item.get("sid"), error=error[:300])
            return self.update(job, items=items)

    def refresh_browser_jobs(self):
        jobs = [j for j in self.store.list("operation") if j["tool"] == "process_missing_lessons" and j["status"] == "running"]
        if not jobs: return
        sessions = {s["id"]: s for s in self.classroom.records()}
        for job in jobs:
            items = job["items"]
            for item in items:
                session = sessions.get(item.get("sid"))
                if not session or item["status"] != "queued": continue
                if session.get("status") == "failed" or session.get("analysis_status") == "failed":
                    item.update(status="failed", error=session.get("error") or session.get("analysis_error") or "处理未完成")
                elif session.get("analysis_status") == "complete" and not (session.get("summary") or {}).get("partial"): item["status"] = "done"
            finished = sum(i["status"] in {"done", "skipped", "failed"} for i in items)
            self.update(job, items=items, status="completed" if finished == len(items) else "running",
                message=f"{finished}/{len(items)} 节已返回；{sum(i['status']=='failed' for i in items)} 节失败。已有字幕的课只补笔记")
