"""Explicit same-course refresh of saved classroom records; never re-transcribes."""
from __future__ import annotations
import json,sqlite3
from contextlib import closing
from .storage import digest

def saved_sessions(store,course):
    path=store.path.parent/'assistant.sqlite3'
    if not path.is_file():return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        rows=db.execute("SELECT data FROM sessions WHERE json_extract(data,'$.course_title')=? LIMIT 501",(course,)).fetchall()
    if len(rows)>500:raise ValueError('同课记录超过500项，未用不完整快照同步；请在课堂助手管理')
    return [json.loads(r[0]) for r in rows]

def index_status(store,course):
    sessions=saved_sessions(store,course)
    indexed={s['origin']:s['fingerprint'] for s in store.sources(course) if s['origin'].startswith('session:')}
    if sessions is None:return {'record_store_available':False,'indexed_lessons':len(indexed),'needs_refresh':False,
        'notice':'仅能检查当前索引；未发现课堂记录库，不推定课程完整。'}
    current={}
    for s in sessions:
        if s.get('deleted_at') or s.get('superseded_by') or not s.get('segments'):continue
        current['session:'+s['id']]=digest(['timed-spans-v2',s['segments'],s.get('time_basis'),s.get('title'),s.get('page_url')])
    missing=sum(indexed.get(k)!=v for k,v in current.items())
    obsolete=len(set(indexed)-set(current))
    return {'record_store_available':True,'saved_lessons':len(current),'indexed_lessons':len(indexed),
            'new_or_changed_lessons':missing,'obsolete_index_entries':obsolete,'needs_refresh':bool(missing or obsolete),
            'notice':'保存、进入索引与实际课时完整性是不同状态；同步不等于重新识别或生成笔记。'}

def refresh_index(store,course):
    from .ingest import sync_sessions
    before=index_status(store,course)
    sessions=saved_sessions(store,course)
    if sessions is None:raise ValueError('本资料目录没有课堂记录库；可继续查已有资料，不能声称同步成功')
    result=sync_sessions(store,sessions,course=course)
    return {'course':course,'before':before,'result':result,'after':index_status(store,course),
            'notice':'只同步本课已保存记录；未转写、未调用模型、未改动原课堂记录或其他课程。'}
