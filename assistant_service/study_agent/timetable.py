from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .planner import TZ

# Reference schedule, never silently treated as a confirmed current timetable.
PERIOD_REFERENCE = {
    "label": "大学城 / 广州国际校区参考作息",
    "source_url": "https://www2.scut.edu.cn/_upload/article/files/8d/32/1dbbcf9a47729b8e2b85e4c02e01/a25ee229-19e8-4f09-afd5-3ea70b02bec6.pdf",
    "periods": [{"number": i + 1, "start": start, "end": end} for i, (start, end) in enumerate([
        ("08:50", "09:35"), ("09:40", "10:25"), ("10:40", "11:25"), ("11:30", "12:15"),
        ("14:00", "14:45"), ("14:50", "15:35"), ("15:45", "16:30"), ("16:35", "17:20"),
        ("19:00", "19:45"), ("19:55", "20:40"), ("20:50", "21:35")])],
}


def week_numbers(text):
    if not re.fullmatch(r"[第\d周,，、\-()单双\s]+", text):
        raise ValueError("周次格式需要人工确认")
    weeks = set()
    for match in re.finditer(r"(?:第)?(\d+)(?:-(\d+))?周(?:\(([单双])\))?", text):
        first, last = int(match[1]), int(match[2] or match[1])
        if first < 1 or last > 30 or last < first:
            raise ValueError("周次超出范围")
        weeks.update(w for w in range(first, last + 1) if not match[3] or w % 2 == (1 if match[3] == "单" else 0))
    if not weeks:
        raise ValueError("未识别周次")
    return sorted(weeks)


def extract_timetable(path: Path):
    """SCUT grid PDF only. Column-aware extraction returns a proposal, never confirmed events."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("请先安装 study 可选依赖以读取课表表格") from exc
    candidates, warnings, titles, campuses = [], [], {}, set()
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > 10:
            raise ValueError("课表超过 10 页，请选择学校导出的单人课表")
        for page_no, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                for row in table:
                    if len(row) != 9 or any("学号" in (cell or "") or "星期一" in (cell or "") for cell in row):
                        continue
                    for weekday, cell in enumerate(row[2:]):
                        if not cell:
                            continue
                        text = cell.replace("（", "(").replace("）", ")")
                        if "广州国际" in text:
                            campuses.add("gzic")
                        if "五山校区" in text:
                            campuses.add("wushan")
                        matches = list(re.finditer(r"\((\d+)-(\d+)节\)([^/]+?)/校区", text))
                        for index, match in enumerate(matches):
                            prefix = text[(matches[index - 1].end() if index else 0):match.start()]
                            # A new course follows the previous course's credits. Wrapped names join.
                            prefix = re.split(r"/学\s*分[:：]\s*\d+(?:\.\d+)?", prefix)[-1].strip()
                            if prefix and not any(s in prefix for s in ("/", "校区", "教学班", "教师", "选课", "学分")) and len(prefix) < 60:
                                titles[weekday] = prefix.replace("\n", "")
                            title = titles.get(weekday)
                            weeks_text = re.sub(r"\s+", "", match[3])
                            if not title:
                                warnings.append(f"第 {page_no} 页星期{weekday + 1}的课程名称需核对")
                                continue
                            try:
                                weeks = week_numbers(weeks_text)
                            except ValueError:
                                warnings.append(f"{title}：周次“{weeks_text}”需要核对")
                                continue
                            candidates.append({"course": title, "weekday": weekday, "first_period": int(match[1]),
                                               "last_period": int(match[2]), "weeks": weeks, "weeks_text": weeks_text,
                                               "source_page": page_no})
    if not candidates:
        raise ValueError("没有读出课表网格；请使用学校导出的 PDF，或手动登记忙碌时段")
    return {"status": "pending", "candidates": candidates, "warnings": warnings,
            "campus_hint": "gzic" if campuses == {"gzic"} else "", "period_reference": PERIOD_REFERENCE,
            "notice": "核对课程所在星期、节次和单双周，再填写第 1 周星期一与作息时间；尚未写入日历。"}


def expand_timetable(candidates, semester_monday: date, periods):
    if semester_monday.weekday() != 0:
        raise ValueError("第 1 周起始日期必须是星期一")
    by_number = {p["number"]: p for p in periods}
    result = []
    for item in candidates:
        if item["first_period"] > item["last_period"]:
            raise ValueError("课表起止节次错误")
        try:
            first, last = by_number[item["first_period"]], by_number[item["last_period"]]
        except KeyError as exc:
            raise ValueError("请填写所有用到的起止节次的作息时间") from exc
        begin, end = time.fromisoformat(first["start"]), time.fromisoformat(last["end"])
        if end <= begin:
            raise ValueError("下课时间须晚于上课时间")
        for week in item["weeks"]:
            day = semester_monday + timedelta(days=(week - 1) * 7 + item["weekday"])
            result.append({"start": datetime.combine(day, begin, TZ).isoformat(),
                           "end": datetime.combine(day, end, TZ).isoformat(), "course": item["course"]})
    result.sort(key=lambda r: r["start"])
    conflicts = [{"left": a, "right": b} for a, b in zip(result, result[1:])
                 if a["end"] > b["start"] and a["course"] != b["course"]]
    return {"busy": result, "conflicts": conflicts}
