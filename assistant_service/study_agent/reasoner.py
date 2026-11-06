from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .storage import digest
from .prompt_catalog import read as read_prompt, study_prompt
from .context import evidence_payload, pack_evidence, history_context, select_memory
from .classroom_search import classroom_evidence, mention_cards, attach_locations, topic_query
from ..telemetry import RuntimeLog, compact, current, emit, model_start, model_finish, model_phase, phase, traced


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(Strict):
    evidence_id: str = Field(max_length=80)
    quote: str = Field(min_length=4, max_length=500)


class Claim(Strict):
    text: str = Field(min_length=1, max_length=1200)
    citations: list[Citation] = Field(min_length=1, max_length=5)


class Proposal(Strict):
    kind: Literal["task", "correction", "requirement"]
    title: str = Field(min_length=1, max_length=150)
    detail: str = Field(max_length=1400)
    citations: list[Citation] = Field(min_length=1, max_length=5)
    due_text: str = Field(default="", max_length=150)


class Exercise(Strict):
    question: str = Field(min_length=1, max_length=1200)
    expected_answer: str = Field(min_length=1, max_length=1800)
    citations: list[Citation] = Field(min_length=1, max_length=5)


class Connection(Strict):
    concept_a: str = Field(min_length=1, max_length=120)
    concept_b: str = Field(min_length=1, max_length=120)
    relationship: str = Field(min_length=1, max_length=1000)
    citations: list[Citation] = Field(min_length=2, max_length=5)


class Final(Strict):
    action: Literal["finish"]
    claims: list[Claim] = Field(default_factory=list, max_length=10)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    proposals: list[Proposal] = Field(default_factory=list, max_length=6)
    exercise: Exercise | None = None
    reply: str = Field(default="", max_length=1000)
    next_question: str = Field(default="", max_length=200)
    connections: list[Connection] = Field(default_factory=list, max_length=4)


class Search(Strict):
    action: Literal["search"]
    query: str = Field(min_length=1, max_length=200)


class Read(Strict):
    action: Literal["read"]
    evidence_id: str = Field(max_length=80)


