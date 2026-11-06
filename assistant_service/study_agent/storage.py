from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def terms(text):
    text = text.casefold()
    words = re.findall(r"[a-z0-9][a-z0-9_-]{1,}|[\u3400-\u9fff]+", text)
    return set(part for word in words for part in
               ([word] if not re.search(r"[\u3400-\u9fff]", word) or len(word) < 3
                else [word[i:i + 2] for i in range(len(word) - 1)]))


ALIASES = {"截止": "deadline ddl due tbd", "作业": "homework assignment exercise", "要求": "requirement requirements",
           "提交": "submit submission deliverable", "进程": "process processes", "线程": "thread threads",
           "突触": "synapse synaptic", "神经元": "neuron neurons", "项目": "project", "考试": "exam examination"}


class StudyStore:
    """Versioned source snapshots. Open connections per operation (also safe on Windows)."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "study-agent.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, origin TEXT NOT NULL, course TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, active INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS source_origin ON sources(origin,course);
                CREATE TABLE IF NOT EXISTS evidence(
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, course TEXT NOT NULL,
                    text TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS evidence_source ON evidence(source_id);
                CREATE TABLE IF NOT EXISTS entities(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS entities_kind ON entities(kind);
                CREATE INDEX IF NOT EXISTS evidence_course ON evidence(course);
                PRAGMA user_version=1;
            """)
            # Tokenise Chinese bigrams once at ingestion instead of every question.
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS evidence_search USING fts5(evidence_id UNINDEXED, tokens)")
                pending = db.execute("SELECT e.id,e.text,e.data FROM evidence e WHERE NOT EXISTS (SELECT 1 FROM evidence_search f WHERE f.rowid=e.rowid)").fetchall()
                for row in pending:
                    value = json.loads(row["data"])
                    self._index(db, row["id"], row["text"], value["title"])
                self.indexed = True
            except sqlite3.OperationalError:
                self.indexed = False

    @staticmethod
    def _index(db, eid, text, title):
        row = db.execute("SELECT rowid FROM evidence WHERE id=?", (eid,)).fetchone()
        db.execute("DELETE FROM evidence_search WHERE rowid=?", (row[0],))
        db.execute("INSERT INTO evidence_search(rowid,evidence_id,tokens) VALUES(?,?,?)",
                   (row[0], eid, " ".join(sorted(terms(text+" "+title)))))

    def duplicate(self, course, fingerprint):
        with self.connect() as db:
            row=db.execute("SELECT id FROM sources WHERE course=? AND fingerprint=? AND active=1",(course,fingerprint)).fetchone()
        return {"id":row[0],"duplicate":True,"chunks":0} if row else None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def source(self, *, origin, course, title, kind, fingerprint, chunks, meta=None):
        sid = digest([origin, course, fingerprint])[:32]
        value = dict(id=sid, origin=origin, course=course, title=title, kind=kind,
                     fingerprint=fingerprint, created_at=time.time(), meta=meta or {})
        with self.connect() as db:
            old = db.execute("SELECT id FROM sources WHERE course=? AND fingerprint=? AND active=1",
                             (course, fingerprint)).fetchone()
            if old:
                return {"id": old[0], "duplicate": True, "chunks": 0}
            # New versions deactivate only this origin, never destroy an earlier citation.
            db.execute("UPDATE sources SET active=0 WHERE origin=? AND course=?", (origin, course))
            db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,1,?)",
                       (sid, origin, course, fingerprint, json.dumps(value, ensure_ascii=False)))
            for n, chunk in enumerate(chunks):
                eid = f"{sid}:{n}"
                item = {**chunk, "id": eid, "source_id": sid, "course": course, "title": title, "kind": kind}
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,course=excluded.course,text=excluded.text,data=excluded.data",
                           (eid, sid, course, item["text"], json.dumps(item, ensure_ascii=False)))
                if self.indexed:
                    self._index(db, eid, item["text"], title)
        return {"id": sid, "duplicate": False, "chunks": len(chunks)}

    def sources(self, course=""):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM sources WHERE active=1" + (" AND course=?" if course else "") + " ORDER BY rowid DESC", (course,) if course else ()).fetchall()
        return [json.loads(row[0]) for row in rows]

    def evidence(self, eid):
        with self.connect() as db:
            row = db.execute("SELECT e.data,s.active FROM evidence e JOIN sources s ON s.id=e.source_id WHERE e.id=?", (eid,)).fetchone()
        if not row:
            raise KeyError("来源片段不存在")
        from .knowledge import decorate_evidence
        return decorate_evidence(self, {**json.loads(row[0]), "active": bool(row[1])})

    def search(self, query, course, limit=8, *, source_id=None):
        wanted = terms(query + " " + " ".join(value for key, value in ALIASES.items() if key in query))
        if not wanted:
            return []
        scored = []
        from .knowledge import decorate_evidence
        policies = {(p["course"],p["origin"]):p for p in self.list("source_policy")}
        sources = {s["id"]:s for s in self.sources(course)}
        if source_id is not None:
            sources = {sid:s for sid,s in sources.items() if sid == source_id}
            if not sources:
                return []
        hidden = {s["id"] for s in sources.values() if policies.get((s["course"],s["origin"]),{}).get("excluded")}
        with self.connect() as db:
            scope = " AND e.source_id=?" if source_id is not None else ""
            scope_args = (source_id,) if source_id is not None else ()
            if self.indexed:
                match=" OR ".join('"'+word.replace('"','""')+'"' for word in sorted(wanted)[:160])
                excluding=" AND e.source_id NOT IN ("+",".join("?" for _ in hidden)+")" if hidden else ""
                rows=db.execute("SELECT e.data FROM evidence_search f JOIN evidence e ON e.rowid=f.rowid JOIN sources s ON s.id=e.source_id WHERE evidence_search MATCH ? AND s.active=1 AND e.course=?"+scope+excluding+" ORDER BY rank LIMIT ?",
                    (match, course, *scope_args, *sorted(hidden), max(96, limit*4))).fetchall()
            else:
                rows=db.execute("SELECT e.data FROM evidence e JOIN sources s ON s.id=e.source_id WHERE s.active=1 AND e.course=?"+scope,(course,*scope_args)).fetchall()
        for row in rows:
            value = json.loads(row[0])
            if value["source_id"] in hidden:
                continue
            found = terms(value["text"] + " " + value["title"])
            score = len(wanted & found) / max(1, len(wanted))
            if score:
                scored.append((score, value))
        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [decorate_evidence(self, item, source=sources[item["source_id"]], policies=policies) for _, item in scored[:limit]]

    def save(self, kind, value, eid=None):
        eid = eid or digest([kind, value, time.time_ns()])[:32]
        value = {**value, "id": eid, "kind": kind, "updated_at": time.time()}
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO entities VALUES(?,?,?)", (eid, kind, json.dumps(value, ensure_ascii=False)))
        return value

    def get(self, eid, kind=None):
        with self.connect() as db:
            row = db.execute("SELECT kind,data FROM entities WHERE id=?", (eid,)).fetchone()
        if not row or kind and row[0] != kind:
            raise KeyError("条目不存在")
        return json.loads(row[1])

    def list(self, kind):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM entities WHERE kind=? ORDER BY rowid DESC", (kind,))]

    def save_run(self, value):
        value = {**value, "evidence_ids":[e["id"] for e in value.get("evidence",[])]}
        value.pop("evidence",None)  # immutable evidence is already stored by ID
        rid = digest([value, time.time_ns()])[:32]
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?)", (rid, json.dumps(value, ensure_ascii=False)))
        return rid
