"""Evidence policy, curated memory and tentative lecture/document alignment.

Original text is never corrected in place. Retrieval eligibility belongs to an
origin so a reimport cannot silently undo a student's exclusion decision.
"""
from __future__ import annotations

import re
import time

from .storage import digest, terms

ROLES = {"reference", "exercise", "slides", "transcript", "external", "other"}
NOISY = {"asr", "school_subtitle", "image_reading", "student_report"}


def policy_id(source):
    return "policy-" + digest([source["course"], source["origin"]])[:32]


def source_policy(store, source):
    try:
        return store.get(policy_id(source), "source_policy")
    except KeyError:
        return {"role": "transcript" if source["kind"] in NOISY else "external" if source["kind"] == "external" else "other",
                "excluded": False, "trusted": False}


def catalog(store, course=""):
    return [{**source, "policy": source_policy(store, source)} for source in store.sources(course)]


def set_policy(store, sid, *, role, excluded=False, trusted=False):
    if role not in ROLES:
        raise ValueError("未知资料类型")
    source = next((s for s in store.sources() if s["id"] == sid), None)
    if not source:
        raise ValueError("资料已更新，请刷新后选择当前版本")
    if trusted and source["kind"] in NOISY:
        raise ValueError("转写和识图只能作为待核对记录，不能直接升级为参考资料")
    return store.save("source_policy", {"source_id": sid, "course": source["course"], "origin": source["origin"],
        "role": role, "excluded": excluded, "trusted": trusted}, policy_id(source))


def decorate_evidence(store, item, *, source=None, policies=None):
    if source is None:
        with store.connect() as db:
            row = db.execute("SELECT data FROM sources WHERE id=?", (item["source_id"],)).fetchone()
        if not row:return item
        import json
        source = json.loads(row[0])
    policy = source_policy(store, source) if policies is None else policies.get((source["course"],source["origin"]), {"excluded":False,"trusted":False})
    return {**item, "policy": policy, "origin": source["origin"], "source_meta": source.get("meta", {}),
            "reliability": "transcription_unchecked" if item["kind"] in NOISY else
                           "selected_reference" if policy["trusted"] else "original_unchecked"}


def annotate_claims(store, result, allowed):
    for claim in [*result.get("claims", []), *result.get("connections", [])]:
        evidence = [allowed[c["evidence_id"]] for c in claim["citations"]]
        from .research import source_family
        independent = {source_family(e) for e in evidence}
        reference = any(e.get("policy", {}).get("trusted") and e["kind"] not in NOISY for e in evidence)
        # These labels describe provenance, NOT a proof that the proposition is true.
        level = "needs_check" if all(e["kind"] in NOISY for e in evidence) else "reference_backed" if reference else "source_backed"
        claim["verification"] = {"status": level, "source_count": len(independent),
            "label": "仅有转写/识读依据，待核对" if level == "needs_check" else
                     "引用了指定参考资料，仍需检查结论" if reference else "有原文依据，尚未独立核验"}
    if any(c.get("verification", {}).get("status") == "needs_check" for c in result.get("claims", [])):
        result.setdefault("uncertainties", []).append("这部分结论只有转写或识图支持，暂不计入已核对知识。可加入教材、题目原文件或外部参考再核对。")
    return result


def remember_claim(store, *, course, text, citations, status="pending", supersedes=None):
    if status not in {"pending", "confirmed", "dismissed"}:
        raise ValueError("未知知识状态")
    if not citations:
        raise ValueError("知识条目需要保留来源")
    evidence = [store.evidence(c["evidence_id"]) for c in citations]
    if any(e["course"] != course or not e["active"] or e.get("policy", {}).get("excluded") for e in evidence):
        raise ValueError("来源不属于本课、已失效或已停用")
    if status == "confirmed" and all(e["kind"] in NOISY for e in evidence):
        raise ValueError("还缺独立资料依据；原话会保留，但不能记成已核对知识")
    mid = digest(["knowledge", course, text, citations])[:32]
    return store.save("knowledge", {"course": course, "text": text, "citations": citations,
        "status": status, "supersedes": supersedes, "confirmed_at": time.time() if status == "confirmed" else None}, mid)


