from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree as ET

from .storage import digest
from .materials import register_material

FORMATS = {".pdf", ".pptx", ".docx", ".md", ".txt"}
SKIP = {".venv", "venv", "node_modules", ".git", ".codex", ".aws", "site-packages", "__pycache__",
        "dist", "build", "cache", ".local", ".idea"}
MAX_SIZE = 32 * 1024 * 1024


def safe_file(root: Path, path: Path):
    root, path = root.resolve(), path.resolve()
    if not path.is_relative_to(root) or path.suffix.lower() not in FORMATS:
        raise ValueError("只读取所选资料目录中的 PDF、PPTX、DOCX、MD、TXT")
    if any(p.lower() in SKIP or p.startswith(".") for p in path.relative_to(root).parts):
        raise ValueError("跳过程序、隐藏目录和本地配置")
    if re.search(r"credential|password|secret|connection|token|api.?key", path.name, re.I):
        raise ValueError("跳过可能包含连接信息的文件")
    if not path.is_file() or path.stat().st_size > MAX_SIZE:
        raise ValueError("文件不存在或超过 32 MB")
    return path


def inventory(root: Path):
    root = root.resolve()
    if not root.is_dir() or len(root.parts) < 3:
        raise ValueError("请选择具体的课程资料文件夹")
    files, skipped = [], 0
    # Walk explicitly: never descend into environments, symlinks or build outputs.
    stack = [(root, 0)]
    visited = 0
    while stack and visited < 6000:
        directory, depth = stack.pop()
        for path in sorted(directory.iterdir()):
            visited += 1
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                continue
            if path.is_dir():
                if depth < 7 and path.name.lower() not in SKIP and not path.name.startswith("."):
                    stack.append((path, depth + 1))
                continue
            if path.suffix.lower() not in FORMATS:
                continue
            try:
                safe_file(root, path)
                files.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size})
            except ValueError:
                skipped += 1
            if len(files) >= 300:
                return {"files": files, "skipped": skipped, "limited": True}
    return {"files": files, "skipped": skipped, "limited": bool(stack)}


def split_text(text, locator):
    text = re.sub(r"[ \t]+", " ", text).strip()
    # Keep adjacent context and exact source excerpts. No ASR 'correction' here.
    return [{"text": text[pos:pos + 2200], "locator": {**locator, "offset": pos}}
            for pos in range(0, len(text), 1900) if text[pos:pos + 2200].strip()]


