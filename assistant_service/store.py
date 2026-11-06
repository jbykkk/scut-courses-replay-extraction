from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .settings import atomic_json
from .records import annotate
from subtitle import json_to_srt as to_srt, json_to_text as to_txt


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "assistant.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, lane TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                priority INTEGER NOT NULL, created REAL NOT NULL, error TEXT);
            CREATE INDEX IF NOT EXISTS job_queue ON jobs(lane,state,priority,created);
        """)
        # ASR chunks are idempotent and keep their input files; resume them after a crash.
        # A partially downloaded media job requires an explicit retry instead.
        self.db.execute("UPDATE jobs SET state='pending',error=NULL WHERE state='running' AND lane='asr'")
        self.db.execute("UPDATE jobs SET state='failed',error='本地服务重启中断了任务，请重试' WHERE state='running'")
        self.db.commit()
        for s in self.list():
            if s.get("analysis_status") == "running" and not self.pending(s["id"], "analysis"):
                self.update(s["id"], analysis_status="failed", warning="服务重启中断了分析，点击继续生成笔记即可接着处理")
            if s["mode"] == "live" and s["status"] in {"recording", "stopping"}:
                self.update(s["id"], status="interrupted", warning="浏览器或服务重启；已接收音频保留，可完成已接收内容")

    def get(self, sid: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT data FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                raise KeyError("课时任务不存在")
            return json.loads(row[0])

    def list(self) -> list[dict]:
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM sessions ORDER BY rowid DESC")]

    def records(self):
        with self.lock:
            return annotate(self.list(), self.audio_inputs)

    def trash(self, sid):
        with self.lock:
            session = self.get(sid)
            if (not session["stopped"] and session["mode"] == "live") or any(j["state"] in {"pending", "running"} for j in self.jobs(sid)):
                raise ValueError("请先结束录音或停止本课任务，等待运行中的任务结束后再删除")
            stamp = time.time()
            for record in self.records():
                if record.get("superseded_by") == sid:
                    self.update(record["id"], deleted_at=stamp, deleted_with=sid)
            return self.update(sid, deleted_at=stamp)

    def restore(self, sid):
        with self.lock:
            for record in self.list():
                if record.get("deleted_with") == sid:
                    self.update(record["id"], deleted_at=None, deleted_with=None)
            return self.update(sid, deleted_at=None)

    def create(self, lesson: dict) -> dict:
        sid = uuid.uuid4().hex
        value = {
            **lesson, "id": sid, "created_at": time.time(), "updated_at": time.time(),
            "status": "recording" if lesson["mode"] == "live" else "queued",
            "segments": [], "events": [], "summary": None, "analysis_status": "idle",
            "warning": "", "error": "", "stopped": False, "last_analysis_end": 0,
        }
        with self.lock:
            self.db.execute("INSERT INTO sessions VALUES(?,?)", (sid, json.dumps(value, ensure_ascii=False)))
            self.db.commit()
            self.export(value)
        return value

    def imports(self) -> list[dict]:
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM imports ORDER BY rowid DESC")]

    def save_imports(self, items: list[dict]):
        # Persist before contacting the school. A failed lookup must not disappear with the popup.
        with self.lock, self.db:
            for item in items:
                row = self.db.execute("SELECT data FROM imports WHERE id=?", (item["request_id"],)).fetchone()
                old = json.loads(row[0]) if row else {}
                if old.get("status") == "queued":
                    continue  # A delayed browser update must not undo a successful hand-off.
                value = {**item, "created_at": old.get("created_at", time.time()), "updated_at": time.time()}
                self.db.execute("INSERT OR REPLACE INTO imports VALUES(?,?)",
                                (item["request_id"], json.dumps(value, ensure_ascii=False)))

    def update(self, sid: str, **changes) -> dict:
        with self.lock:
            value = self.get(sid)
            value.update(changes, updated_at=time.time())
            self.db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(value, ensure_ascii=False), sid))
            self.db.commit()
            self.export(value)
            return value

    def directory(self, sid: str) -> Path:
        # IDs are generated locally; containment is checked again for exports.
        if len(sid) != 32 or any(c not in "0123456789abcdef" for c in sid):
            raise ValueError("无效任务 ID")
        path = self.root / "sessions" / sid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def export(self, value: dict):
        directory = self.directory(value["id"])
        atomic_json(directory / "session.json", value)
        items = [{"BeginSec": x["start"], "EndSec": x["end"], "Text": x["text"]} for x in value["segments"]]
        atomic_json(directory / "subtitles.json", items)
        (directory / "subtitles.srt").write_text(to_srt(items), encoding="utf-8")
        (directory / "subtitles.txt").write_text(to_txt(items), encoding="utf-8")
        atomic_json(directory / "events.json", value["events"])
        lines = [f"# {value.get('course_title', '')} · {value.get('title', '')}", "",
                 f"来源：{value.get('page_url', '')}", f"状态：{value['status']}", ""]
        if summary := value.get("summary"):
            lines += ["## " + (summary.get("headline") or "本课概览"), summary.get("abstract") or summary.get("overview", ""), ""]
            for group in summary.get("groups", []):
                lines += ["### " + group["title"], group["detail"], ""]
            lines += ["## 分段笔记"]
            for topic in summary.get("topics", []):
                lines += ["", "### " + topic["title"], "",
                          f"[{topic.get('start', 0):.0f}s] · {topic.get('chapter') or '未明确章节'}", "",
                          topic.get("detail", ""), ""]
                for ref in topic.get("material_refs", []):
                    loc = ref.get("locator", {})
                    page = f"第 {loc['page']} 页" if "page" in loc else f"第 {loc.get('slide', '?')} 张"
                    lines += [f"  课件：{ref['title']} · {page}", "  摘录：" + ref["quote"]]
                cited = set(topic.get("segment_ids", []))
                for segment in value["segments"]:
                    if segment["id"] in cited:
                        lines += [f"  老师原话 [{segment['start']:.0f}s]：{segment['text']}"]
        lines += ["", "## 重点提醒（请核对原文）"]
        for event in value["events"]:
            if event.get("source") == "local_rule":
                continue
            lines += [f"- [{event['start']:.0f}s] **{event['label']}** {event['message']} ({event['source']})",
                      *[f"  {label}：{event['details'][key]}" for key, label in
                        (("action", "任务"), ("deadline", "时间"), ("submission", "提交方式"),
                         ("requirements", "具体要求"), ("grading", "评分")) if event.get("details", {}).get(key)],
                      f"  原文：{event['evidence']}"]
        candidates = value.get("rule_candidates", [e for e in value["events"] if e.get("source") == "local_rule"])
        if candidates:
            lines += ["", "<details><summary>关键词初筛记录（不是已确认事项）</summary>", ""]
            lines += [f"- [{e['start']:.0f}s] {e['evidence']}" for e in candidates]
            lines += ["", "</details>"]
        if value.get("warning"):
            lines += ["", "提示：" + value["warning"]]
        (directory / "notes.md").write_text("\n".join(lines), encoding="utf-8")

    def enqueue(self, sid: str, kind: str, payload: dict, lane="asr", priority=10, key=None):
        jid = key or uuid.uuid4().hex
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,'pending',?,?,NULL)",
                            (jid, sid, lane, kind, json.dumps(payload, ensure_ascii=False), priority, time.time()))
            self.db.commit()
        return jid

    def claim(self, lane: str, max_priority: int | None = None, exclude_sessions: set[str] | None = None):
        with self.lock:
            query = "SELECT * FROM jobs WHERE lane=? AND state='pending'"
            args = [lane]
            if max_priority is not None:
                query += " AND priority<=?"
                args.append(max_priority)
            minimum = self.db.execute("SELECT MIN(priority) FROM (" + query + ")", args).fetchone()[0]
            if minimum is None:
                return None
            query += " AND priority=?"
            args.append(minimum)
            if exclude_sessions:
                query += " AND session_id NOT IN (" + ",".join("?" for _ in exclude_sessions) + ")"
                args.extend(sorted(exclude_sessions))
            row = self.db.execute(query + " ORDER BY created LIMIT 1", args).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
                self.db.commit()
                return {**dict(row), "payload": json.loads(row["payload"])}

    def finish(self, jid: str, error: str = ""):
        with self.lock:
            self.db.execute("UPDATE jobs SET state=?,error=? WHERE id=?", ("failed" if error else "done", error, jid))
            self.db.commit()

    def jobs(self, sid: str | None = None) -> list[dict]:
        with self.lock:
            query = "SELECT id,session_id,lane,kind,state,error,created FROM jobs"
            rows = self.db.execute(query + (" WHERE session_id=?" if sid else "") + " ORDER BY created", (sid,) if sid else ())
            return [dict(row) for row in rows]

    def pending(self, sid: str, lane: str) -> bool:
        return any(j["lane"] == lane and j["state"] in {"pending", "running"} for j in self.jobs(sid))

    def retry(self, sid: str):
        with self.lock:
            self.db.execute("UPDATE jobs SET state='pending',error=NULL WHERE session_id=? AND state='failed' AND lane!='analysis'", (sid,))
            self.db.commit()

    def recover_missing_replay_audio(self, sid: str) -> bool:
        """Rebuild missing replay chunks before letting ASR claim failed jobs."""
        with self.lock:
            rows = self.db.execute("SELECT payload,state FROM jobs WHERE session_id=? AND kind='chunk' AND state IN ('failed','waiting_media')", (sid,)).fetchall()
            waiting = any(row["state"] == "waiting_media" for row in rows)
            missing = any(not Path(json.loads(row["payload"])["path"]).is_file() for row in rows)
            if not missing:
                if waiting:
                    self.release_waiting_media(sid)
                return waiting
            media = self.db.execute("SELECT id,state FROM jobs WHERE session_id=? AND kind='media' ORDER BY created DESC LIMIT 1", (sid,)).fetchone()
            if not media:
                raise ValueError("音频片段已丢失，且没有可重新下载的回放源；请从课程播放页重新导入")
            self.db.execute("UPDATE jobs SET state='waiting_media',error=NULL WHERE session_id=? AND kind='chunk' AND state='failed'", (sid,))
            if media["state"] not in {"pending", "running"}:
                self.db.execute("UPDATE jobs SET state='pending',error=NULL WHERE id=?", (media["id"],))
            self.db.commit()
            return True

    def release_waiting_media(self, sid: str):
        with self.lock:
            rows = self.db.execute("SELECT id,payload FROM jobs WHERE session_id=? AND state='waiting_media'", (sid,)).fetchall()
            for row in rows:
                exists = Path(json.loads(row["payload"])["path"]).is_file()
                self.db.execute("UPDATE jobs SET state=?,error=? WHERE id=?",
                                ("pending" if exists else "failed",
                                 None if exists else "重新下载后仍缺少音频片段，请从课程播放页重新导入", row["id"]))
            self.db.commit()

    def audio_inputs(self, sid: str):
        with self.lock:
            return [{"id": row["id"], **json.loads(row["payload"])} for row in self.db.execute(
                "SELECT id,payload FROM jobs WHERE session_id=? AND kind='chunk' ORDER BY created", (sid,))]

    def cancel_pending_analysis(self, sid: str, *, realtime_only=False):
        with self.lock:
            self.db.execute("UPDATE jobs SET state='cancelled' WHERE session_id=? AND lane='analysis' AND state='pending'"
                            + (" AND kind='analyze'" if realtime_only else ""), (sid,))
            self.db.commit()

    def cancel(self, sid: str):
        with self.lock:
            self.db.execute("UPDATE jobs SET state='cancelled' WHERE session_id=? AND state='pending'", (sid,))
            self.db.commit()
            self.update(sid, status="cancelled", stopped=True)
