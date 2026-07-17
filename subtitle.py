import json
from pathlib import Path

from config import OUTPUT_DIR


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
    if seconds < 0:
        raise ValueError("字幕时间不能为负数")
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def json_to_srt(subtitle_json: list[dict]) -> str:
    lines = []
    index = 0
    for item in subtitle_json:
        raw_text = item.get("Text", "")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text:
            continue

        try:
            begin = float(item.get("BeginSec", 0))
            raw_end = item.get("EndSec")
            end = float(raw_end) if raw_end is not None else begin + 5
            if begin < 0 or end < begin:
                continue
        except (TypeError, ValueError):
            continue

        index += 1
        lines.append(str(index))
        lines.append(f"{format_time(begin)} --> {format_time(end)}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines)


def json_to_text(subtitle_json: list[dict]) -> str:
    return "\n".join(
        item.get("Text", "").strip()
        for item in subtitle_json
        if isinstance(item.get("Text", ""), str) and item.get("Text", "").strip()
    )


def save_subtitle(
    subtitle_json: list[dict], sub_id: str, course_name: str | None = None
) -> tuple[Path, Path, Path]:
    json_dir = OUTPUT_DIR / "json"
    srt_dir = OUTPUT_DIR / "srt"
    txt_dir = OUTPUT_DIR / "txt"
    for d in (json_dir, srt_dir, txt_dir):
        d.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in (course_name or sub_id)
    )
    base_name = f"{safe_name}_{sub_id}" if course_name else str(sub_id)

    json_path = json_dir / f"{base_name}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(subtitle_json, f, ensure_ascii=False, indent=2)

    srt_path = srt_dir / f"{base_name}.srt"
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(json_to_srt(subtitle_json))

    txt_path = txt_dir / f"{base_name}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(json_to_text(subtitle_json))

    return json_path, srt_path, txt_path
