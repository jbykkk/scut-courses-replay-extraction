"""Course-note outlines and separately saved, revision-checked Markmap edits."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


MAX_MARKDOWN = 200_000


class MindmapEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    markdown: str = Field(min_length=1, max_length=MAX_MARKDOWN)
    revision: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def label(value, limit=160):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return re.sub(r"([\\`*_{}\[\]<>()!#+|])", r"\\\1", text)


def points(detail):
    """Keep nodes concise; the linked original always retains the complete notes."""
    candidates, fenced = [], False
    for line in str(detail or "").splitlines():
        if re.match(r"\s*(`{3,}|~{3,})", line):
            fenced = not fenced
            continue
        if fenced or not line.strip():
            continue
        match = re.match(r"\s*(?:#{1,6}\s+|[-+*]\s+|\d+[.)]\s+)(.+)", line)
        text = match.group(1) if match else line.strip()
        text = re.sub(r"\*\*|__|`", "", text)
        candidates.append(label(text))
    return list(dict.fromkeys(candidates))[:6]


def outline(session):
    summary = session.get("summary") or {}
    topics = summary.get("topics") or []
    lines = ["# " + label(summary.get("headline") or session.get("title") or session.get("course_title") or "课程笔记"), ""]
    sources, included = {}, set()
    speech = {s["id"]: s for s in session.get("segments", [])}

    def topic(index, depth):
        item = topics[index]
        # Links are content-addressed: an old outline can never point at a different
        # topic merely because a new note was inserted before it.
        key = fingerprint({k: item.get(k) for k in ("title", "start", "end", "segment_ids", "material_refs")})[:20]
        sources[key] = index
        target = "#lecture-topic-" + key
        lines.extend(["#" * depth + " [" + label(item.get("title") or "知识点") + "](" + target + ")", ""])
        lines.extend("- " + text for text in points(item.get("detail")))
        cited = [speech[sid] for sid in item.get("segment_ids", []) if sid in speech]
        if cited:
            start = max(0, int(min(s.get("start", 0) for s in cited)))
            stamp = f"{start // 3600:02}:{start // 60 % 60:02}:{start % 60:02}"
            kind = "录音原话" if session.get("time_basis") == "capture" else "课堂原话"
            lines.append(f"- [{kind} {stamp}]({target})")
        for ref in (item.get("material_refs") or [])[:3]:
            loc = ref.get("locator") or {}
            position = f" · 第 {loc['page']} 页" if loc.get("page") else f" · 第 {loc['slide']} 张" if loc.get("slide") else ""
            lines.append("- [课件：" + label(str(ref.get("title") or "课件") + position) + "](" + target + ")")
        lines.append("")
        included.add(index)

    for group in summary.get("groups") or []:
        indices = list(dict.fromkeys(i for i in group.get("topic_indices", [])
                                    if type(i) is int and 0 <= i < len(topics) and i not in included))
        if not indices:
            continue
        lines.extend(["## " + label(group.get("title") or "知识点分组"), ""])
        for index in indices:
            topic(index, 3)
    remaining = [i for i in range(len(topics)) if i not in included]
    if remaining and included:
        lines.extend(["## 其他知识点", ""])
    grouped = bool(included)
    for index in remaining:
        topic(index, 3 if grouped else 2)
    if not topics:
        lines.extend("- " + text for text in points(summary.get("abstract") or summary.get("overview")))
    return "\n".join(lines).rstrip() + "\n", sources


def view(session):
    generated, sources = outline(session)
    summary = session.get("summary") or {}
    source_hash = fingerprint(summary)
    saved = session.get("mindmap") or {}
    custom = isinstance(saved.get("markdown"), str) and bool(saved["markdown"].strip())
    return {
        "markdown": saved["markdown"] if custom else generated,
        "generated_markdown": generated,
        "revision": saved.get("revision", ""),
        "source_hash": saved.get("source_hash", source_hash) if custom else source_hash,
        "current_source_hash": source_hash,
        "custom": custom,
        "stale": custom and saved.get("source_hash") != source_hash,
        "partial": bool(summary.get("partial")),
        "available": bool(summary.get("topics") or summary.get("abstract") or summary.get("overview")),
        "sources": sources,
        "updated_at": saved.get("updated_at"),
    }


def router(store):
    api = APIRouter(prefix="/api/sessions")

    def session(sid):
        value = store.get(sid)
        if value.get("deleted_at"):
            raise HTTPException(404, "请先从回收站恢复这份课程笔记")
        return value

    @api.get("/{sid}/mindmap")
    def get_mindmap(sid: str):
        return view(session(sid))

    @api.post("/{sid}/mindmap")
    def save_mindmap(sid: str, body: MindmapEdit):
        if not body.markdown.strip():
            raise ValueError("提纲不能为空，请至少保留一个标题或知识点")
        if len(body.markdown.splitlines()) > 4000 or re.search(r"(?m)^[ \t]{61}", body.markdown):
            raise ValueError("提纲层级或行数过多，请拆分为较小的导图")
        with store.lock:
            value = session(sid)
            saved = value.get("mindmap") or {}
            if body.revision != saved.get("revision", ""):
                raise HTTPException(409, "另一窗口已保存此导图；请先导出当前提纲，再刷新页面合并修改")
            current_hash = fingerprint(value.get("summary") or {})
            if body.source_hash not in {current_hash, saved.get("source_hash")}:
                raise HTTPException(409, "课程笔记已更新；请先导出当前提纲，再从最新笔记重建导图")
            value = store.update(sid, mindmap={
                "markdown": body.markdown, "revision": uuid.uuid4().hex,
                "source_hash": body.source_hash, "updated_at": time.time(),
            })
        return view(value)

    return api
