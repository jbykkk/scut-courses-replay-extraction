from __future__ import annotations

import math
import hashlib
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from pathlib import Path

from .analysis import Analyzer, AnalysisPaused, NOTES_PROMPT_VERSION, analysis_packs, merge_events, rule_events
from .lecture_materials import material_context, selected_materials
from .courseware import CoursewareAlignment
from .study_agent.storage import StudyStore
from .settings import atomic_json
from .asr import Transcriber, validate_wav, repetitive_segments
from .media import Downloader, decode, split_wav
from .mobile import MobileNotifier
from .telemetry import traced, emit


def append_segments(existing: list[dict], fresh: list[dict], key: str, offset: float, duration: float) -> list[dict]:
    result = [s for s in existing if s.get("chunk") != key]  # retries replace their own output
    for index, item in enumerate(fresh):
        start, end = offset + max(0, float(item["start"])), offset + min(duration, float(item["end"]))
        text = item["text"].strip()
        if not text or not math.isfinite(start + end) or end <= start:
            continue
        # A repaired earlier chunk must never deduplicate against a later lecture sentence.
        overlap = [s for s in result if s.get("chunk") != key and s["start"] <= start < s["end"]]
        previous = max((s["end"] for s in overlap), default=0)
        if start < previous and result:
            prior = max(overlap, key=lambda s: s["end"])["text"]
            for length in range(min(len(text), len(prior), 80), 2, -1):
                if prior[-length:] == text[:length]:
                    text = text[length:].lstrip("，。！？、 ")
                    start = max(start, previous)
                    break
            if end <= previous and any(re.sub(r"\W", "", text) == re.sub(r"\W", "", s["text"]) for s in overlap):
                continue
        if text and end > start:
            result.append({"id": f"{key}:{index}", "chunk": key, "start": round(start, 3), "end": round(end, 3), "text": text,
                           **{k: item[k] for k in ("quality", "language", "language_probability", "avg_logprob") if k in item}})
    return sorted(result, key=lambda s: (s["start"], s["end"]))


