"""Local execution events, request snapshots and usage. No model hidden reasoning."""
from __future__ import annotations
import contextvars
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

_CURRENT = contextvars.ContextVar("scut_execution", default=None)
_PHASE = contextvars.ContextVar("scut_model_phase", default=("answer", 2400))
_SCHEMA_LOCK = threading.Lock()
SECRET_FIELDS = {"key", "token", "authorization", "password", "secret", "cookie", "api_key", "bearer"}
HIDDEN_FIELDS = {"reasoning_content", "chain_of_thought", "encrypted_reasoning"}
def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

def encoded(value):
    text=compact(value)
    return b"Z1"+zlib.compress(text.encode("utf-8")) if len(text)>8000 else text

def decoded(value):
    return json.loads(zlib.decompress(value[2:]) if isinstance(value,bytes) and value.startswith(b"Z1") else value)

def safe(value, secrets=()):
    if isinstance(value, dict):
        return {str(k): "[redacted]" if str(k).lower() in SECRET_FIELDS else safe(v, secrets)
                for k,v in value.items() if str(k).lower() not in HIDDEN_FIELDS}
    if isinstance(value, (list,tuple)):
        return [safe(v,secrets) for v in value[:1000]]
    if isinstance(value,str):
        if value.startswith("data:image/"):
            return "[图片输入，仅记录摘要 "+hashlib.sha256(value.encode()).hexdigest()[:12]+"]"
        for secret in secrets:
            if secret and len(secret)>6: value=value.replace(secret,"[redacted]")
        value=re.sub(r"(?i)(Bearer\s+)[\w.\-]{10,}",r"\1[redacted]",value)
        value=re.sub(r"(?:sk-|ghp_|github_pat_)[A-Za-z0-9_\-]{16,}","[redacted]",value)
        return value[:180000]+("…[快照超过长度限制]" if len(value)>180000 else "")
    return value

