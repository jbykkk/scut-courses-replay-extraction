from __future__ import annotations

import hashlib
import json
import re
import time
import threading
from contextlib import contextmanager

import httpx
from .study_agent.prompt_catalog import read as read_prompt
from .telemetry import traced, model_start, model_finish, phase, emit

CATEGORIES = {
    "attendance": ("点名／签到", "urgent", r"点名|签到|考勤|抽查.{0,5}到|没来的同学"),
    "qr": ("扫码互动", "urgent", r"扫.{0,4}码|二维码|雨课堂|学习通.{0,8}(答题|签到)"),
    "question": ("提问", "urgent", r"请.{0,10}回答|哪位同学|谁来.{0,5}(回答|说)|找.{0,5}同学|同学.{0,6}回答"),
    "assignment": ("作业要求", "important", r"作业|课后.{0,6}(完成|练习)|提交|截止|交.{0,4}(报告|实验)"),
    "quiz": ("测验／考试", "urgent", r"随堂.{0,5}(测试|测验)|小测|测验|考试|现在.{0,5}答题"),
    "requirements": ("课程要求", "important", r"课程要求|这门课.{0,8}(要求|需要)|必须.{0,8}(完成|参加)|教材|参考书"),
    "schedule": ("课程安排", "important", r"调课|停课|补课|下[周次节].{0,15}(讲|上|课|安排)|课程安排|上课时间"),
    "grading": ("评分要求", "important", r"平时[成绩分]|期末[成绩分]|总评|及格|学分|扣分|加分|占.{0,5}(分|%|％|百分)|百分之"),
    "reminder": ("特别提醒", "important", r"特别提醒|务必|一定要注意|重点.{0,4}(考|掌握)|考试重点|不要忘记"),
}


def event_id(category: str, evidence: str, start: float) -> str:
    return hashlib.sha256(f"{category}:{evidence}:{int(start // 20)}".encode()).hexdigest()[:20]


def rule_events(segments: list[dict]) -> list[dict]:
    result = []
    for segment in segments:
        if segment.get("quality") == "uncertain":
            continue
        text = segment["text"]
        for category, (label, priority, pattern) in CATEGORIES.items():
            if re.search(pattern, text):
                # Explicit negation should not trigger urgent attendance reminders.
                if category == "attendance" and re.search(r"(不|无需|不用|取消).{0,3}(点名|签到|考勤)", text):
                    continue
                result.append({"id": event_id(category, text, segment["start"]), "category": category,
                               "label": label, "priority": priority, "message": "疑似" + label + "，请核对原文",
                               "evidence": text, "start": segment["start"], "end": segment["end"],
                               "segment_ids": [segment["id"]], "source": "local_rule", "confidence": 0.55})
    return result


SYSTEM = read_prompt("classroom-v1")
LIVE_SYSTEM = SYSTEM + "\n实时课堂事件检测时，每段最多 6 个主题，overview 和 detail 各不超过 160 字。"
NOTES_SYSTEM = SYSTEM + "\n\n" + read_prompt("classroom-review-v1")
OUTLINE_TASK = "以下是整节课各段的知识点笔记。请综合全部内容，额外返回 title（整课主题标题），overview（整课概览），topics（3至6个主要知识主题，将相关内容归组并引用代表片段ID）。以教学内容为主，不要把开场助教介绍、课堂寒暄或作业通知作为整课主题。此步骤 events 为空，不增加原笔记没有的事实。不设字数限制，保留双语术语与定义，detail 使用 Markdown 梳理逻辑联系、解释难点。"
NOTES_PROMPT_VERSION = hashlib.sha256((NOTES_SYSTEM + "\n" + OUTLINE_TASK).encode()).hexdigest()[:12]
# Keep outline-only changes separate from the segment prompt fingerprint: saved
# detailed notes remain valid when the final aggregation strategy changes.
OUTLINE_BATCH_CHARS = 14000
OUTLINE_GUIDANCE = "\n当前步骤只生成整课导航概览，完整的双语复习讲义已另行保存，不要重新扩写逐段讲义。overview 概括知识主线，detail 简明解释主题之间的联系，保留核心术语。输入可能是部分笔记或前一轮归纳；只根据本批内容归组。每批归纳为至多 3 个主题；segment_ids 在此步骤用于归组，应覆盖本批全部输入 ID，不受代表片段数量限制。"


