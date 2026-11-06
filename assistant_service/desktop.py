"""On-demand school handoff. No passwords, cookies or arbitrary launch commands."""
from __future__ import annotations

import threading
import time
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from config import parse_course_url
from .settings import ROOT, atomic_json

SCHOOL = "https://video.jw.scut.edu.cn/"
VPN = "https://video-jw-443.webvpn.scut.edu.cn/"


def school_url(value: str, external=False):
    """Reconstruct only supported school URLs; never persist redirect/token parameters."""
    parsed = parse_course_url(value)
    if not parsed.course_id:
        raise ValueError("课程链接缺少 course_id")
    origin = VPN.rstrip("/") if external else parsed.origin
    return origin + "/livingroom?" + urlencode({"course_id": parsed.course_id,
        "sub_id": parsed.sub_id, "tenant_code": parsed.tenant_code})


class Connect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course: str = Field(default="", max_length=200)
    external: bool = False


class BrowserHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    version: str = Field(max_length=30)
    courses: list[str] = Field(default_factory=list, max_length=30)
    school_tabs: int = Field(default=0, ge=0, le=10000)
    unreadable_tabs: int = Field(default=0, ge=0, le=10000)


class BrowserClaim(BaseModel):
    client: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


def router(settings, classroom):
    api = APIRouter(prefix="/api/desktop")
    lock = threading.RLock()
    # Heartbeats are ephemeral: restarting must not pretend a browser is connected.
    clients = {}
    path = settings.root / "school-handoff.json"
    import json
    try:
        requests = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        requests = []

    def live():
        return [c for c in clients.values() if time.time() - c["seen"] < 75]

    @api.post("/welcomed")
    def welcomed():
        (settings.root / "desktop-welcomed").touch()
        return {"ok": True}

    @api.get("/status")
    def status():
        with lock:
            online = live()
            courses = sorted({course for c in online for course in c["courses"]})
            return {"extension_connected": bool(online), "readable_courses": courses,
                "school_state": "readable" if courses else "needs_login_or_course" if any(c["school_tabs"] for c in online) else "closed",
                "pending": [r for r in requests if time.time()-r["created"] < 86400 and r["course"] not in courses][-10:],
                "extension_path": str(ROOT / "extension"), "school_url": SCHOOL, "vpn_url": VPN,
                "key_saved": settings.public()["llm_configured"], "version": "0.14.0"}

    @api.post("/school")
    def connect(body: Connect):
        with lock:
            url = VPN if body.external else SCHOOL
            for session in classroom.list():
                if body.course and session.get("course_title") == body.course:
                    try:
                        url = school_url(session["page_url"], body.external)
                        break
                    except (KeyError, ValueError):
                        pass
            existing = next((r for r in reversed(requests) if r["course"] == body.course and
                r["url"] == url and time.time()-r["created"] < 15), None)
            if existing:
                return {**existing, "via_extension": bool(live())}
            item = {"id": uuid.uuid4().hex, "course": body.course, "url": url,
                "created": time.time(), "opened": not bool(live())}
            requests[:] = [r for r in requests if time.time()-r["created"] < 86400][-29:]+[item]
            atomic_json(path, requests)
            return {**item, "via_extension": bool(live())}

    @api.post("/heartbeat")
    def heartbeat(body: BrowserHeartbeat):
        with lock:
            # Bound state even if browser installations change repeatedly.
            for key in [k for k,v in clients.items() if time.time()-v["seen"] > 75]:
                clients.pop(key)
            clients[body.client] = {**body.model_dump(), "seen": time.time()}
            return {"ok": True}

    @api.get("/browser-requests")
    def browser_requests():
        with lock:
            return [r for r in requests if not r["opened"] and time.time()-r["created"] < 120]

    @api.post("/browser-requests/{rid}/claim")
    def claim(rid: str, body: BrowserClaim):
        with lock:
            item = next((r for r in requests if r["id"] == rid), None)
            if not item or item["opened"] or time.time()-item["created"] >= 120:
                raise HTTPException(409, "学校页面已交给浏览器打开")
            item["opened"] = True
            atomic_json(path, requests)
            return item

    return api
