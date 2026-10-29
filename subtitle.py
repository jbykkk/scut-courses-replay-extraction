import json
import math
from pathlib import Path

from config import OUTPUT_DIR, validate_numeric_id


def extract_subtitle_items(body) -> list[dict] | None:
    """从 JSON 响应中提取有效字幕列表，支持多层嵌套。"""
    if isinstance(body, list):
        if body and all(isinstance(item, dict) for item in body):
            if any("BeginSec" in item and "Text" in item for item in body):
                return body
        for item in body:
            if isinstance(item, (dict, list)):
                inner = extract_subtitle_items(item)
                if inner:
                    return inner
        return None
    if isinstance(body, dict):
        for key in ("all_content", "data", "result", "list", "rows"):
            value = body.get(key)
            if isinstance(value, (dict, list)):
                inner = extract_subtitle_items(value)
                if inner:
                    return inner
    return None


def format_time(seconds: float) -> str:
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("字幕时间必须是有限的非负数")
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _subtitle_text(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    raw_text = item.get("Text", "")
    return raw_text.strip() if isinstance(raw_text, str) else ""


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timed_subtitle(item: object) -> tuple[str, float, float] | None:
    text = _subtitle_text(item)
    if not text or not isinstance(item, dict) or "BeginSec" not in item:
        return None
    begin = _finite_number(item["BeginSec"])
    if begin is None or begin < 0:
        return None
    raw_end = item.get("EndSec")
    end = begin + 5 if raw_end is None else _finite_number(raw_end)
    if end is None or end < begin:
        return None
    return text, begin, end


def json_to_srt(subtitle_json: list[dict]) -> str:
    lines = []
    for item in subtitle_json:
        normalized = _timed_subtitle(item)
        if normalized is None:
            continue
        text, begin, end = normalized
        lines.append(str(len(lines) // 4 + 1))
        lines.append(f"{format_time(begin)} --> {format_time(end)}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines).rstrip("\n")


def json_to_text(subtitle_json: list[dict]) -> str:
    return "\n".join(text for item in subtitle_json if (text := _subtitle_text(item)))


def sanitize_filename_component(value: object) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(value)
    ).strip("._ ")
    return cleaned or "untitled"


def _safe_output_path(directory: Path, filename: str) -> Path:
    resolved_output = OUTPUT_DIR.resolve()
    resolved_directory = directory.resolve()
    if not resolved_directory.is_relative_to(resolved_output):
        raise ValueError("字幕输出子目录越界。")
    candidate = (resolved_directory / filename).resolve()
    if not candidate.is_relative_to(resolved_directory):
        raise ValueError("字幕输出路径越界。")
    return candidate


def save_subtitle(
    subtitle_json: list[dict], sub_id: str, course_name: str | None = None
) -> tuple[Path, Path, Path]:
    safe_sub_id = sanitize_filename_component(
        validate_numeric_id(sub_id, "sub_id")
    )
    safe_name = sanitize_filename_component(course_name or safe_sub_id)
    base_name = f"{safe_name}_{safe_sub_id}" if course_name else safe_sub_id

    json_dir = OUTPUT_DIR / "json"
    srt_dir = OUTPUT_DIR / "srt"
    txt_dir = OUTPUT_DIR / "txt"
    for d in (json_dir, srt_dir, txt_dir):
        d.mkdir(parents=True, exist_ok=True)

    json_path = _safe_output_path(json_dir, f"{base_name}.json")
    srt_path = _safe_output_path(srt_dir, f"{base_name}.srt")
    txt_path = _safe_output_path(txt_dir, f"{base_name}.txt")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(subtitle_json, f, ensure_ascii=False, indent=2)

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(json_to_srt(subtitle_json))

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(json_to_text(subtitle_json))

    return json_path, srt_path, txt_path
