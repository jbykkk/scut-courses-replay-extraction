from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .storage import digest

TZ = timezone(timedelta(hours=8))  # SCUT local time; works without Windows tzdata.


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)


def make_plan(tasks, *, start: date, days=7, daily_minutes=90, start_hour=19, busy=None):
    """Deterministic, capacity-limited proposal. Durations are user estimates, not model certainty."""
    busy = [(stamp(b["start"]), stamp(b["end"])) for b in (busy or [])]
    active = [t for t in tasks if t.get("status") == "accepted" and not t.get("completed")]
    active.sort(key=lambda t: (t.get("deadline") or "9999-12-31", -t.get("difficulty", 3), t["id"]))
    scheduled, risks, remaining = [], [], {}
    for task in active:
        remaining[task["id"]] = max(0, task.get("estimate_max", 60) - task.get("spent_minutes", 0))
        if not task.get("deadline"):
            risks.append({"task_id": task["id"], "text": "截止时间未确认；暂按学习任务安排，不生成到期提醒。"})
    for day in range(days):
        local_day = start + timedelta(days=day)
        cursor = datetime.combine(local_day, time(start_hour), TZ)
        end = cursor + timedelta(minutes=daily_minutes)
        # Short blocks + recovery gaps, inside the stated daily availability window.
        while cursor + timedelta(minutes=15) <= end:
            possible = [t for t in active if remaining[t["id"]] > 0 and
                        (not t.get("deadline") or local_day.isoformat() <= t["deadline"])]
            if not possible:
                break
            task = possible[0]
            minutes = min(40, remaining[task["id"]], int((end - cursor).total_seconds() // 60))
            finish = cursor + timedelta(minutes=minutes)
            conflicts = [(a, b) for a, b in busy if cursor < b and finish > a]
            if conflicts:
                cursor = max(b for _, b in conflicts) + timedelta(minutes=5)
                continue
            scheduled.append({"task_id": task["id"], "course": task["course"], "title": task["title"],
                              "start": cursor.isoformat(), "end": finish.isoformat(),
                              "reason": f"按你确认的 {task.get('estimate_min', 30)}–{task.get('estimate_max', 60)} 分钟估计拆分；难度 {task.get('difficulty', 3)}/5"})
            remaining[task["id"]] -= minutes
            cursor = finish + timedelta(minutes=10)
    for task in active:
        rest = remaining[task["id"]]
        if rest:
            risks.append({"task_id": task["id"], "text": f"当前 {days} 天空闲时段排不完，还剩约 {rest} 分钟；需提前开始、增加时段或缩小任务范围。"})
        if deadline := task.get("deadline"):
            available_days = (date.fromisoformat(deadline) - start).days + 1
            total = max(0, task.get("estimate_max", 60) - task.get("spent_minutes", 0))
            if available_days > 0 and total > available_days * daily_minutes * .6:
                risks.append({"task_id": task["id"], "text": f"距离截止还有 {available_days} 天，但这项任务的估计学习量已占大部分可用时间，建议本周开始。"})
    return {"status": "draft", "start_date": start.isoformat(), "days": days, "blocks": scheduled, "risks": risks,
            "assumptions": ["按耗时估计的上限排程，每段后留 10 分钟。", "只避开你已登记的忙碌时段；课表未确认时不保证无冲突。", "完成情况和耗时改变后需要重新生成；不会自动写入外部日历。"]}


def calendar(plan):
    if plan.get("status") != "accepted":
        raise ValueError("请先确认这份计划，再导出日历")

    def escape(value):
        return str(value).replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")

    def utc(value):
        return stamp(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SCUT//Study Agent//ZH", "CALSCALE:GREGORIAN"]
    for block in plan["blocks"]:
        uid = digest([plan["id"], block])[:32]
        lines += ["BEGIN:VEVENT", "UID:" + uid + "@scut-study.local", "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                  "DTSTART:" + utc(block["start"]), "DTEND:" + utc(block["end"]),
                  "SUMMARY:" + escape(block["course"] + " · " + block["title"]), "DESCRIPTION:" + escape(block["reason"]), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    # RFC 5545 folds UTF-8 by octets, without splitting a code point.
    folded = []
    for line in lines:
        current = ""
        for char in line:
            if len((current + char).encode("utf-8")) > 73:
                folded.append(current)
                current = " "
            current += char
        folded.append(current)
    return "\r\n".join(folded) + "\r\n"


def review_interval(attempts, rating):
    # Self-report, deliberately not a claim of objectively measured mastery.
    streak = 0
    for prior in reversed(attempts):
        if prior.get("rating", 0) < 3:
            break
        streak += 1
    return 1 if rating < 3 else min(21, [2, 4, 7, 14, 21][min(streak, 4)])
