"""Cross-lesson retrieval and server-owned, quote-level playback provenance."""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlencode, urlparse

from .storage import terms
from .concepts import expand_query
from .index_state import index_status
from ..telemetry import emit

HOSTS = {"video.jw.scut.edu.cn", "video-jw-443.webvpn.scut.edu.cn"}
TOPICS = {
    "条件变量": ("condition variable", "condition variables", "pthread_cond"),
    "互斥锁": ("mutex", "mutual exclusion"), "信号量": ("semaphore", "semaphores"),
    "死锁": ("deadlock",), "生产者消费者": ("producer consumer", "producer-consumer"),
}


def is_classroom_question(question):
    return bool(re.search(r"讲过|提到过|回看|课堂原句|(?:在哪|什么时候).*讲|老师.*哪|哪一?节课|哪次课", question))


def topic_query(question, course=""):
    query = question.replace(course, "") if course else question
    query = re.sub(r"老师|请问|帮我|查一下|找一下|在哪(?:里|儿)?|什么时候|哪一?节(?:课)?|哪次(?:课)?|课堂原句|讲(?:解)?过|提到过|讲的|讲了|说的|回看|解释一下", " ", query)
    return query.strip(" \t\n?？!！。，:：\"'“”") or question


def query_variants(query):
    variants = [query]
    folded = query.casefold()
    for chinese, english in TOPICS.items():
        if chinese in query or any(x in folded for x in english):
            variants += [chinese, *english]
    return list(dict.fromkeys(variants))[:6]


