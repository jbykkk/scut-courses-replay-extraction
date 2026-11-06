"""Bounded, course-scoped PDF evidence for lecture notes."""
from __future__ import annotations

import re
import json
from pathlib import Path

from .study_agent.knowledge import source_policy
from .study_agent.storage import terms


def available_materials(study, course):
    if not course:
        return []
    return [s for s in study.sources(course) if s["kind"] == "material"
            and Path(s["title"]).suffix.lower() in {".pdf", ".pptx"}
            and not source_policy(study, s).get("excluded")]


def selected_materials(study, session):
    sources = available_materials(study, session.get("course_title", ""))
    selected = session.get("note_source_ids")
    if selected is not None:
        origins = set()
        if selected:
            with study.connect() as db:
                rows = db.execute("SELECT data FROM sources WHERE course=? AND id IN (" + ",".join("?" for _ in selected) + ")",
                                  (session.get("course_title", ""), *selected)).fetchall()
            origins = {json.loads(row[0])["origin"] for row in rows}
        sources = [s for s in sources if s["id"] in selected or s["origin"] in origins]
    else:
        # Reference books and exercise sheets are not automatically treated as slides.
        sources = [s for s in sources if source_policy(study, s).get("role") not in {"reference", "exercise"}]
    return sources


def material_context(study, sources, segments):
    text = "\n".join(s["text"] for s in segments)
    # Sample the entire pack so the first minute cannot monopolize retrieval.
    query = "\n".join(text[i:i + 600] for i in range(0, len(text), max(600, len(text) // 4)))[:2400]
    wanted = terms(query)
    candidates = []
    for source in sources:
        for item in study.search(query, source["course"], 4, source_id=source["id"]):
            overlap = [t for t in wanted & terms(item["text"]) if not re.fullmatch(r"\d+", t)]
            if len(overlap) < 3:
                continue
            candidates.append((len(overlap), item))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [{k: item[k] for k in ("id", "source_id", "title", "text", "locator")}
            for _, item in candidates[:10]]
