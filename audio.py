"""Replay media selection, lossless AAC extraction and audio archive metadata."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from config import validate_numeric_id
from subtitle import sanitize_filename_component


class ReplayError(RuntimeError):
    pass


def read_report(path: Path, include_errors: bool = False) -> tuple[str, list[str]]:
    """Select missing-subtitle lessons from the browser extension's export report."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("results"), list):
            raise ValueError("缺少 results 数组")
        course_id = validate_numeric_id(report.get("course_id"), "course_id")
        wanted = {"no_subtitle", "error"} if include_errors else {"no_subtitle"}
        sub_ids = []
        for item in report["results"]:
            if not isinstance(item, dict):
                raise ValueError("results 中的课时必须是对象")
            if item.get("status") in wanted:
                sub_ids.append(validate_numeric_id(item.get("sub_id"), "sub_id"))
        return course_id, list(dict.fromkeys(sub_ids))
    except (OSError, ValueError) as exc:
        raise ReplayError(f"无法读取字幕导出报告：{exc}") from exc


def extract_media(data: dict) -> tuple[str, str]:
    """Prefer the main playback, then backup, teacher camera and other qualities."""
    content = data.get("content")
    content = content if isinstance(content, dict) else {}
    candidates = []
    for key, field in (("save_playback", "contents"), ("playback", "url")):
        playback = content.get(key)
        if isinstance(playback, dict):
            candidates.append((f"content.{key}.{field}", playback.get(field)))
    videos = data.get("video_list") or []
    if isinstance(videos, dict):
        videos = list(videos.values())
    if isinstance(videos, list):
        videos = [video for video in videos if isinstance(video, dict)]
        videos.sort(key=lambda video: str(video.get("type")) != "3")
        candidates.extend(
            (f"video_list(type={video.get('type')})", video.get("preview_url"))
            for video in videos
        )
    playurl = data.get("playurl")
    if isinstance(playurl, dict):
        candidates.extend((f"playurl.{key}", url) for key, url in playurl.items())
    for field, url in candidates:
        if not isinstance(url, str):
            continue
        try:
            parsed = urlparse(url)
            valid = parsed.scheme in ("http", "https") and parsed.hostname
        except ValueError:
            continue
        if valid:
            if (
                str(data.get("is_m3u8")).lower() == "yes"
                or parsed.path.lower().endswith(".m3u8")
            ):
                raise ReplayError("当前音频下载仅支持普通 MP4 回放，暂不支持 HLS。")
            return field, url
    raise ReplayError("未找到有效的 HTTP(S) 回放媒体地址。")


def output_paths(
    out_dir: Path, course_id: str, sub_id: str, title: object
) -> tuple[Path, Path]:
    """Keep all generated paths inside the selected course's archive directory."""
    course_id = validate_numeric_id(course_id, "course_id")
    sub_id = validate_numeric_id(sub_id, "sub_id")
    root = out_dir.expanduser().resolve()
    directory = (root / course_id).resolve()
    if not directory.is_relative_to(root):
        raise ReplayError("音频课程目录越界。")
    title = title.strip() if isinstance(title, str) else ""
    date = re.match(r"\d{4}-\d{2}-\d{2}", title)
    stem = date[0] if date else sanitize_filename_component(title)[:80]
    if not title:
        stem = ""
    name = f"{stem}_{sub_id}" if stem else sub_id
    output = (directory / f"{name}.m4a").resolve()
    manifest = (directory / "媒体地址.json").resolve()
    if not all(path.is_relative_to(directory) for path in (output, manifest)):
        raise ReplayError("音频输出路径越界。")
    return output, manifest


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ReplayError(f"找不到 {name}，请先安装 ffmpeg（macOS: brew install ffmpeg）。")
    return path


def probe_audio(ffprobe: str, path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        metadata = json.loads(result.stdout)
        duration = float(metadata["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or not any(
            stream.get("codec_type") == "audio" for stream in metadata["streams"]
        ):
            raise ValueError("没有有效音轨或时长")
        return {"duration_seconds": round(duration, 3), "size_bytes": path.stat().st_size}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise ReplayError(f"音频校验失败：{path.name}；已有文件可用 --force 重新下载。") from exc


async def download_audio(
    ffmpeg: str, ffprobe: str, url: str, output: Path, force: bool = False
) -> tuple[str, dict]:
    if output.exists() and output.stat().st_size > 0 and not force:
        return "exists", await asyncio.to_thread(probe_audio, ffprobe, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".part.m4a", dir=output.parent
    )
    os.close(fd)
    part = Path(temporary)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-rw_timeout", "30000000", "-i", url, "-map", "0:a:0", "-vn",
            "-c:a", "copy", "-movflags", "+faststart", str(part),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[-800:]
            raise ReplayError(f"ffmpeg 提取音频失败：{detail}")
        metadata = await asyncio.to_thread(probe_audio, ffprobe, part)
        part.replace(output)
        return "downloaded", metadata
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        part.unlink(missing_ok=True)


def update_manifest(path: Path, entry: dict) -> None:
    """Update one lesson atomically; preserve other lessons and archive timestamps."""
    try:
        data = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists() else {"items": []}
        )
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("清单缺少 items 数组")
        if any(not isinstance(item, dict) for item in data["items"]):
            raise ValueError("清单课时必须是对象")
        if (
            data.get("course_id") is not None
            and str(data["course_id"]) != str(entry["course_id"])
        ):
            raise ValueError("清单 course_id 与当前课程不一致")
        items = {str(item.get("sub_id")): item for item in data["items"]}
        sub_id = str(entry["sub_id"])
        items[sub_id] = items.get(sub_id, {}) | entry
        data["course_id"] = entry["course_id"]
        if entry.get("course_title"):
            data["course_title"] = entry["course_title"]
        data["items"] = sorted(
            items.values(), key=lambda item: str(item.get("title") or item.get("sub_id"))
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=path.parent)
        part = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        raise ReplayError(f"无法更新媒体清单：{exc}") from exc