class OutputTooLong(ValueError):
    pass


class CitationValidationError(TypeError):
    pass


def original_material_quote(text: str, quote: str) -> str | None:
    """Ignore whitespace differences, then return the exact span from the source."""
    if quote in text:
        return quote
    needle = "".join(c for c in quote if not c.isspace())
    if not needle:
        return None
    positions = [i for i, c in enumerate(text) if not c.isspace()]
    compact = "".join(text[i] for i in positions)
    start = compact.find(needle)
    if start < 0:
        return None
    return text[positions[start]:positions[start + len(needle) - 1] + 1]


class AnalysisPaused(Exception):
    """The user stopped analysis while a request was in flight."""


EVENT_TASK = read_prompt("events-v1")


def context_packs(segments: list[dict], budget=160000) -> list[list[dict]]:
    """Keep a lecture together when possible; very long lectures overlap by 90 seconds."""
    result, start = [], 0
    while start < len(segments):
        end, size = start, 0
        while end < len(segments):
            cost = len(segments[end]["text"]) + 70
            if end > start and size + cost > budget:
                break
            size += cost
            end += 1
        result.append(segments[start:end])
        if end == len(segments):
            break
        overlap = end
        while overlap > start + 1 and segments[overlap - 1]["end"] >= segments[end - 1]["end"] - 90:
            overlap -= 1
        # At most half the previous window overlaps, so malformed timestamps still progress.
        start = max(overlap, start + max(1, (end - start) // 2))
    return result


def analysis_packs(segments: list[dict]) -> list[list[dict]]:
    """Bound both serialized input and citation count, including short school subtitles."""
    packs, current, size = [], [], 0
    for segment in segments:
        cost = len(json.dumps({k: segment[k] for k in ("start", "end", "text")}, ensure_ascii=False)) + 16
        if current and (len(current) >= 96 or size + cost > 6200):
            packs.append(current)
            current, size = [], 0
        current.append(segment)
        size += cost
    if current:
        packs.append(current)
    return packs


class Analyzer:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport
        self.client = httpx.Client(transport=transport, trust_env=False, limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
        self.proxy_client = None
        self.client_lock = threading.RLock()
        self.active_clients = {}
        self.retired_clients = set()

    def close(self):
        with self.client_lock:
            clients = {self.client, self.proxy_client} | self.retired_clients
            for client in clients - {None}:
                client.close()
            self.retired_clients.clear()

    def _client(self, provider):
        # Creation and pool replacement must be atomic across lecture workers.
        with self.client_lock:
            if provider != "openai":
                if self.client is None:
                    self.client = httpx.Client(transport=self.transport, trust_env=False,
                                               limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
                return self.client
            if self.proxy_client is None:
                self.proxy_client = httpx.Client(transport=self.transport, trust_env=True,
                                                 limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
            return self.proxy_client

    @contextmanager
    def _borrow_client(self, provider):
        with self.client_lock:
            client = self._client(provider)
            self.active_clients[client] = self.active_clients.get(client, 0) + 1
        try:
            yield client
        finally:
            with self.client_lock:
                remaining = self.active_clients[client] - 1
                if remaining:
                    self.active_clients[client] = remaining
                else:
                    self.active_clients.pop(client)
                    if client in self.retired_clients:
                        self.retired_clients.remove(client)
                        client.close()

    def _reset_client(self, provider):
        """Replace a stranded pool without closing other lectures' active requests."""
        with self.client_lock:
            if provider == "openai":
                client, self.proxy_client = self.proxy_client, None
            else:
                client, self.client = self.client, None
            if client:
                if self.active_clients.get(client):
                    self.retired_clients.add(client)
                else:
                    client.close()

    @traced("classroom")
    def consolidate(self, segments, *, checkpoint=None, save=lambda: None, check=lambda: None):
        """Read original lecture text, then reconcile repeated notices across long windows.

        Cache each completed request. A failed/paused pass never replaces the saved events.
        """
        cache = checkpoint if checkpoint is not None else {}

        def request(rows, key, depth=0, candidates=None):
            check()
            if key in cache:
                emit("cache.hit", "复用已核对的课堂事项", {"cache":"lecture_events","part":key})
                return cache[key]
            try:
                value = self._request(rows, summary=True, task=EVENT_TASK, event_candidates=candidates)["events"]
            except OutputTooLong:
                if len(rows) < 4 or depth >= 8:
                    raise ValueError("整课事项输出过长；已保留原记录和完成部分，请继续整理") from None
                mid = len(rows) // 2
                overlap = min(12, max(1, len(rows) // 8))
                value = merge_events(request(rows[:mid + overlap], key + "L", depth + 1),
                                     request(rows[mid - overlap:], key + "R", depth + 1))
                # A split can separate a deadline from its task; reunite their cited context.
                value = reconcile(value, rows, key + "RZ", depth + 1)
            check()
            cache[key] = value
            save()
            return value

        def reconcile(events, rows, key, depth=0):
            if not events:
                return []
            ids = {i for e in events for i in e["segment_ids"]}
            positions = [i for i, s in enumerate(rows) if s["id"] in ids]
            include = {j for i in positions for j in range(max(0, i - 8), min(len(rows), i + 9))}
            context = [s for i, s in enumerate(rows) if i in include]
            if depth >= 8:
                raise ValueError("事项过多，尚未完成跨段合并；已保存进度，可更换模型后继续")
            # Reclassification must not delete a notice. Reconcile all categories together,
            # with the candidate meanings AND their original surrounding text available.
            return request(context, key, depth, candidates=events)

        packs = context_packs(segments)
        events = []
        for i, pack in enumerate(packs):
            events = merge_events(events, request(pack, f"whole-{i}"))
        if len(packs) > 1:
            events = reconcile(events, segments, "reconcile-")
        check()
        return events

    @traced("classroom")
    def analyze(self, segments: list[dict], *, summary=False, materials=None, _depth=0) -> dict:
        try:
            return self._request(segments, summary=summary, materials=materials)
        except OutputTooLong:
            if len(segments) < 2 or _depth >= 8:
                raise ValueError("一段字幕的分析仍超出输出上限；已保留完成部分，请重试或更换分析模型") from None
            middle = len(segments) // 2
            left = self.analyze(segments[:middle], summary=summary, materials=materials, _depth=_depth + 1)
            right = self.analyze(segments[middle:], summary=summary, materials=materials, _depth=_depth + 1)
            return {"events": merge_events(left["events"], right["events"]),
                    "topics": left["topics"] + right["topics"],
                    "overview": left["overview"] + "\n\n" + right["overview"]}

    @traced("classroom")
    def synthesize(self, topics: list[dict], *, checkpoint=None, save=lambda: None, check=lambda: None) -> dict:
        if not topics:
            return {"title": "", "overview": "", "groups": []}
        cache = checkpoint if checkpoint is not None else {}
        nodes = [{**t, "topic_indices": [i]} for i, t in enumerate(topics)]
        task = OUTLINE_TASK + OUTLINE_GUIDANCE

        def serialized(row):
            return json.dumps(row, ensure_ascii=False, separators=(",", ":"))

        for level in range(8):
            rows, references = [], {}
            for node in nodes:
                # A single long topic must also fit. Split its text without
                # truncating it, and carry its original topic index on every part.
                pending = [node["title"] + "：" + node.get("detail", "")]
                while pending:
                    text = pending.pop()
                    row = {"id": str(len(rows)), "start": node["start"], "end": node["end"], "text": text}
                    if len(serialized(row)) + 10 > OUTLINE_BATCH_CHARS:
                        middle = len(text) // 2
                        if not middle:
                            raise ValueError("整课概览的单条输入无法分批；已保留完整笔记")
                        pending.extend([text[middle:], text[:middle]])
                        continue
                    rows.append(row)
                    references[row["id"]] = node["topic_indices"]
            packs, current, size = [], [], 2
            for row in rows:
                # Allow for commas and the longer request-local citation aliases.
                cost = len(serialized(row)) + 8
                if current and size + cost > OUTLINE_BATCH_CHARS:
                    packs.append(current)
                    current, size = [], 2
                current.append(row)
                size += cost
            if current:
                packs.append(current)

            reduced = []
            for index, pack in enumerate(packs):
                check()
                key = hashlib.sha256(serialized([task, pack]).encode()).hexdigest()
                if key in cache:
                    emit("cache.hit", "复用已完成的整课概览分批", {"cache": "lecture_outline", "part": key})
                    result = cache[key]
                else:
                    emit("analysis.outline_batch", "分批归纳整课概览",
                         {"level": level + 1, "part": index + 1, "total": len(packs),
                          "input_characters": len(serialized(pack))})
                    result = self._request(pack, summary=True, task=task)
                    check()
                    if len(packs) > 1 and not result["topics"] and not result["overview"]:
                        raise ValueError("整课概览未返回可合并的主题；已保存完整笔记和完成的概览分批，可继续生成")
                    cache[key] = result
                    save()
                groups = [{**t, "topic_indices": sorted({i for ref in t["segment_ids"] for i in references[ref]})}
                          for t in result["topics"]]
                if len(packs) == 1:
                    return {"title": result.get("title", ""), "overview": result["overview"],
                            "groups": [{k: t[k] for k in ("title", "detail", "topic_indices")} for t in groups]}
                if not groups and result["overview"]:
                    groups = [{"title": result.get("title", ""), "detail": result["overview"],
                               "start": pack[0]["start"], "end": pack[-1]["end"],
                               "topic_indices": sorted({i for row in pack for i in references[row["id"]]})}]
                if not groups:
                    raise ValueError("整课概览未返回可合并的主题；已保存完整笔记和完成的概览分批，可继续生成")
                reduced.extend(groups)
                # Uncited topics still need a place in the next pass. Retain their
                # text instead of silently dropping them from the lecture outline.
                cited = {ref for t in result["topics"] for ref in t["segment_ids"]}
                if result["topics"]:
                    reduced.extend({"title": "", "detail": row["text"], "start": row["start"], "end": row["end"],
                                    "topic_indices": references[row["id"]]} for row in pack if row["id"] not in cited)
            nodes = reduced
        raise ValueError("整课概览尚未完成分层合并；已保存完整笔记和概览进度，可继续生成")

    def _request(self, segments: list[dict], *, summary=False, task=None, event_candidates=None, materials=None) -> dict:
        if not segments:
            return {"events": [], "topics": [], "overview": "无可分析字幕"}
        llm = self.settings.llm()
        key = llm["key"]
        if not key:
            raise ValueError(f"未配置 {llm['label']} API Key；本地字幕和规则提醒仍可用")
        # Request-local references avoid echoing long database IDs hundreds of times.
        aliases = {f"s{i + 1}": s["id"] for i, s in enumerate(segments)}
        compact = [{"id": f"s{i + 1}", **{k: s[k] for k in ("start", "end", "text")}} for i, s in enumerate(segments)]
        material = {"task": task or ("课段总结和重要事项" if summary else "实时课堂事件检测"), "segments": compact}
        material_aliases = {f"m{i + 1}": item for i, item in enumerate(materials or [])}
        if material_aliases:
            material["materials"] = [{"id": alias, **{k: item[k] for k in ("title", "locator", "text")}}
                                     for alias, item in material_aliases.items()]
            material["task"] += "；结合课件与老师讲述整理知识点，按内容建立对应，不假设页序等于讲述顺序。"
        if event_candidates is not None:
            reverse = {v: k for k, v in aliases.items()}
            material["reconciliation"] = "这是跨段核对步骤。下面是候选事项及各处原文的前后文，并非连续字幕。逐项核对候选，合并同一任务的重复和补充、更正；保留互不相同的任务。排除仅为举例或没有原文支持的内容。分类可以调整，不要因分类变化遗漏事项。"
            material["candidates"] = [{**{k: e.get(k) for k in ("category", "message", "details")},
                                       "segment_ids": [reverse[i] for i in e["segment_ids"] if i in reverse]}
                                      for e in event_candidates]
        notes = summary and task != EVENT_TASK
        outline = task is not None and task.startswith(OUTLINE_TASK)
        output_budget = 16000 if notes and not outline else 6000
        payload = {"model": llm["model"], "messages": [
            {"role": "system", "content": NOTES_SYSTEM if notes else SYSTEM if summary else LIVE_SYSTEM},
            {"role": "user", "content": json.dumps(material, ensure_ascii=False, separators=(",", ":"))}],
            "response_format": {"type": "json_object"}, llm["token_param"]: output_budget,
            "stream": False}
        if llm["temperature"] is not None:
            payload["temperature"] = llm["temperature"]
        # New DeepSeek models offer non-thinking mode for latency-sensitive alerts.
        if llm["provider"] == "deepseek" and llm["model"].startswith(("deepseek-v4", "deepseek-flash")):
            payload["thinking"] = {"type": "disabled"}
        url = llm["base_url"].rstrip("/") + "/chat/completions"
        for attempt in range(3):
            body, ticket, recorded = {}, None, False
            try:
                with phase("classroom_events" if task == EVENT_TASK else "classroom_outline" if outline else "classroom_notes" if summary else "live_alerts", output_budget):
                    ticket = model_start(payload, (key,))
                    with self._borrow_client(llm["provider"]) as client:
                        response = client.post(url, timeout=180 if notes else 120 if task == EVENT_TASK else 60, json=payload,
                                               headers={"Authorization": "Bearer " + key})
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    model_finish(ticket, {}, (key,), error=f"HTTP {response.status_code}; retry {attempt+1}")
                    recorded = True
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code == 400 and "temperature" in payload:
                    try:
                        param = (response.json().get("error") or {}).get("param")
                    except ValueError:
                        param = None
                    if param == "temperature":
                        # A model that only accepts the default temperature gets
                        # one retry without the field instead of failing the job.
                        payload.pop("temperature")
                        model_finish(ticket, {}, (key,), error="HTTP 400: temperature unsupported; retrying without")
                        recorded = True
                        continue
                if response.status_code != 200:
                    raise ValueError(f"{llm['label']} HTTP {response.status_code}；请检查 Key、余额和模型名")
                body = response.json()
                model_finish(ticket, body, (key,))
                recorded = True
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise OutputTooLong("分析结果超过输出上限；已保留完成的分段笔记，可以继续生成")
                parsed = json.loads(choice["message"]["content"])
                if not isinstance(parsed, dict):
                    raise TypeError("Expected JSON object")
                for field in ("events", "topics"):
                    for item in (parsed.get(field) or []):
                        if isinstance(item, dict) and isinstance(item.get("segment_ids"), list):
                            item["segment_ids"] = [aliases.get(i, "") if isinstance(i, str) else "" for i in item["segment_ids"]]
                if task == EVENT_TASK:
                    known = {s["id"]: s for s in segments}
                    for event in parsed.get("events") or []:
                        if isinstance(event, dict) and isinstance(event.get("segment_ids"), list):
                            event["evidence_quotes"] = [known[i]["text"] for i in event["segment_ids"] if i in known]
                result = self.validate(parsed, segments, materials=material_aliases)
                if task == EVENT_TASK and len(result["events"]) != len(parsed.get("events") or []):
                    # Never turn invalid citations into a misleading "no important events" result.
                    raise CitationValidationError("Whole-lecture event citations could not be verified")
                return result
            except (httpx.TransportError, json.JSONDecodeError, KeyError, TypeError) as exc:
                if ticket and not recorded:
                    model_finish(ticket, body, (key,), error=type(exc).__name__)
                    recorded = True
                if not isinstance(exc, httpx.TransportError):
                    emit("analysis.validation_error", "模型返回内容未通过校验",
                         {"call_id": ticket[0] if ticket else None, "error_type": type(exc).__name__,
                          "error": str(exc)[:300], "attempt": attempt + 1})
                if isinstance(exc, httpx.PoolTimeout):
                    self._reset_client(llm["provider"])
                if attempt == 2:
                    if isinstance(exc, httpx.PoolTimeout):
                        reason = "请求等待可用连接超时"
                    elif isinstance(exc, httpx.TimeoutException):
                        reason = "请求超时"
                    elif isinstance(exc, httpx.TransportError):
                        reason = "连接失败或中断，请检查网络或代理"
                    elif isinstance(exc, CitationValidationError):
                        reason = "已返回分析，但引用原文未通过核对"
                    else:
                        reason = "返回内容格式无效"
                    if outline:
                        raise ValueError(f"{llm['label']} 生成整课概览时{reason}；已自动重试，字幕和分段笔记已保存，可继续生成笔记") from exc
                    raise ValueError(f"{llm['label']} {reason}；已自动重试，字幕已保存，可继续生成笔记") from exc
                time.sleep(2 ** attempt)
            finally:
                if ticket and not recorded:model_finish(ticket, body, (key,), error="模型请求未完成")
        raise ValueError(f"{llm['label']} 暂时繁忙，请稍后重试")

    @staticmethod
    def validate(body: dict, segments: list[dict], *, materials=None) -> dict:
        if not isinstance(body, dict):
            raise ValueError("分析返回值不是对象")
        known = {s["id"]: s for s in segments}

        def cited(item):
            ids = item.get("segment_ids", [])
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in known for i in ids):
                return []
            return sorted([known[i] for i in ids], key=lambda s: s["start"])

        events, topics = [], []
        for item in (body.get("events") or [])[:100]:
            if not isinstance(item, dict) or item.get("category") not in CATEGORIES:
                continue
            refs = cited(item)
            quotes = item.get("evidence_quotes")
            if quotes is None:
                quotes = [item.get("evidence")]
            if not refs or not isinstance(quotes, list) or not quotes or len(quotes) > max(24, len(known)):
                continue
            source = re.sub(r"\s", "", "".join(s["text"] for s in refs))
            if any(not isinstance(q, str) or not q.strip() or re.sub(r"\s", "", q) not in source for q in quotes):
                continue
            evidence = "\n…\n".join(quotes)
            category = item["category"]
            label, priority, _ = CATEGORIES[category]
            confidence = item.get("confidence", 0.5)
            if not isinstance(confidence, (float, int)) or not 0 <= confidence <= 1:
                confidence = 0.5
            message = item.get("message")
            if not isinstance(message, str):
                continue
            raw_details = item.get("details") or {}
            details = {k: raw_details[k][:1000] for k in ("action", "deadline", "submission", "requirements", "grading")
                       if isinstance(raw_details, dict) and isinstance(raw_details.get(k), str) and raw_details[k].strip()}
            events.append({"id": event_id(category, evidence, refs[0]["start"]), "category": category, "label": label,
                           "priority": priority, "message": message[:400], "evidence": evidence[:2000],
                           "segment_ids": [s["id"] for s in refs], "start": refs[0]["start"], "end": refs[-1]["end"],
                           "source": "deepseek", "confidence": confidence, "details": details,
                           "evidence_quotes": quotes})
        for item in (body.get("topics") or [])[:100]:
            if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                continue
            refs = cited(item)
            if not refs:
                continue
            chapter = item.get("chapter")
            # A claimed textbook location must actually appear in the cited text.
            if not isinstance(chapter, str) or chapter not in "".join(s["text"] for s in refs):
                chapter = None
            material_refs = []
            raw_refs = item.get("material_refs", [])
            if not isinstance(raw_refs, list) or len(raw_refs) > 4:
                raise CitationValidationError("Invalid courseware citations")
            for citation in raw_refs:
                if not isinstance(citation, dict):
                    raise CitationValidationError("Invalid courseware citation")
                mid, quote = citation.get("id"), citation.get("quote")
                evidence = (materials or {}).get(mid) if isinstance(mid, str) else None
                verified = original_material_quote(evidence["text"], quote) if evidence and isinstance(quote, str) and quote.strip() else None
                if verified is None:
                    raise CitationValidationError("Courseware quote could not be verified")
                material_refs.append({"evidence_id": evidence["id"], "source_id": evidence["source_id"],
                                      "title": evidence["title"], "locator": evidence["locator"], "quote": verified})
            topic = {"title": item["title"], "chapter": chapter,
                           "detail": str(item.get("detail", "")), "start": refs[0]["start"], "end": refs[-1]["end"],
                           "segment_ids": [s["id"] for s in refs]}
            if materials:
                topic["material_refs"] = material_refs
            topics.append(topic)
        return {"events": events, "topics": topics, "overview": str(body.get("overview", "")),
                "title": str(body.get("title", ""))}


def merge_events(existing: list[dict], incoming: list[dict]) -> list[dict]:
    result = list(existing)
    for event in incoming:
        same = next((i for i, e in enumerate(result) if e["category"] == event["category"] and
                     (e["id"] == event["id"] or (abs(e["start"] - event["start"]) < 20 and
                                                set(e["segment_ids"]) & set(event["segment_ids"])))), None)
        if same is None:
            result.append(event)
        elif event["source"] == "deepseek":
            result[same] = {**event, "id": result[same]["id"]}
    return sorted(result, key=lambda e: e["start"])