def relevant_memory(store, course, question, limit=5):
    wanted = terms(question)
    values = []
    memories = store.list("knowledge")
    superseded = {m.get("supersedes") for m in memories if m.get("status") == "confirmed"}
    for memory in memories:
        if memory.get("course") != course or memory.get("status") != "confirmed" or memory["id"] in superseded:
            continue
        score = len(wanted & terms(memory["text"]))
        if not score:continue
        try:
            evidence = [store.evidence(c["evidence_id"]) for c in memory["citations"]]
            if not all(e["active"] and not e.get("policy", {}).get("excluded") for e in evidence):
                continue
        except KeyError:
            continue
        if score:
            values.append((score, {"text": memory["text"], "citations": memory["citations"]}))
    return [m for _, m in sorted(values, key=lambda x: -x[0])[:limit]]


def align_class(store, course, query, *, limit=4):
    """Candidate links, never silently assert that the lecturer discussed a file."""
    found = store.search(query, course, 30)
    spoken = [x for x in found if x["kind"] in {"asr", "school_subtitle"}]
    written = [x for x in found if x["kind"] not in NOISY]
    links = []
    for speech in spoken[:limit]:
        candidates = []
        speech_terms = terms(speech["text"])
        for item in written:
            overlap = speech_terms & terms(item["text"])
            # A problem number alone never establishes alignment.
            substantive = [x for x in overlap if not re.fullmatch(r"\d+", x)]
            if len(substantive) >= 3:
                candidates.append((len(substantive), item))
        if not candidates:
            continue
        score, material = max(candidates, key=lambda x: x[0])
        source = next(s for s in store.sources(course) if s["id"] == speech["source_id"])
        nearby = []
        start, end = speech["locator"].get("start", 0), speech["locator"].get("end", 0)
        for visual in store.list("visual"):
            if visual.get("course") != course or speech["locator"].get("time_basis") == "capture":
                continue
            from urllib.parse import urlparse, parse_qs
            ids = parse_qs(urlparse(visual.get("course_url", "")).query)
            if ids.get("sub_id", [None])[0] != source.get("meta", {}).get("sub_id"):
                continue
            shots = sorted(visual.get("items", []), key=lambda x: x["seconds"])
            prior = [x for x in shots if x["seconds"] <= start]
            if prior:
                nearby.append(prior[-1])
            nearby += [x for x in shots if start < x["seconds"] <= end][:2]
        value = {"course": course, "speech": speech, "material": material, "visuals": nearby[:3],
                 "status": "candidate", "reason": f"同课资料有 {score} 个共同词项；请核对题号、符号和条件"}
        links.append(value)
    return links


def read_aligned_visuals(store, provider, root, links, question):
    """At most two original, validated school screenshots per reconstruction."""
    import base64
    import httpx
    from .attachments import accept_attachment, prepare_attachments
    urls = list(dict.fromkeys(v["url"] for link in links for v in link["visuals"]))[:2]
    if not urls: return [], []
    if not provider.public().get("vision"):
        return [], ["当前模型不支持图片，本次只对照字幕与文档；截图保留原图链接。"]
    ids, warnings = [], []
    for index, url in enumerate(urls):
        if not re.fullmatch(r"https://video\.jw\.scut\.edu\.cn/play/ai\d+/ppt/[\w/-]+\.jpe?g", url): continue
        try:
            with httpx.Client(timeout=15, follow_redirects=False, trust_env=True) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status(); data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > 8 * 1024 * 1024: raise ValueError("截图过大")
            item = accept_attachment(store, root, name=f"课堂屏幕-{index+1}.jpg", content=base64.b64encode(data).decode())
            ids.append(item["id"])
        except (httpx.HTTPError, ValueError, OSError):
            warnings.append("有一张课堂截图暂时无法读取，没有用猜测内容补齐。")
    if not ids: return [], warnings
    try:
        evidence, _ = prepare_attachments(store, provider, root, ids, links[0]["course"], question)
        return evidence, warnings + ["截图已实际识读；题号、数字和公式仍须对照原图确认。"]
    except ValueError as exc:
        return [], warnings + [str(exc)]