def extract(path: Path):
    chunks = []
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF 读取组件未安装，请安装 study 可选依赖") from exc
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("加密 PDF 需要先在本机解密")
        if len(reader.pages) > 800:
            raise ValueError("资料超过 800 页，请先选择章节")
        for n, page in enumerate(reader.pages, 1):
            chunks += split_text(page.extract_text() or "", {"page": n})
    elif suffix in {".pptx", ".docx"}:
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > 128 * 1024 * 1024:
                raise ValueError("解压后的文档过大")
            names = (["word/document.xml"] if suffix == ".docx" else
                     sorted([n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)],
                            key=lambda n: int(re.search(r"slide(\d+)", n)[1])))
            if suffix == ".pptx" and "ppt/presentation.xml" in archive.namelist():
                presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
                relationships = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
                targets = {r.attrib["Id"]: posixpath.normpath(posixpath.join("ppt", r.attrib["Target"]))
                           if not r.attrib["Target"].startswith("/") else r.attrib["Target"].lstrip("/")
                           for r in relationships if r.attrib.get("TargetMode") != "External"}
                names = [targets[node.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
                         for node in presentation.iter() if node.tag.endswith("}sldId")]
                if any(not re.fullmatch(r"ppt/slides/slide\d+\.xml", name) for name in names):
                    raise ValueError("PPTX 页序关系无效，请重新导出课件")
            for index, name in enumerate(names, 1):
                node = ET.fromstring(archive.read(name))
                text = "\n".join(t.text or "" for t in node.iter() if t.tag.endswith("}t"))
                chunks += split_text(text, {"slide" if suffix == ".pptx" else "section": index})
    else:
        chunks = split_text(path.read_text(encoding="utf-8-sig"), {"section": 1})
    if not chunks:
        raise ValueError("未提取到文字：可能是扫描图片，需 OCR 后再导入；未记为成功")
    if sum(len(c["text"]) for c in chunks) > 2_000_000:
        raise ValueError("资料文字过多，请按章节导入")
    return chunks


def import_file(store, root, relative, course):
    path = safe_file(root, root / relative)
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = store.duplicate(course, fingerprint)
    if existing:return existing
    return register_material(store, origin=str(path), course=course, title=path.name,
                             fingerprint=fingerprint, chunks=extract(path))


def sync_sessions(study, sessions):
    count = duplicates = 0
    active_origins = set()
    for session in sessions:
        origin = "session:" + session["id"]
        if session.get("deleted_at") or session.get("superseded_by"):
            continue
        active_origins.add(origin)
        segments = session.get("segments", [])
        if not segments:
            continue
        fingerprint = digest(["timed-spans-v2", segments, session.get("time_basis"), session.get("title"), session.get("page_url")])
        if study.duplicate(session.get("course_title") or "未分类课程", fingerprint):
            duplicates += 1
            continue
        chunks, buffer, size = [], [], 0

        def flush():
            if buffer:
                offset, spans = 0, []
                for s in buffer:
                    spans.append({"offset": offset, "length": len(s["text"]), "start": s.get("start"), "end": s.get("end")})
                    offset += len(s["text"]) + 1
                chunks.append({"text": "\n".join(s["text"] for s in buffer), "locator": {
                    "session_id": session["id"], "start": buffer[0].get("start", 0),
                    "end": buffer[-1].get("end", 0), "time_basis": session.get("time_basis", "video"),
                    "spans": spans, "segment_ids": [s.get("id", "") for s in buffer]}})

        for segment in segments:
            if size + len(segment.get("text", "")) > 2600 and buffer:
                flush()
                buffer, size = buffer[-2:], sum(len(s["text"]) for s in buffer[-2:])
            buffer.append({**segment, "text": str(segment.get("text", ""))[:4000]})
            size += len(buffer[-1]["text"])
        flush()
        result = study.source(origin=origin, course=session.get("course_title") or "未分类课程",
                              title=session.get("title", "课堂字幕"), kind="school_subtitle" if session.get("mode") == "subtitle" else "asr",
                              fingerprint=fingerprint, chunks=chunks,
                              meta={"course_id": session.get("course_id"), "sub_id": session.get("sub_id"),
                                    "page_url": session.get("page_url", "")})
        duplicates += int(result["duplicate"])
        count += int(not result["duplicate"])
    # Deleted/superseded classroom records stop participating in new answers.
    with study.connect() as db:
        for row in db.execute("SELECT id,origin FROM sources WHERE active=1 AND origin LIKE 'session:%'").fetchall():
            if row["origin"] not in active_origins:
                db.execute("UPDATE sources SET active=0 WHERE id=?", (row["id"],))
    return {"imported": count, "unchanged": duplicates}


def validate_visuals(course_url, items):
    url = urlparse(course_url)
    query = parse_qs(url.query)
    if url.scheme != "https" or url.netloc != "video.jw.scut.edu.cn" or url.path != "/livingroom":
        raise ValueError("请选择学校的具体课时页面")
    if not all(re.fullmatch(r"\d+", query.get(k, [""])[0]) for k in ("course_id", "sub_id")):
        raise ValueError("课时标识缺失")
    result, seen = [], set()
    for item in items:
        target = urlparse(item["url"])
        if (target.scheme != "https" or target.netloc != url.netloc or target.query or target.fragment
                or not re.fullmatch(r"/play/ai\d+/ppt/[\w/-]+\.jpe?g", target.path)):
            raise ValueError("屏幕图片不属于学校的 PPT 资源路径")
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        result.append(item)
    return result