def seconds(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def clock(value):
    if value is None:
        return "时间未记录"
    n = int(value)
    return f"{n//3600:02d}:{n//60%60:02d}:{n%60:02d}"


def quote_range(text, quote):
    if not quote:
        return None
    return re.search(r"\s+".join(re.escape(p) for p in quote.strip().split()), text)


def location(evidence, quote=""):
    """Never trust model-provided URLs/times, or invent video alignment for capture."""
    loc, meta = evidence.get("locator", {}), evidence.get("source_meta", {})
    match = quote_range(evidence["text"], quote)
    if quote and not match:
        raise ValueError("这段引文不在来源中")
    start, end = seconds(loc.get("start")), seconds(loc.get("end"))
    precision = "chunk"
    if match:
        spans = [s for s in loc.get("spans", []) if s["offset"] < match.end() and s["offset"] + s["length"] > match.start()]
        if spans and all(seconds(s.get("start")) is not None and seconds(s.get("end")) is not None for s in spans):
            start, end, precision = min(s["start"] for s in spans), max(s["end"] for s in spans), "segment"
    if end is not None and start is not None and end < start:
        start = end = None
    basis = loc.get("time_basis", "unknown")
    url = urlparse(meta.get("page_url") or "")
    query = parse_qs(url.query)
    playback = ""
    if (evidence.get("active", True) and not evidence.get("policy", {}).get("excluded")
        and basis == "video" and start is not None and url.scheme == "https" and url.netloc in HOSTS and url.path == "/livingroom"
        and all(re.fullmatch(r"\d+", query.get(k, [""])[0]) for k in ("course_id", "sub_id"))
        and all(not meta.get(k) or str(meta[k]) == query[k][0] for k in ("course_id", "sub_id"))):
        # Keep only routing keys, not cookies, arbitrary query values or media URLs.
        clean = {k: query[k][0] for k in ("course_id", "sub_id")}
        if re.fullmatch(r"\d+", query.get("tenant_code", [""])[0]):
            clean["tenant_code"] = query["tenant_code"][0]
        clean["note_play"] = str(int(start))
        playback = f"https://{url.netloc}/livingroom?{urlencode(clean)}"
    return {"lesson": evidence["title"], "course": evidence["course"], "session_id": loc.get("session_id", ""),
            "start": start, "end": end, "time_basis": basis, "precision": precision,
            "time_label": clock(start) + ("–" + clock(end) if end is not None and end != start else ""),
            "playback_url": playback, "kind": evidence["kind"],
            "label": "直播录音计时，尚未对齐回放" if basis == "capture" else
                     "字幕时间，回看核对" if playback else "原文位置（暂无回放定位）"}


def _excerpt(evidence, variants):
    text = evidence["text"]
    found = next((m for q in variants if (m := re.search(re.escape(q), text, re.I))), None)
    if found:
        spans = evidence.get("locator", {}).get("spans", [])
        segment = next((s for s in spans if s["offset"] <= found.start() < s["offset"] + s["length"]), None)
        start = segment["offset"] if segment else max(0, found.start() - 80)
        end = min(segment["offset"] + segment["length"], start + 480) if segment else min(len(text), found.end() + 200)
        if found.start() >= end:
            start, end = max(0, found.start() - 80), min(len(text), found.end() + 200)
        return text[start:end]
    return text[:360]


def classroom_evidence(store, course, query, limit=8):
    query = topic_query(query, course)
    expanded=expand_query(store,course,query)
    variants = list(dict.fromkeys([*query_variants(query),*expanded['variants']]))[:10]
    emit("tool.start", "查找跨课时原句", {"tool": "find_classroom_mentions", "arguments": {"course": course, "query": query}})
    pool = {}
    for variant in variants:
        for e in store.search(variant, course, 64):
            if e["kind"] in {"asr", "school_subtitle"} and e.get("locator", {}).get("session_id"):
                pool[e["id"]] = e
    ranked = []
    for e in pool.values():
        text = e["text"].casefold()
        exact = any(q.casefold() in text for q in variants)
        overlap = max(len(terms(q) & terms(text)) / max(1, len(terms(q))) for q in variants)
        if not exact and overlap < .6:
            continue
        ranked.append((int(exact) * 2 + overlap, e))
    ranked.sort(key=lambda v: (-v[0], v[1]["title"], v[1]["id"]))
    # One first match per lesson before returning extra moments from the same lesson.
    first, extra, seen_lessons, seen_moments = [], [], set(), set()
    for _, e in ranked:
        meta = e.get("source_meta", {})
        lesson = (meta.get("course_id"), meta.get("sub_id")) if meta.get("sub_id") else e["title"]
        excerpt = _excerpt(e, variants)
        position = location(e, excerpt)
        moment = (str(lesson), position["time_basis"], int((position["start"] or 0) // 30))
        if moment in seen_moments:
            continue
        seen_moments.add(moment)
        (extra if lesson in seen_lessons else first).append(e)
        seen_lessons.add(lesson)
    selected = (first + extra)[:max(1, min(limit, 12))]
    emit("tool.finish", "跨课时检索完成", {"tool": "find_classroom_mentions", "matches": len(ranked),
         "lessons": len(seen_lessons), "selected": [e["id"] for e in selected]})
    return selected


def mention_cards(evidence, query, variants=None):
    variants = variants or query_variants(query)
    return [{"evidence_id": e["id"], "quote": (quote := _excerpt(e, variants)), **location(e, quote)} for e in evidence]


def find_classroom_mentions(store, course, query, limit=8):
    query = topic_query(query, course)
    evidence = classroom_evidence(store, course, query, limit)
    terminology=expand_query(store,course,query)
    variants=list(dict.fromkeys([*query_variants(query),*terminology['variants']]))[:10]
    return {"course": course, "query": query, "items": mention_cards(evidence, query, variants), "terminology":terminology,
            "classroom_index":index_status(store,course),
            "notice": "原句来自字幕，可能含识别错误；定位只覆盖已导入的课堂记录。"}


def attach_locations(result, allowed):
    for item in [*result.get("claims", []), *result.get("connections", [])]:
        for cite in item.get("citations", []):
            e = allowed.get(cite["evidence_id"])
            if e and e.get("locator", {}).get("session_id"):
                cite["location"] = location(e, cite["quote"])