class RuntimeLog:
    def __init__(self, root):
        self.path=Path(root)/"agent-runtime.sqlite3"
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # Concurrent first requests can race journal_mode before SQLite's busy
        # handler is active. Serialize only schema setup; event writes stay concurrent.
        with _SCHEMA_LOCK, self.db() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY,parent_id TEXT,kind TEXT,label TEXT,course TEXT,
                status TEXT,started REAL,updated REAL,ended REAL,metrics TEXT);
              CREATE TABLE IF NOT EXISTS runtime_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,execution_id TEXT,at REAL,kind TEXT,label TEXT,data TEXT);
              CREATE INDEX IF NOT EXISTS events_execution ON runtime_events(execution_id,seq);
              CREATE INDEX IF NOT EXISTS executions_time ON executions(started);
              CREATE TABLE IF NOT EXISTS result_cache(key TEXT PRIMARY KEY,created REAL,value TEXT);
            """)
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()
    def begin(self,kind,label,course="",parent_id=None):
        rid=uuid.uuid4().hex;now=time.time()
        with self.db() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (rid,parent_id,kind,safe(label[:250]),safe(course),"running",now,now,None,"{}"))
            old=db.execute("SELECT id FROM executions WHERE status!='running' AND (started<? OR id NOT IN (SELECT id FROM executions ORDER BY started DESC LIMIT 300))",(now-30*86400,)).fetchall()
            for row in old:
                db.execute("DELETE FROM runtime_events WHERE execution_id=?",(row[0],))
                db.execute("DELETE FROM executions WHERE id=?",(row[0],))
            db.execute("DELETE FROM result_cache WHERE created<?",(now-7*86400,))
        return rid
    def emit(self,rid,kind,label,data=None):
        with self.db() as db:
            db.execute("INSERT INTO runtime_events(execution_id,at,kind,label,data) VALUES(?,?,?,?,?)",
                       (rid,time.time(),kind,label,encoded(safe(data or {}))))
            db.execute("UPDATE executions SET updated=? WHERE id=?",(time.time(),rid))
    def finish(self,rid,status):
        detail=self.detail(rid)
        events=detail["events"]
        requests=[e for e in events if e["kind"]=="model.start"]
        responses=[e["data"] for e in events if e["kind"] in {"model.finish","model.error"}]
        usage=[x["usage"] for x in responses if x.get("usage") is not None]
        metrics={"model_calls":len(requests),"reported_calls":len(usage),
                 "input_tokens":sum(x.get("input_tokens",0) for x in usage),
                 "output_tokens":sum(x.get("output_tokens",0) for x in usage),
                 "cached_input_tokens":sum(x.get("cached_input_tokens") or 0 for x in usage),
                 "cache_reported_calls":sum(x.get("cached_input_tokens") is not None for x in usage),
                 "cache_reuses":sum(e["kind"]=="cache.hit" for e in events),
                 "tool_calls":sum(e["kind"]=="tool.start" for e in events)}
        with self.db() as db:
            db.execute("UPDATE executions SET status=?,updated=?,ended=?,metrics=? WHERE id=?",
                       (status,time.time(),time.time(),compact(metrics),rid))
    def list(self,limit=40):
        with self.db() as db:
            rows=db.execute("SELECT * FROM executions ORDER BY started DESC LIMIT ?",(min(100,limit),)).fetchall()
        return [dict(row)|{"metrics":json.loads(row["metrics"])} for row in rows]
    def detail(self,rid,after=0):
        with self.db() as db:
            run=db.execute("SELECT * FROM executions WHERE id=?",(rid,)).fetchone()
            if not run: raise KeyError("运行记录不存在或已经过期")
            events=db.execute("SELECT * FROM runtime_events WHERE execution_id=? AND seq>? ORDER BY seq LIMIT 1000",(rid,after)).fetchall()
        return dict(run)|{"metrics":json.loads(run["metrics"]),"events":[dict(e)|{"data":decoded(e["data"])} for e in events]}
    def recover(self):
        with self.db() as db:
            db.execute("UPDATE executions SET status='interrupted',ended=?,updated=? WHERE status='running'",(time.time(),time.time()))
    def state(self,rid):
        with self.db() as db:row=db.execute("SELECT status FROM executions WHERE id=?",(rid,)).fetchone()
        return row[0] if row else None
    def cache_get(self,key):
        with self.db() as db:
            row=db.execute("SELECT value FROM result_cache WHERE key=? AND created>?",(key,time.time()-7*86400)).fetchone()
        return json.loads(row[0]) if row else None
    def cache_put(self,key,value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO result_cache VALUES(?,?,?)",(key,time.time(),compact(value)))
            db.execute("DELETE FROM result_cache WHERE key NOT IN (SELECT key FROM result_cache ORDER BY created DESC LIMIT 256)")

class Execution:
    def __init__(self,log,rid):self.log,self.id,self.status=log,rid,"completed"
    def emit(self,kind,label,data=None):
        if self.log is None:return
        try:self.log.emit(self.id,kind,label,data)
        except sqlite3.Error:pass  # Instrumentation must not stop a lecture.
def current():return _CURRENT.get()
def emit(kind,label,data=None):
    if current():current().emit(kind,label,data)
@contextmanager
def phase(name,max_tokens=2400):
    token=_PHASE.set((name,max_tokens))
    try:yield
    finally:_PHASE.reset(token)
def model_phase():return _PHASE.get()

@contextmanager
def execution(root,kind,label,course="",parent_id=None):
    if current():
        yield current();return
    try:
        log=RuntimeLog(root);rid=log.begin(kind,label,course,parent_id)
    except (sqlite3.Error,OSError):
        log,rid=None,None
    active=Execution(log,rid);token=_CURRENT.set(active);state="completed"
    active.emit("run.start","开始处理",{"kind":kind,"course":course})
    try:yield active
    except Exception as exc:
        state="failed";active.emit("run.error","处理未完成",{"error":str(exc)[:500]});raise
    finally:
        if state=="completed":state=active.status
        active.emit("run.finish","处理结束",{"status":state})
        try:
            if log is not None:log.finish(rid,state)
        except sqlite3.Error:pass
        _CURRENT.reset(token)

def traced(kind):
    def decorate(fn):
        @wraps(fn)
        def call(*args,**kwargs):
            obj=args[0]
            if kind=="classroom_job" and args[1].get("kind") not in {"analyze","summary"}:
                return fn(*args,**kwargs)
            root=obj.path.parent if hasattr(obj,"path") else obj.settings.root
            job=args[1] if kind=="operation" else {}
            label=job.get("question",kwargs.get("question",kind))
            course=job.get("course",kwargs.get("course",""))
            if kind=="classroom":label="课堂分析 · "+str(len(args[1]) if len(args)>1 else 0)+" 段内容"
            if kind=="feedback":label="练习反馈 · "+str(args[2].get("question",""))[:140]
            if kind=="classroom_job":
                session=obj.store.get(args[1]["session_id"])
                course=session.get("course_title","")
                label=("课堂笔记 · " if args[1]["kind"]=="summary" else "实时事项 · ")+session.get("title","")
            with execution(root,kind,label,course,
                           job.get("parent_trace")) as run:
                value=fn(*args,**kwargs)
                if kind=="operation" and value is None and obj.store.get(job["id"],"operation").get("status")=="cancelled":
                    run.status="cancelled"
                if isinstance(value,dict):
                    value.setdefault("trace_id",run.id)
                    if value.get("status") in {"failed","cancelled"}:
                        run.status=value["status"]
                        emit("tool.error","操作未完成",{"status":value["status"],"message":value.get("message","")})
                    if kind=="operation":
                        emit("tool.finish","操作结束",{k:v for k,v in value.items() if k in {"status","message","files","id"}})
                return value
        return call
    return decorate

def usage_of(body):
    u=body.get("usage")
    if not isinstance(u,dict) or "prompt_tokens" not in u:return None
    details=u.get("prompt_tokens_details") or {}
    cached=u.get("prompt_cache_hit_tokens", details.get("cached_tokens") if isinstance(details,dict) else None)
    try:
        return {"input_tokens":int(u.get("prompt_tokens") or 0),"output_tokens":int(u.get("completion_tokens") or 0),
                "cached_input_tokens":int(cached) if cached is not None else None}
    except (ValueError,TypeError):return None

def model_start(payload,secrets=()):
    cid=uuid.uuid4().hex[:16];now=time.monotonic()
    emit("model.start","请求模型",{"call_id":cid,"phase":model_phase()[0],
         "model":payload.get("model"),"max_output_tokens":payload.get("max_completion_tokens",payload.get("max_tokens")),
         "input_characters":len(compact(payload.get("messages",[]))),
         "request":safe({k:payload[k] for k in ("messages","tools","tool_choice","response_format","thinking") if k in payload},secrets)})
    return cid,now,model_phase()[0]
def model_finish(ticket,body,secrets=(),error=None):
    cid,started,stage=ticket
    choice=(body.get("choices") or [{}])[0] if isinstance(body,dict) and isinstance(body.get("choices",[]),list) else {}
    if not isinstance(choice,dict):choice={}
    emit("model.error" if error else "model.finish","模型请求失败" if error else "模型已返回",
         {"call_id":cid,"phase":stage,"duration_ms":round((time.monotonic()-started)*1000),
          "usage":usage_of(body) if isinstance(body,dict) else None,"finish_reason":choice.get("finish_reason"),
          "response":safe(choice.get("message",{}),secrets),"error":safe(error,secrets) if error else None})
