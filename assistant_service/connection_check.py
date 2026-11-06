from __future__ import annotations

import hashlib
import time

import httpx

from .settings import MODEL_ALIASES


def identity(settings):
    llm = settings.llm()
    return hashlib.sha256((llm["key"] + "\0" + llm["provider"] + "\0" + llm["model"]).encode()).hexdigest()


def check(settings, transport=None):
    llm = settings.llm()
    label = llm["label"]
    if not llm["key"]:
        return {"ok": False, "message": "尚未保存 Key", "checked_at": time.time()}
    try:
        # Overseas endpoints honor system/env proxies; domestic DeepSeek stays direct.
        with httpx.Client(timeout=12, trust_env=llm["provider"] == "openai", transport=transport, follow_redirects=False) as client:
            response = client.get(llm["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + llm["key"]})
        if response.status_code != 200:
            message = {401: "Key 认证失败，请替换密钥", 403: "账户没有访问权限", 429: "请求频率受限，请稍后重试"}.get(response.status_code, f"{label} HTTP {response.status_code}，请稍后重试")
            return {"ok": False, "message": message, "checked_at": time.time()}
        selected = MODEL_ALIASES.get(llm["model"], llm["model"])
        available = selected in {m["id"] for m in response.json()["data"]}
        return {"ok": available, "message": "Key 认证通过，当前模型可用" if available else "Key 认证通过，但当前模型不在可用列表中", "checked_at": time.time()}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {"ok": False, "message": "无法完成连接检测，请检查网络后重试", "checked_at": time.time()}
