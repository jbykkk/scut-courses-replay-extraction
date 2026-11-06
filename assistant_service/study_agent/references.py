"""Bounded external reference reads. Never send classroom text to a search site."""
from __future__ import annotations

import html
import json
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urlparse, quote
from urllib.request import getproxies

import httpx

from .ingest import split_text
from .storage import digest


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer"}: self.hidden += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer"}: self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "h1", "h2", "li"}: self.parts.append("\n")
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


FIXED_REFERENCE_HOSTS = {"openstax.org", "www.ncbi.nlm.nih.gov", "www.nist.gov", "docs.python.org",
                         "en.wikipedia.org", "zh.wikipedia.org", "pages.cs.wisc.edu", "api.crossref.org", "www.ebi.ac.uk"}


def reference_proxy(url, root=None):
    # Some local VPN clients resolve public hosts to 198.18/15 and perform real
    # resolution at the HTTP proxy. Use only the configured loopback proxy and
    # fixed public publishers; TLS hostname validation remains enabled.
    if urlparse(url).hostname not in FIXED_REFERENCE_HOSTS: return None
    proxy = getproxies().get("https") or getproxies().get("all")
    if root is not None and (root / "study-network.json").is_file():
        proxy = json.loads((root / "study-network.json").read_text(encoding="utf-8")).get("reference_proxy") or proxy
    if proxy:
        p = urlparse(proxy)
        if p.scheme in {"http", "https"} and p.hostname in {"127.0.0.1", "::1", "localhost"}:
            return proxy
        raise ValueError("外部参考代理只接受本机 HTTP / HTTPS 代理地址")
    return None


def public_reference_url(url, proxy=None):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {443, None}:
        raise ValueError("参考链接须为公开 HTTPS 页面")
    if parsed.query or parsed.fragment or len(url) > 1600:
        raise ValueError("请提供不含查询口令和片段的公开资料链接")
    approved = host in FIXED_REFERENCE_HOSTS or host.endswith((".edu", ".edu.cn", ".ac.uk", ".gov"))
    if not approved:
        raise ValueError("暂只读取高校、政府、OpenStax、NCBI、Wikipedia 和官方 Python 文档。其他资料可下载后导入。")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    def routable(value):
        address=ipaddress.ip_address(value)
        # TUN-mode VPNs also use benchmark aliases without an HTTP proxy. Only
        # these fixed publishers may use that range; HTTPS still verifies the
        # exact public hostname, and no redirects/private/LAN IPs are accepted.
        benchmark = address in ipaddress.ip_network("198.18.0.0/15") if address.version == 4 else address in ipaddress.ip_network("2001:2::/48")
        return address.is_global or (host in FIXED_REFERENCE_HOSTS and benchmark)
    if not addresses or any(not routable(x[4][0]) for x in addresses):
        raise ValueError("参考地址必须指向公网")
    return url


def read_reference(store, course, url, client=None):
    proxy = reference_proxy(url, store.path.parent) if client is None else None
    public_reference_url(url, proxy=proxy)
    own = client is None
    client = client or httpx.Client(timeout=18, follow_redirects=False, trust_env=False, proxy=proxy,
        headers={"User-Agent": "SCUTStudy/0.11 (local study reference reader)"})
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("资料链接发生跳转，请使用最终公开地址")
            buf = bytearray(); started = time.monotonic()
            for data in response.iter_bytes():
                buf.extend(data)
                if len(buf) > 6 * 1024 * 1024:
                    raise ValueError("外部资料超过 6 MB，请下载后从本地导入")
                if time.monotonic() - started > 20:
                    raise ValueError("读取资料超时，请稍后重试")
            ctype = response.headers.get("content-type", "")
        if "pdf" in ctype or urlparse(url).path.endswith(".pdf"):
            import io
            from pypdf import PdfReader
            pages = PdfReader(io.BytesIO(buf)).pages
            chunks = []
            for n, page in enumerate(pages[:30], 1):
                chunks += split_text((page.extract_text() or "")[:10000], {"page": n, "url": url})
            title = urlparse(url).path.rsplit("/", 1)[-1]
        elif any(t in ctype for t in ("html", "text/plain")):
            raw = bytes(buf).decode("utf-8", errors="replace")
            parser = PlainText(); parser.feed(raw)
            text = re.sub(r"[ \t]+", " ", " ".join(parser.parts)).strip()[:24000]
            title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
            title = html.unescape(title_match[1]).strip()[:180] if title_match else urlparse(url).path
            chunks = split_text(text, {"url": url})
        else:
            raise ValueError("此地址不是可读取的网页或 PDF")
        if not chunks:
            raise ValueError("外部页面没有可读取的正文")
        result = store.source(origin=url, course=course, title=title, kind="external",
            fingerprint=digest(chunks), chunks=chunks, meta={"url": url, "retrieved_at": time.time(), "verification": "unverified_reference"})
        return {**result, "url": url, "title": title}
    except httpx.HTTPError as exc:
        raise ValueError("外部参考暂时无法读取；本地记录不变，可改用下载的教材") from exc
    finally:
        if own: client.close()


def search_reference(store, course, concept, client=None):
    if not 2 <= len(concept) <= 120 or re.search(r"[\\/@:]|sk-|\d{8,}", concept, re.I):
        raise ValueError("外部检索只发送简短概念词，不接受路径、账号、课堂原文或口令")
    own = client is None
    language = "zh" if re.search(r"[\u3400-\u9fff]", concept) else "en"
    base = f"https://{language}.wikipedia.org"
    client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=True,
        proxy=reference_proxy(base, store.path.parent), headers={"User-Agent": "SCUTStudy/0.11 (local study reference reader)"})
    try:
        response = client.get(base + "/w/api.php", params={"action": "query", "format": "json", "generator": "search",
            "gsrsearch": concept, "gsrlimit": 2, "prop": "extracts", "explaintext": 1, "exintro": 1, "exlimit": 2})
        response.raise_for_status()
        if len(response.content) > 300000:
            raise ValueError("外部检索响应过大")
        output = []
        for page in response.json().get("query", {}).get("pages", {}).values():
            title, text = page.get("title", ""), page.get("extract", "")[:7000]
            if not text: continue
            url = base + "/wiki/" + quote(title.replace(" ", "_"), safe="")
            chunks = split_text(text, {"url": url})
            item = store.source(origin=url, course=course, title=title, kind="external", fingerprint=digest(chunks), chunks=chunks,
                meta={"url": url, "retrieved_at": time.time(), "provider": "Wikipedia", "verification": "unverified_reference"})
            output.append({**item, "title": title, "url": url})
        return output
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise ValueError("外部百科暂不可用；可以粘贴公开教材链接，或指定本地教材交叉核对") from exc
    finally:
        if own: client.close()
