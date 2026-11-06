from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.request import getproxies

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .settings import atomic_json
from .study_agent.reviews import ReviewScheduler

APP_ID = "scut-classroom"
RELAY_URL = "https://ironegg.vercel.app"
PAIRING_PREFIX = "SCUT1."
ALERT_CATEGORIES = {"attendance", "qr", "question", "assignment", "quiz", "requirements", "schedule", "grading", "reminder"}


def relay_proxy() -> str | None:
    # httpx does not read Windows Internet Settings. Respect the user's HTTPS
    # proxy for the remote relay only; localhost requests remain direct.
    return getproxies().get("https") if os.name == "nt" else None


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_pairing() -> dict:
    return {"relay_url": RELAY_URL, "app_id": APP_ID, "channel": b64(secrets.token_bytes(16)),
            "auth_token": b64(secrets.token_bytes(32)), "e2e_key": b64(secrets.token_bytes(32)),
            "enabled_at": time.time()}


def pairing_code(pairing: dict) -> str:
    payload = {"u": pairing["relay_url"], "i": pairing["app_id"], "c": pairing["channel"],
               "a": pairing["auth_token"], "k": pairing["e2e_key"]}
    return PAIRING_PREFIX + b64(json.dumps(payload, separators=(",", ":")).encode())


def registry_entry(pairing: dict) -> dict:
    return {"channel": pairing["channel"],
            "authSha256": hashlib.sha256(pairing["auth_token"].encode()).hexdigest(),
            "allowedOrigins": []}


def seal(pairing: dict, kind: str, data: dict, *, message_id: str | None = None, now: float | None = None) -> tuple[str, str]:
    message_id = message_id or b64(secrets.token_bytes(16))
    direction = "to-phone"
    salt = ("dsh-remote:" + pairing["channel"]).encode()
    info = ("dsh-remote/v2:" + direction).encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(unb64(pairing["e2e_key"]))
    envelope = {"v": 2, "id": message_id, "k": kind, "ts": int((now or time.time()) * 1000), "d": data}
    iv = secrets.token_bytes(12)
    aad = f"dsh-remote/v2|{pairing['channel']}|{direction}|{message_id}".encode()
    ciphertext = AESGCM(key).encrypt(iv, json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(), aad)
    return message_id, f"v2.{message_id}.{b64(iv)}.{b64(ciphertext)}"