class Provider:
    """Small OpenAI-compatible transport; provider secrets never enter prompts or traces."""

    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(transport=transport, follow_redirects=False, trust_env=True, limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))

    def close(self):
        self.client.close()

    def config(self):
        path = self.settings.root / "study-providers.json"
        if not path.exists():
            llm = self.settings.llm()
            return {"id": llm["provider"], "base_url": llm["base_url"], "model": llm["model"],
                    "key": llm["key"], "json_mode": True, "disable_thinking": llm["provider"] == "deepseek",
                    "token_param": llm["token_param"]}
        config = json.loads(path.read_text(encoding="utf-8"))
        selected = next((p for p in config["providers"] if p["id"] == config["selected"]), None)
        if not selected:
            raise ValueError("学习模型配置中没有所选服务")
        parsed = urlparse(selected["base_url"])
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("模型服务须使用 HTTPS；本机服务可用 HTTP")
        env_name = selected.get("key_env", "")
        if env_name and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", env_name):
            raise ValueError("模型 Key 必须引用环境变量名")
        # Only the exact official endpoint can reuse an existing saved credential.
        if selected.get("use_saved_deepseek_key") and parsed.netloc == "api.deepseek.com":
            key = self.settings.key()
        elif selected.get("use_saved_openai_key") and parsed.netloc == "api.openai.com":
            key = self.settings.openai_key()
        else:
            key = os.getenv(env_name, "")
        if not local and not key:
            raise ValueError("学习模型的 Key 尚未配置")
        return {**selected, "key": key}

    def public(self):
        try:
            cfg = self.config()
            vision = cfg.get("supports_images", cfg.get("id") == "openai"
                             or (cfg.get("model") in {"deepseek-flash", "deepseek-v4-flash-vision-exp"}
                                 and urlparse(cfg["base_url"]).netloc == "api.deepseek.com"))
            return {"id": cfg["id"], "model": cfg["model"], "vision": bool(vision), "configured": bool(cfg.get("key")) or urlparse(cfg["base_url"]).hostname in {"localhost", "127.0.0.1", "::1"}}
        except (ValueError, KeyError, TypeError):
            return {"configured": False, "model": "配置需要检查"}

    def _request(self, cfg, payload, timeout):
        ticket = model_start(payload, (cfg.get("key"),))
        body = {}
        try:
            response = self.client.post(cfg["base_url"].rstrip("/") + "/chat/completions",
                content=compact(payload).encode(), timeout=timeout,
                headers={"Authorization": "Bearer " + cfg.get("key", ""), "Content-Type":"application/json"})
            if response.status_code != 200:
                raise ValueError(f"学习模型 HTTP {response.status_code}，原有资料和计划未改变")
            if len(response.content) > 512_000:
                raise ValueError("学习模型响应过大")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("模型输出未完成，请缩小问题范围后重试")
            model_finish(ticket, body, (cfg.get("key"),))
            return choice["message"]
        except Exception as exc:
            model_finish(ticket, body, (cfg.get("key"),), error=str(exc))
            if isinstance(exc, ValueError): raise
            raise ValueError("学习模型暂时没有返回有效结果；可先查看本地来源") from exc

    @traced("model")
    def complete(self, messages, timeout):
        cfg = self.config()
        if not cfg.get("key") and urlparse(cfg["base_url"]).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("尚未配置学习模型；可先在设置中保存 AI 服务 Key")
        payload = {"model":cfg["model"], "messages":messages, cfg.get("token_param","max_tokens"):model_phase()[1]}
        if cfg.get("json_mode", True): payload["response_format"]={"type":"json_object"}
        if cfg.get("disable_thinking"): payload["thinking"]={"type":"disabled"}
        return self._request(cfg, payload, timeout)["content"]

    def choose_tool(self, question, course, tools):
        """Native function calling for operation selection, without a general agent SDK.

        Tool arguments are proposals. The local executor revalidates capability,
        literal path/URL scope, idempotency and course before any side effect.
        """
        cfg = self.config()
        messages = [{"role": "system", "content":
            read_prompt("route-v1")},
            {"role": "user", "content": json.dumps({"course": course, "request": question}, ensure_ascii=False)}]
        payload = {"model": cfg["model"], "messages": messages, "tools": tools, "tool_choice": "auto", cfg.get("token_param", "max_tokens"): 650}
        if cfg.get("disable_thinking"):
            payload["thinking"] = {"type": "disabled"}
        try:
            with phase("route", 650):
                calls = self._request(cfg, payload, 25).get("tool_calls", [])
            if not calls:
                return None
            if len(calls) != 1:
                raise ValueError("请一次执行一个课程操作")
            fn = calls[0]["function"]
            return {"tool": fn["name"], "arguments": json.loads(fn["arguments"])}
        except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("操作识别暂时失败。可以用‘读取 路径’、‘处理本课未处理的回放’或‘导出复习 PDF’重试。") from exc

    def audit_result(self, result, evidence):
        entries = [{"id": "claim-"+str(i), "text": x["text"], "citations": x["citations"]}
                   for i, x in enumerate(result.get("claims", []))]
        entries += [{"id": "link-"+str(i), "text": x["relationship"], "citations": x["citations"]}
                    for i, x in enumerate(result.get("connections", []))]
        if result.get("exercise"):
            x = result["exercise"]
            entries.append({"id":"exercise", "text":x["question"]+"\n参考答案："+x["expected_answer"], "citations":x["citations"]})
        if not entries: return {}
        ids = {c["evidence_id"] for e in entries for c in e["citations"]}
        source_data = [evidence_payload(evidence[eid]) for eid in sorted(ids)]
        cfg = self.config()
        cache_key = digest(["support-v2", cfg["base_url"], cfg["model"], read_prompt("audit-v1"), entries, source_data])
        try:
            cache = RuntimeLog(self.settings.root)
            cached = cache.cache_get(cache_key)
        except (sqlite3.Error,OSError):
            cache,cached = None,None
        if cached is not None:
            emit("cache.hit", "复用已核对的相同结论", {"cache":"support", "entries":len(entries), "key":cache_key[:12]})
            return cached
        with phase("audit", min(3200, 400 + len(entries)*200)):
            raw = self.complete([{"role":"system", "content":read_prompt("audit-v1")},
                {"role":"user", "content":compact({"entries":entries,"source_data":source_data})}],25)
        checks=json.loads(raw).get("checks",[])
        if not isinstance(checks,list) or len(checks)!=len(entries): raise ValueError("支持度检查没有覆盖全部结论")
        result_map={}
        for check in checks:
            if check.get("id") in result_map or check.get("status") not in {"supported","insufficient","conflict"}:
                raise ValueError("支持度检查结构无效")
            result_map[check["id"]]={"status":check["status"],"reason":str(check.get("reason",""))[:400]}
        if set(result_map)!={e["id"] for e in entries}: raise ValueError("支持度检查条目不匹配")
        if cache is not None:
            try:cache.cache_put(cache_key, result_map)
            except (sqlite3.Error,OSError):pass
        return result_map


