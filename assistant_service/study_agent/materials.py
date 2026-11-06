"""Shared material identity for uploads, local files and lecture attachments."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .storage import digest
from .knowledge import source_policy, set_policy


# All material entry points in this service share the lookup/write boundary.
material_lock = threading.RLock()


def matching_material(store, course, fingerprint, chunks):
    fingerprints = {fingerprint, digest([fingerprint, chunks])}
    for source in store.sources(course):
        if source["kind"] == "material" and (source["fingerprint"] in fingerprints
                or source.get("meta", {}).get("content_fingerprint") == fingerprint):
            return source
    return None


def material_choices(store, course, attachment):
    duplicate = matching_material(store, course, attachment["fingerprint"], attachment["chunks"])
    name = attachment["name"].casefold()
    conflicts = [s for s in store.sources(course) if s["kind"] == "material"
                 and s.get("meta", {}).get("original_name", s["title"]).casefold() == name
                 and (not duplicate or s["id"] != duplicate["id"])]
    return {"attachment_id": attachment["id"], "name": attachment["name"],
            "duplicate_id": duplicate["id"] if duplicate else None,
            "conflicts": [{"id": s["id"], "title": s["title"]} for s in conflicts]}


def replacement_target(store, course, fingerprint, chunks, source_id):
    if not source_id:
        return None
    duplicate = matching_material(store, course, fingerprint, chunks)
    if duplicate:
        if duplicate["id"] == source_id or duplicate.get("meta", {}).get("replaces_source_id") == source_id:
            return duplicate  # A repeated submission of the same replacement.
        raise ValueError("这份资料已存在，无需替换；请保留已有资料")
    target = next((s for s in store.sources(course) if s["id"] == source_id and s["kind"] == "material"), None)
    if target is None:
        raise ValueError("要替换的课件已更新或不属于本课程，请重新选择")
    return target


def register_material(store, *, course, title, fingerprint, chunks, origin=None, attachment_id=None, replace_source_id=None):
    with material_lock:
        target = replacement_target(store, course, fingerprint, chunks, replace_source_id)
        duplicate = matching_material(store, course, fingerprint, chunks)
        if duplicate:
            # Preserve legacy source IDs and citations while teaching them the raw file identity.
            meta = {**duplicate.get("meta", {}), "content_fingerprint": fingerprint}
            if attachment_id:
                meta.setdefault("attachment_id", attachment_id)
            with store.connect() as db:
                db.execute("UPDATE sources SET data=? WHERE id=?", (json.dumps({**duplicate, "meta": meta}, ensure_ascii=False), duplicate["id"]))
            return {"id": duplicate["id"], "duplicate": True, "chunks": 0}
        original_name = title
        origin = target["origin"] if target else origin or "attachment:" + (attachment_id or fingerprint)
        meta = {"content_fingerprint": fingerprint, "original_name": original_name, "verification": "original_text"}
        if attachment_id:
            meta["attachment_id"] = attachment_id
        if target:
            title = target["title"]
            meta.update(original_name=target.get("meta", {}).get("original_name", title), replaces_source_id=target["id"])
        else:
            names = {s["title"].casefold() for s in store.sources(course) if s["kind"] == "material" and s["origin"] != origin}
            stem, suffix, number = Path(title).stem, Path(title).suffix, 2
            while title.casefold() in names:
                title = f"{stem}（{number}）{suffix}"
                number += 1
        return store.source(origin=origin, course=course, title=title, kind="material",
                            fingerprint=fingerprint, chunks=chunks, meta=meta)


def mark_as_slides(store, source_id):
    source = next(s for s in store.sources() if s["id"] == source_id)
    policy = source_policy(store, source)
    return set_policy(store, source_id, role="slides", excluded=policy["excluded"], trusted=policy["trusted"])