class MobileNotifier:
    def __init__(self, settings, store, *, transport=None):
        self.settings, self.store, self.transport = settings, store, transport
        self.path = settings.root / "mobile-notifications.json"
        self.stop = threading.Event()
        self.thread = None
        self.last_error = ""
        self.last_success = 0.0
        self.state = self._load()
        self.study_reviews = ReviewScheduler(settings.root)

    def _load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (ValueError, OSError):
            value = {}
        return {"version": 1, "pairing": value.get("pairing", ""),
                "sent": dict(list((value.get("sent") or {}).items())[-2000:]),
                "outbox": list(value.get("outbox") or [])[-1000:]}

    def _save(self):
        atomic_json(self.path, self.state)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="scut-mobile-notifier")
        self.thread.start()

    def close(self):
        self.stop.set()

    def status(self):
        configuration_error = ""
        try:
            configured = bool(self.settings.mobile_pairing())
            if not configured and self.settings.data.get("mobile_notifications_enabled"):
                configuration_error = "未找到手机配对信息，请检查提醒设置"
        except ValueError as exc:
            configured = False
            configuration_error = str(exc)
        return {"enabled": self.settings.data.get("mobile_notifications_enabled", False),
                "configured": configured, "pending": len(self.state["outbox"]),
                "last_success": self.last_success, "last_error": configuration_error or self.last_error}

    def _identity(self, pairing):
        return hashlib.sha256((pairing["app_id"] + ":" + pairing["channel"]).encode()).hexdigest()

    def queue(self, kind: str, data: dict, dedupe: str):
        pairing = self.settings.mobile_pairing()
        if not pairing or not self.settings.data.get("mobile_notifications_enabled") or dedupe in self.state["sent"]:
            return False
        identity = self._identity(pairing)
        if self.state["pairing"] != identity:
            self.state = {"version": 1, "pairing": identity, "sent": {}, "outbox": []}
        message_id, wire = seal(pairing, kind, data)
        item = {"id": message_id, "wire": wire}
        if data.get("source") == "study-review":
            item["study_alert_id"] = data["id"]
        self.state["outbox"].append(item)
        self.state["sent"][dedupe] = int(time.time())
        self.state["sent"] = dict(list(self.state["sent"].items())[-2000:])
        self._save()
        return True

    def scan(self):
        # Same background lane as the phone outbox: no competing JSON writers.
        # Local review inbox still advances when mobile delivery is disabled.
        try:
            self.study_reviews.tick(self)
            self.study_reviews.last_error = ""
        except Exception as exc:
            self.study_reviews.last_error = type(exc).__name__
        pairing = self.settings.mobile_pairing()
        if not pairing or not self.settings.data.get("mobile_notifications_enabled"):
            return
        for session in self.store.list():
            cutoff = pairing["enabled_at"] - 300
            active = session.get("status") in {"recording", "stopping"}
            recent = session.get("created_at", 0) >= cutoff
            if session.get("mode") != "live" or session.get("deleted_at") or (not active and not recent):
                continue
            for event in session.get("events", []):
                if event.get("source") != "deepseek" or event.get("category") not in ALERT_CATEGORIES:
                    continue
                # Pairing may happen halfway through a lecture. Keep listening to that
                # active session, but do not flood the phone with its earlier events.
                if active and not recent:
                    base = session.get("start_at") or session.get("created_at")
                    start = event.get("start")
                    if not isinstance(base, (int, float)) or not isinstance(start, (int, float)) or base + start < cutoff:
                        continue
                content = json.dumps([event.get("message"), event.get("details")], ensure_ascii=False, sort_keys=True)
                dedupe = f"{session['id']}:{event['id']}:{hashlib.sha256(content.encode()).hexdigest()[:12]}"
                payload = {"course": session.get("course_title", "当前课程"),
                    "lesson": session.get("title", ""), "session_id": session["id"],
                    **{k: event.get(k) for k in ("id", "category", "label", "priority", "message", "details", "evidence", "start", "confidence")}}
                base, start = session.get("start_at"), event.get("start")
                if isinstance(base, (int, float)) and isinstance(start, (int, float)):
                    payload["occurred_at"] = base + start
                self.queue("classroom-alert", payload, dedupe)

    def flush(self):
        pairing = self.settings.mobile_pairing()
        if not pairing or not self.state["outbox"]:
            return
        item = None
        for candidate in list(self.state["outbox"]):
            decision = self.study_reviews.delivery_state(candidate["study_alert_id"]) if candidate.get("study_alert_id") else "send"
            if decision == "discard":
                self.state["outbox"].remove(candidate)
                self._save()
            elif decision == "send":
                item = candidate
                break
        if item is None:
            return False  # Quiet reviews must not block later urgent classroom alerts.
        url = f"{pairing['relay_url']}/api/apps/{pairing['app_id']}/push"
        with httpx.Client(timeout=15, transport=self.transport, trust_env=False,
                          proxy=relay_proxy() if self.transport is None else None) as client:
            response = client.post(url, headers={"Authorization": "Bearer " + pairing["auth_token"]}, json={
                "channel": pairing["channel"], "direction": "to-phone", "id": item["id"], "wire": item["wire"]})
        if response.status_code != 201 or response.json().get("ok") is not True:
            raise ValueError(f"手机中继 HTTP {response.status_code}")
        self.state["outbox"].remove(item)
        if item.get("study_alert_id"):
            self.study_reviews.delivered(item["study_alert_id"])
        self._save()
        self.last_success, self.last_error = time.time(), ""
        return True

    def test(self):
        stamp = int(time.time())
        self.queue("classroom-alert", {"course": "课堂助手", "lesson": "手机连接测试", "category": "reminder",
            "label": "连接测试", "priority": "urgent", "message": "手机提醒连接成功。之后点名、扫码、作业和测验会显示在这里。",
            "details": {}, "start": 0, "confidence": 1}, f"test:{stamp}")
        self.flush()

    def _loop(self):
        failures = 0
        while not self.stop.is_set():
            try:
                self.scan()
                while self.state["outbox"] and not self.stop.is_set():
                    if self.flush() is False:
                        break
                failures = 0
            except Exception as exc:
                self.last_error = str(exc)[:160]
                failures += 1
            self.stop.wait(min(60, 2 * 2 ** min(failures, 5)))