SYSTEM = study_prompt("ask")


def check_citations(value, allowed):
    def validate(citations):
        for citation in citations:
            item = allowed.get(citation.evidence_id)
            parts = re.split(r"\s+", citation.quote.strip())
            match = re.search(r"\s+".join(re.escape(part) for part in parts), item["text"]) if item and parts != [""] else None
            if not match:
                raise ValueError("引用不在本次读取的原文中")
            # PDF line wrapping is typography, not a changed word. Return the real
            # contiguous source slice, never the model's reflowed rendition.
            citation.quote = match.group(0)
    for item in [*value.claims, *value.proposals, *value.connections, *([value.exercise] if value.exercise else [])]:
        validate(item.citations)
    for connection in value.connections:
        if len({c.evidence_id for c in connection.citations}) < 2:
            raise ValueError("知识关联需要两处不同证据，不能重复同一引文")
    for proposal in value.proposals:
        from .research import source_family
        if proposal.kind == "correction" and len({source_family(allowed[c.evidence_id]) for c in proposal.citations}) < 2:
            raise ValueError("转写纠正必须有不同来源支持；单一来源只能标记不确定")
        # A deadline is preserved as an exact quote, never parsed into a guessed date.
        if proposal.due_text and not any(proposal.due_text in allowed[c.evidence_id]["text"] for c in proposal.citations):
            raise ValueError("截止时间没有原文支持")