class Manager:
    def __init__(self, settings, store, transcriber=None, analyzer=None):
        self.settings, self.store = settings, store
        self.asr = transcriber or Transcriber(settings)
        self.analyzer = analyzer or Analyzer(settings)
        self.mobile = MobileNotifier(settings, store)
        self.courseware = CoursewareAlignment(settings, store, getattr(self.analyzer, "transport", None))
        self.shutdown = threading.Event()
        self.threads = []
        self.last_finalize = 0
        self.worker_lock = threading.RLock()
        self.started = False

    def llm_stamp(self):
        llm = self.settings.llm()
        return f"{llm['provider']}:{llm['model']}"

    def start(self):
        self.started = True
        self.courseware.queue()
        self.configure_workers()
        self.mobile.start()

    def concurrency(self, key, default, maximum):
        try:
            return max(1, min(maximum, int(self.settings.data.get(key, default))))
        except (TypeError, ValueError):
            return default

    def worker_count(self, lane):
        return {"asr": self.concurrency("asr_workers", 4, 8), "media": 2,
                "analysis": self.concurrency("analysis_workers", 4, 8), "alignment": 1}[lane]

    def configure_workers(self):
        # Grow immediately; lowering a limit parks surplus workers after their
        # current job, without interrupting an in-flight paid request.
        with self.worker_lock:
            if not self.started or self.shutdown.is_set():
                return
            names = {t.name for t in self.threads}
            for lane in ("asr", "media", "analysis", "alignment"):
                for index in range(self.worker_count(lane)):
                    name = f"scut-{lane}-{index + 1}"
                    if name not in names:
                        thread = threading.Thread(target=self.worker, args=(lane, index), daemon=True, name=name)
                        self.threads.append(thread)
                        thread.start()

    def close(self):
        self.shutdown.set()
        self.mobile.close()
        self.courseware.close()
        if hasattr(self.analyzer, "close"):
            self.analyzer.close()
        if hasattr(self.asr, "close"):
            self.asr.close()

    def worker(self, lane, index=0):
        recent_sessions = set()
        while not self.shutdown.is_set():
            if index >= self.worker_count(lane):
                self.shutdown.wait(0.4)
                continue
            job = self.store.claim(lane, exclude_sessions=recent_sessions)
            if not job and recent_sessions:
                recent_sessions.clear()
                job = self.store.claim(lane)
            if not job:
                if lane == "asr":
                    self.finalize(force=False)
                self.shutdown.wait(0.4)
                continue
            self.execute(job)
            recent_sessions.add(job["session_id"])
            self.finalize()

    def analyze_packs(self, packs, completed, analyze, check, parallelism):
        """Fetch independently; the caller alone commits checkpoints and ordered notes."""
        missing = []
        for index, pack in enumerate(packs):
            if str(index) in completed:
                yield index, completed[str(index)]
            else:
                missing.append((index, pack))
        if parallelism == 1:
            for index, pack in missing:
                check()
                yield index, analyze(pack)
            return
        remaining = iter(missing)
        pending, failure = {}, None
        with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="scut-notes-part") as pool:
            def fill():
                while len(pending) < parallelism:
                    check()
                    item = next(remaining, None)
                    if item is None:
                        break
                    index, pack = item
                    pending[pool.submit(copy_context().run, analyze, pack)] = index
            try:
                fill()
                while pending:
                    ready, _ = wait(pending, return_when=FIRST_COMPLETED)
                    successful = []
                    for future in ready:
                        index = pending.pop(future)
                        try:
                            successful.append((index, future.result()))
                        except Exception as exc:
                            if failure is None:
                                failure = exc
                    for item in successful:
                        yield item
                    # Finish and save already-running work on failure, then retry
                    # only missing parts. Never submit the rest of the lecture.
                    if failure is None:
                        fill()
                if failure is not None:
                    raise failure
            finally:
                for future in pending:
                    future.cancel()

    def execute(self, job):
        sid, lane = job["session_id"], job["lane"]
        if self.store.get(sid)["status"] == "cancelled":
            self.store.finish(job["id"])
            return
        try:
            self.run(job)
            self.store.finish(job["id"])
        except AnalysisPaused:
            self.store.finish(job["id"])
        except Exception as exc:
            # Do not include URLs, authorization headers or third-party bodies in errors.
            if isinstance(exc, FileNotFoundError) and job["kind"] in {"chunk", "repair"}:
                error = ("本地音频片段已丢失；点击“重试字幕处理”重新下载本节回放"
                         if self.store.get(sid)["mode"] == "replay" else "录音片段已丢失，无法继续转写")
            else:
                error = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}：处理失败，请检查本地配置后重试"
            self.store.finish(job["id"], error)
            if lane == "alignment":
                self.store.update(sid, alignment_status="failed", alignment_error=error,
                                  alignment_progress="", alignment_retry_at=time.time() + 60)
                return
            if self.store.get(sid)["status"] != "cancelled":
                changes = {"warning" if lane == "analysis" else "error": error}
                if lane == "analysis":
                    changes["analysis_status"] = "failed"
                    if job["kind"] == "summary":
                        attempt = self.store.get(sid).get("notes_auto_attempts", 1)
                        changes["notes_retry_at"] = time.time() + 60 * 5 ** min(max(attempt - 1, 0), 2)
                self.store.update(sid, **changes)

    @traced("classroom_job")
    def run(self, job):
        sid, payload, kind = job["session_id"], job["payload"], job["kind"]
        if kind == "align_courseware":
            self.courseware.run(sid, payload["generation"])
        elif kind in {"chunk", "repair"}:
            path = Path(payload["path"])
            started = time.monotonic()
            fresh = self.asr.transcribe(path)
            if repetitive_segments(fresh):
                # Also protect alternative engines (such as XXL) from sending loops to analysis.
                path.with_suffix(".asr.json").write_text(json.dumps({"audio": path.name, "reason": "extreme_repetition", "raw": fresh}, ensure_ascii=False, indent=2), encoding="utf-8")
                fresh = [{"start": min(s["start"] for s in fresh), "end": max(s["end"] for s in fresh),
                          "text": "〔此段识别不可靠，已保留音频待复核〕", "quality": "uncertain"}]
            if self.store.get(sid)["status"] == "cancelled":
                return
            with self.store.lock:
                session = self.store.get(sid)
                chunk_key = payload.get("replace_key", job["id"])
                segments = append_segments(session["segments"], fresh, chunk_key, payload["start"], payload["duration"])
                added = [s for s in segments if s["chunk"] == chunk_key]
                previous_events = [e for e in session["events"] if not (kind == "repair" and any(i.startswith(chunk_key + ":") for i in e["segment_ids"]))]
                events = merge_events(previous_events, rule_events(added))
                self.store.update(sid, segments=segments, events=events,
                                  last_latency=round(time.monotonic() - started, 2), error="")
                self.maybe_analyze(sid)
        elif kind == "media":
            folder = self.store.directory(sid) / "media"
            folder.mkdir(exist_ok=True)
            cancelled = lambda: self.shutdown.is_set() or self.store.get(sid)["status"] == "cancelled"
            self.store.update(sid, status="downloading", progress="正在下载回放媒体")
            source = Downloader(self.settings, cancelled, lambda t: self.store.update(sid, progress=t)).fetch(payload["url"], folder)
            self.store.update(sid, status="transcribing", progress="提取音轨")
            wav = folder / "audio.wav"
            decode(self.settings, source, wav, cancelled)
            for path, start, duration in split_wav(wav, folder):
                if cancelled():
                    return
                self.store.enqueue(sid, "chunk", {"path": str(path), "start": start, "duration": duration},
                                   key=f"{sid}-replay-{int(start)}", priority=20)
            self.store.release_waiting_media(sid)
            self.store.update(sid, stopped=True, progress="音轨已就绪，按顺序转写")
        elif kind in {"analyze", "summary"}:
            session = self.store.get(sid)
            saved_summary = session.get("summary")
            reset_at = session.get("analysis_reset_at", 0)
            if job.get("created", time.time()) < reset_at:
                return
            self.store.update(sid, analysis_status="running", analysis_stage="segments", warning="")
            segments = [s for s in session["segments"] if s.get("quality") != "uncertain"]
            if kind == "analyze":
                segments = [s for s in segments if payload["start"] <= s["end"] <= payload["end"]]
                packs = [segments]
            else:
                packs = analysis_packs(segments)
            model = self.llm_stamp()
            study = StudyStore(self.settings.root) if kind == "summary" else None
            sources = selected_materials(study, session) if study else []
            material_stamp = [[s["id"], s["fingerprint"]] for s in sources]
            fingerprint = hashlib.sha256(json.dumps(["summary-v4-review", NOTES_PROMPT_VERSION, model, reset_at, segments, material_stamp,
                session.get("alignment_generation")], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            checkpoint = self.store.directory(sid) / "analysis-cache" / (fingerprint + ".json")
            completed = {}
            if kind == "summary" and checkpoint.exists():
                try:
                    completed = json.loads(checkpoint.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    completed = {}
            results = {i: completed[str(i)] for i in range(len(packs)) if str(i) in completed}
            if kind == "summary":
                done = len(results)
                self.store.update(sid, analysis_done=done, analysis_total=len(packs),
                                  analysis_progress=f"{done}/{len(packs)}", analysis_model=model)

            def check_pack():
                current = self.store.get(sid)
                if (current["status"] == "cancelled" or current.get("analysis_reset_at", 0) != reset_at
                        or self.shutdown.is_set()):
                    raise AnalysisPaused()
                if kind == "analyze" and current.get("analysis", True) is False and not self.settings.mobile_enabled():
                    self.store.update(sid, analysis_status="paused")
                    raise AnalysisPaused()
                if self.llm_stamp() != model:
                    raise ValueError("分析模型已切换，请点击继续生成笔记，使用新模型整理本课")

            def analyze_pack(pack):
                check_pack()
                context = (self.courseware.context(sid, pack, sources) or material_context(study, sources, pack)) if sources else []
                return self.analyzer.analyze(pack, summary=kind == "summary", **({"materials": context} if context else {}))

            parallelism = self.concurrency("analysis_parallelism", 3, 4) if kind == "summary" else 1
            overviews, topics, sections = [], [], []
            for index, result in self.analyze_packs(packs, completed, analyze_pack, check_pack, parallelism):
                if self.llm_stamp() != model:
                    raise ValueError("分析模型已切换，请点击继续生成笔记，使用新模型整理本课")
                with self.store.lock:
                    session = self.store.get(sid)
                    if session.get("analysis_reset_at", 0) != reset_at or session["status"] == "cancelled":
                        return  # A transcription repair invalidated the in-flight response.
                    if str(index) in completed:
                        emit("cache.hit", "复用已完成的课堂段落", {"cache":"lecture_checkpoint","section":index+1})
                    results[index] = result
                    if kind == "summary":
                        completed[str(index)] = result
                        atomic_json(checkpoint, completed)
                    # Requests may finish out of order; notes and citations always
                    # follow the original video, including partially saved results.
                    ordered = [(packs[i], results[i]) for i in sorted(results)]
                    topics = [topic for _, value in ordered for topic in value["topics"]]
                    overviews = [value["overview"] for _, value in ordered]
                    sections = [{"start": pack[0]["start"], "end": pack[-1]["end"],
                                 "overview": value["overview"]} for pack, value in ordered]
                    done = len(results)
                    partial = {"summary": {"overview": "\n\n".join(overviews), "topics": topics,
                               "sections": sections, "source": "deepseek", "partial": True,
                               "material_source_ids": [s["id"] for s in sources]},
                               "analysis_done": done, "analysis_total": len(packs)} if kind == "summary" else {}
                    if saved_summary and not saved_summary.get("partial"):
                        partial.pop("summary", None)  # Keep the readable old notes during a refresh.
                    self.store.update(sid, events=(merge_events(session["events"], result["events"]) if kind == "analyze" else session["events"]),
                                      analysis_progress=f"{done}/{len(packs)}", **partial)
                if kind == "summary":
                    while urgent := self.store.claim("analysis", max_priority=0):
                        self.execute(urgent)
            update = {"analysis_status": "complete", "warning": ""}
            if kind == "summary":
                check_context = check_pack
                self.store.update(sid, analysis_progress="生成整课概览", analysis_stage="outline")
                outline = completed.get("outline")
                if outline is None:
                    outline_cache = completed.setdefault("outline-parts-v1", {})
                    outline = self.analyzer.synthesize(topics, checkpoint=outline_cache,
                                                      save=lambda: atomic_json(checkpoint, completed), check=check_context)
                    completed["outline"] = outline
                    atomic_json(checkpoint, completed)
                if self.llm_stamp() != model:
                    raise ValueError("分析模型已切换，请点击继续生成笔记，使用新模型整理本课")

                check_context()
                self.store.update(sid, analysis_progress="通读全文，合并作业与要求", analysis_stage="events")
                event_cache = completed.setdefault("context-events-v3", {})
                events = self.analyzer.consolidate(segments, checkpoint=event_cache,
                                                  save=lambda: atomic_json(checkpoint, completed), check=check_context)
                check_context()
                # Commit the new semantic list only after the entire pass succeeds.
                # Keep keyword hits for inspection, but never present them as finished notes.
                update.update(events=events, rule_candidates=rule_events(segments), events_version=3)
                update["summary"] = {"overview": "\n\n".join(overviews), "topics": topics, "sections": sections,
                                     "source": "deepseek", "partial": False, "headline": outline["title"],
                                     "material_source_ids": [s["id"] for s in sources],
                                     "abstract": outline["overview"], "groups": outline["groups"]}
                update.update(analysis_progress=f"{len(packs)}/{len(packs)}", analysis_stage="complete")
            with self.store.lock:
                current = self.store.get(sid)
                if current.get("analysis_reset_at", 0) == reset_at and current["status"] != "cancelled" and (kind == "summary" or current.get("analysis", True)):
                    self.store.update(sid, **update)

    def maybe_analyze(self, sid):
        session = self.store.get(sid)
        if any(j["kind"] == "repair" and j["state"] in {"pending", "running"} for j in self.store.jobs(sid)):
            return
        if (not session.get("analysis") and not self.settings.mobile_enabled()) or session["mode"] != "live" or not session["segments"]:
            return
        end = max(s["end"] for s in session["segments"])
        if end - session["last_analysis_end"] < self.settings.data["analysis_window"]:
            return
        if self.store.pending(sid, "analysis"):
            return
        self.store.enqueue(sid, "analyze", {"start": max(0, session["last_analysis_end"] - 90), "end": end},
                           lane="analysis", priority=0)
        self.store.update(sid, last_analysis_end=end)

    def finalize(self, *, force=True):
        with self.store.lock:
            if not force and time.monotonic() - self.last_finalize < 5:
                return
            self.last_finalize = time.monotonic()
            for session in self.store.list():
                sid = session["id"]
                if session["status"] in {"cancelled", "complete", "failed"}:
                    continue
                if (session["mode"] == "live" and session["status"] == "recording" and not session["stopped"]
                        and time.time() - max(session.get("last_received_at", 0), session["created_at"]) > 600):
                    self.store.update(sid, status="interrupted",
                                      warning="超过 10 分钟未收到录音片段，已暂停显示；浏览器恢复上传后会继续记录")
                jobs = self.store.jobs(sid)
                repairs = [j for j in jobs if j["kind"] == "repair"]
                if repairs:
                    repair_status = "running" if any(j["state"] in {"pending", "running"} for j in repairs) else "failed" if any(j["state"] == "failed" for j in repairs) else "complete"
                    if session.get("repair_status") != repair_status:
                        self.store.update(sid, repair_status=repair_status)
                audio = [j for j in jobs if j["lane"] in {"asr", "media"}]
                if any(j["state"] == "failed" for j in audio):
                    if session["stopped"] or session["mode"] != "live":
                        self.store.update(sid, status="failed")
                    continue
                if not session["stopped"] or any(j["state"] in {"pending", "running"} for j in audio):
                    continue
                if audio and not session["segments"]:
                    self.store.update(sid, status="failed", error="音轨处理完成，但没有识别出可用语音；请检查音源是否有声音，或切换识别语言后重试")
                    continue
                self.store.update(sid, status="complete", progress="处理完成")
                if not self.settings.data["retain_audio"]:
                    # Only temporary audio/media created inside this generated session directory.
                    directory = self.store.directory(sid).resolve()
                    for child in directory.rglob("*"):
                        if child.is_file() and child.suffix in {".wav", ".bin", ".media", ".m3u8"} and child.resolve().is_relative_to(directory):
                            # Keep a flagged input and its recognition audit for review.
                            audit = child.with_suffix(".asr.json")
                            if not audit.exists():
                                child.unlink()
            self.courseware.queue()
            self.queue_saved_notes()

    def queue_saved_notes(self):
        """Reconcile saved subtitles independently of the live reminder preference.

        Call under the store lock. Persist attempt counts so reopening the service
        cannot endlessly repeat a failing paid request. Successful packs resume
        from the existing analysis cache.
        """
        try:
            llm = self.settings.llm()
            key, stamp = llm["key"], llm["provider"] + ":" + llm["model"]
        except ValueError:
            key, stamp = "", ""  # A vault error must not stop the audio worker or subtitle saving.
        for session in self.store.records():
            sid = session["id"]
            if session.get("superseded_by") or not session["stopped"] or session["status"] != "complete":
                continue
            if not any(s.get("quality") != "uncertain" for s in session["segments"]):
                continue
            summary = session.get("summary")
            if summary and not summary.get("partial") and session.get("analysis_status") == "complete":
                continue
            jobs = self.store.jobs(sid)
            if any(j["state"] in {"pending", "running"} or (j["lane"] in {"asr", "media"} and j["state"] == "failed") for j in jobs):
                continue
            if not key:
                if session.get("analysis_status") != "waiting_key":
                    self.store.update(sid, analysis_status="waiting_key", analysis_progress="保存 Key 后自动生成笔记")
                continue
            generation = hashlib.sha256(json.dumps([stamp, key,
                session.get("analysis_reset_at", 0), session["segments"]], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            same = session.get("notes_auto_generation") == generation
            attempts = session.get("notes_auto_attempts", 0) if same else 0
            if attempts >= 3 or (same and session.get("notes_retry_at", 0) > time.time()):
                continue
            attempt = attempts + 1
            self.store.enqueue(sid, "summary", {}, lane="analysis", priority=10,
                               key=f"{sid}-auto-notes-{generation}-{attempt}")
            self.store.update(sid, analysis_status="queued", analysis_progress="等待自动整理", warning="",
                              notes_auto_generation=generation, notes_auto_attempts=attempt, notes_retry_at=0)