@traced("study")
def run_agent(store, provider, *, course, question, purpose="ask", history=None, focus=None, max_steps=3):
    started = time.monotonic()
    from .knowledge import annotate_claims, relevant_memory
    allowed, sent = {}, set()
    trace = []
    source_budget = 24000
    source_characters = 0

    def include(items):
        nonlocal source_characters
        new = [e for e in items if e["id"] not in sent and e["course"] == course
               and e.get("active", True) and not e.get("policy", {}).get("excluded")]
        packed, stats = pack_evidence(new, max(0, source_budget-source_characters))
        chosen = {e["id"] for e in packed}
        allowed.update({e["id"]:e for e in new if e["id"] in chosen})
        sent.update(chosen); source_characters += stats["characters"]
        emit("context.sources", "选入原文", {**stats, "used_characters":source_characters,
             "total_budget":source_budget, "evidence_ids":list(chosen)})
        return packed

    def retrieve(query):
        emit("tool.start", "检索本课资料", {"tool":"search_sources", "arguments":{"query":query,"course":course}})
        candidates = store.search(query, course, 12)
        references = [e for e in candidates if e["kind"] not in {"asr", "school_subtitle", "image_reading", "student_report"}]
        references.sort(key=lambda e: not e.get("policy", {}).get("trusted"))
        lectures = classroom_evidence(store, course, query, 6) if purpose == "classroom" else []
        found = list({e["id"]:e for e in [*lectures, *references[:2], *([] if purpose == "classroom" else candidates)]}.values())[:8 if purpose == "classroom" else 6]
        trace.append({"tool":"search_sources", "query":query, "found":len(found)})
        emit("tool.finish", "检索完成", {"tool":"search_sources", "result":[{"id":e["id"],"title":e["title"],"locator":e.get("locator",{}),"reliability":e.get("reliability")} for e in found]})
        return include(found)

    history = history_context(history or [], question)
    prior_topic = " ".join(x.get("question", "")[:160] for x in history[-2:])
    initial = include([] if purpose == "classroom" else (focus or [])[:6])
    initial += retrieve(topic_query(question, course) if purpose == "classroom" else question + " " + prior_topic)
    def classroom_cards():
        return mention_cards([e for e in allowed.values() if e.get("locator", {}).get("session_id")], topic_query(question, course))[:8]
    if purpose == "classroom" and not classroom_cards():
        return {"status":"needs_sources", "reply":"已导入的课堂记录里还没找到对应原句。可以换一个术语，或先整理相关回放。", "claims":[], "proposals":[], "uncertainties":[], "classroom_matches":[], "trace":trace}
    catalog = [{k:v for k,v in item.items() if k in {"title","kind"}} for item in store.sources(course)][:25]
    if not initial and not catalog:
        return {"status":"needs_sources", "reply":"这门课还没有可参考的内容。发一页课件、截图或相关原文就可以。", "claims":[], "proposals":[], "uncertainties":[], "trace":trace}
    memory = select_memory([{"title":x["title"],"detail":x["detail"],"source":x.get("source","student_confirmed_proposal")}
        for x in store.list("proposal") if x.get("course")==course and x.get("status")=="accepted" and x.get("proposal_kind")=="requirement"], question+" "+prior_topic, 4, fallback=bool(re.search(r"要求|规则|评分|考勤|提交|作业",question)))
    learner_state = select_memory([{"topic":p["question"], "attempt_count":len(p["attempts"]), "last_self_rating":p["attempts"][-1]["rating"]}
        for p in store.list("practice") if p.get("course")==course and p.get("attempts")], question+" "+prior_topic, 3)
    tasks = select_memory([{k:t.get(k) for k in ("title","deadline","estimate_min","estimate_max","spent_minutes","completed")}
        for t in sorted(store.list("task"),key=lambda t:t.get("deadline") or "9999") if t.get("course")==course and t.get("status")=="accepted" and not t.get("completed")], question+" "+prior_topic, 4, fallback=bool(re.search(r"做什么|任务|安排|截止|提交|作业|来得及",question)))
    knowledge = relevant_memory(store, course, question+" "+prior_topic, limit=4)
    # Confirmed memory is only a retrieval pointer; load its current originals before use.
    initial += include([store.evidence(c["evidence_id"]) for m in knowledge for c in m["citations"]])
    context = {"course":course,"purpose":purpose,"confirmed_requirements":memory,
        "source_catalog":catalog,"source_data":initial,"confirmed_knowledge":knowledge,
        "self_reported_learning":learner_state,"confirmed_tasks":tasks,"conversation":history,"question":question}
    messages=[{"role":"system","content":study_prompt(purpose)}, {"role":"user","content":compact(context)}]
    emit("context.ready", "本轮上下文已准备", {"prompt_version":"study-v2", "purpose":purpose,
        "characters":sum(len(m["content"]) for m in messages), "history_turns":len(history),
        "memory":{"requirements":memory,"tasks":tasks,"learning":learner_state,"knowledge":knowledge},
        "source_count":len(allowed), "source_budget":source_budget})
    failure = None
    try:
        for step in range(max_steps):
            remaining = 100 - (time.monotonic() - started)
            if remaining < 5:
                raise ValueError("本轮整理达到时间上限，已保存来源，可缩小问题后继续")
            with phase("answer", 3500 if purpose in {"summary", "connect", "practice"} else 2400):
                raw = provider.complete(messages, min(30, remaining))
            trace.append({"step": step + 1, "operation": "model_response"})
            try:
                if not isinstance(raw, str) or len(raw) > 30000:
                    raise ValueError("响应超出约定长度")
                data = json.loads(raw)
                if data.get("action") == "read" and step < max_steps - 1:
                    target = Read.model_validate(data).evidence_id
                    if target not in allowed:
                        raise ValueError("只能展开已读的课程来源")
                    sid, index = target.rsplit(":", 1)
                    emit("tool.start", "读取相邻原文", {"tool":"read_surrounding_context","arguments":{"evidence_id":target}})
                    found = []
                    for n in range(max(0, int(index) - 1), int(index) + 2):
                        try:
                            item = store.evidence(sid + ":" + str(n))
                            if item["course"] == course and item["active"] and not item.get("policy", {}).get("excluded") :
                                found.append(item)
                        except KeyError:
                            pass
                    trace.append({"tool": "read_surrounding_context", "evidence_id": target, "found": len(found)})
                    packed = include(found)
                    emit("tool.finish", "相邻原文读取完成", {"tool":"read_surrounding_context","new_evidence_ids":[e["id"] for e in packed]})
                    messages += [{"role":"assistant","content":raw}, {"role":"user","content":compact({"source_data":packed, "already_read":[e["id"] for e in found if e["id"] in sent], "instruction":"没有新片段时根据已有资料完成，缺失信息列为不确定。"})}]
                    continue
                if data.get("action") == "search" and step < max_steps - 1:
                    search = Search.model_validate(data)
                    if sum(len(m["content"]) for m in messages) > 40000:
                        raise ValueError("上下文达到上限，请根据已有证据完成")
                    messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": compact({"source_data":retrieve(search.query),"already_read":sorted(sent),"instruction":"没有新片段时根据已有资料完成，不重复相同检索。"})}]
                    continue
                value = Final.model_validate(data)
                if purpose == "practice":
                    if value.exercise is None:
                        raise ValueError("练习请求需要一道完整题目")
                    # A recall exercise must not reveal its answer in surrounding prose.
                    value.claims, value.proposals, value.connections = [], [], []
                    value.reply = "先不看资料，试着用自己的话回答。写下卡住的地方也可以。"
                    value.next_question = ""
                else:
                    value.exercise = None
                check_citations(value, allowed)
            except (ValidationError, ValueError, AttributeError) as exc:
                failure = "模型的结构或引文校验未通过"
                if step < max_steps - 1:
                    messages.append({"role": "user", "content": "上次输出未通过结构/引文校验。请按约定 JSON 格式完成，quote 逐字引用已给来源，不补写日期。"})
                    continue
                raise ValueError(failure) from exc
            result = value.model_dump(exclude={"action"})
            annotate_claims(store, result, allowed)
            audit = {}
            if hasattr(provider, "audit_result"):
                try:
                    emit("validation.start", "核对结论与原文", {"claims":len(result["claims"]),"connections":len(result["connections"])})
                    audit = provider.audit_result(result, allowed)
                    emit("validation.finish", "出处核对完成", {"checks":audit})
                    trace.append({"tool":"check_source_support","items":len(audit)})
                    for field, prefix in (("claims","claim-"),("connections","link-")):
                        retained=[]
                        for i, item in enumerate(result[field]):
                            check=audit.get(prefix+str(i),{})
                            if check.get("status") != "supported":
                                result["uncertainties"].append("未采用一条依据不足的解释："+check.get("reason","需要更多原文"))
                            else:
                                item["support_check"]=check
                                retained.append(item)
                        result[field]=retained
                    if result["exercise"] and audit.get("exercise",{}).get("status") != "supported":
                        result["uncertainties"].append("这道题的参考答案依据不足，暂不保存或安排复习："+audit.get("exercise",{}).get("reason","需要题目原件或教材"))
                        result["exercise"]=None
                    result["support_status"]="checked"
                    if any(c["status"] != "supported" for c in audit.values()):
                        result["reply"] = "部分解释缺少足够原文支持，已列在待核对项中。"
                except (ValueError, KeyError, TypeError):
                    emit("validation.error", "出处核对未完成", {"retained":"原文与未确认回答"})
                    result["support_status"]="unavailable"
                    result["uncertainties"].append("本次出处支持度检查未完成；回答仅供对照原文，暂不能加入确认知识或生成复习文件。")
                    for claim in result["claims"]:
                        claim["verification"]={"status":"needs_check","label":"出处检查未完成，请对照原文"}
                    if result["exercise"]:
                        result["exercise"]=None
                        result["reply"]="题目的出处检查未完成，暂不提供未经核对的参考答案。可以稍后重试。"
            else:
                result["support_status"]="unavailable"
            if any(item.get("kind") == "image_reading" for item in allowed.values()):
                result["uncertainties"].append("图片内容由模型识读，尚未人工核对。公式、数字和模糊文字请点开原图确认。")
            saved = []
            for proposal in result["proposals"]:
                kind = proposal.pop("kind")
                # Knowledge correction cannot be based on two correlated noisy transcripts.
                if kind == "correction" and all(allowed[c["evidence_id"]]["kind"] in {"asr", "school_subtitle", "image_reading"} for c in proposal["citations"]):
                    result["uncertainties"].append(proposal["detail"] + "（目前只有转写/识图，修正待核对）")
                    continue
                pid = digest([course, kind, proposal])[:32]
                try:
                    item = store.get(pid, "proposal")  # Preserve a previous accept/reject decision.
                except KeyError:
                    item = store.save("proposal", {**proposal, "proposal_kind": kind, "course": course, "status": "pending"}, pid)
                saved.append(item)
            result["proposals"] = saved
            if result["exercise"]:
                practice = store.save("practice", {**result["exercise"], "course": course, "attempts": [], "due_at": time.time()})
                result["exercise"] = {k: v for k, v in practice.items() if k != "expected_answer"}
            attach_locations(result, allowed)
            if purpose == "classroom":
                result["classroom_matches"] = classroom_cards()
            result.update(status="ready", trace=trace, evidence=list(allowed.values()),
                          prompt_version="study-v2", elapsed_seconds=round(time.monotonic() - started, 2))
            result["trace_id"] = current().id if current() else None
            result["run_id"] = store.save_run({"course": course, "question": question, **result})
            return result
    except ValueError as exc:
        store.save_run({"course": course, "status": "failed", "trace": trace, "error": str(exc)[:240]})
        if purpose == "classroom" and classroom_cards():
            return {"status":"sources_only", "reply":"已找到这些课堂原句。解释暂时没通过检查，可以先回看原文。", "claims":[], "proposals":[],
                    "uncertainties":[str(exc)[:240]], "classroom_matches":classroom_cards(), "trace":trace}
        raise
    raise ValueError(failure or "本轮没有形成可核对的结果")
