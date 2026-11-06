"""Course-scoped terminology candidates, not a second factual knowledge base.

Aliases are supplied by a reviewed course import. Literal source support and
active versions gate their use; related concepts belong in confusions, not aliases.
"""
from __future__ import annotations
import json,re
from .storage import digest

def normal(text):
    return ' '.join(re.sub(r'[^a-z0-9\u3400-\u9fff]+',' ',text.casefold()).split())

def contains(text,form):
    value=normal(form);haystack=normal(text)
    if not value:return False
    if re.search(r'[\u3400-\u9fff]',value):return value in haystack
    return bool(re.search(r'(?<![a-z0-9])'+re.escape(value)+r'(?![a-z0-9])',haystack))

def register_concept(store,*,course,canonical,zh,aliases,evidence_ids,confusions=None):
    """Local course preparation API; no MCP write tool or automatic fact approval."""
    forms=list(dict.fromkeys([canonical,zh,*aliases]));confusions=confusions or []
    if not course.strip() or len(course)>160 or not 2<=len(canonical)<=160 or not 1<=len(zh)<=160:
        raise ValueError('课程或术语名称超出范围')
    if len(aliases)>8 or any(not isinstance(x,str) or not 2<=len(x)<=160 for x in [canonical,*aliases]):
        raise ValueError('别名过多、过短或过长')
    if len(confusions)>6 or any(not isinstance(x,str) or not 2<=len(x)<=160 for x in confusions):
        raise ValueError('易混概念超出范围')
    if set(map(normal,forms)) & set(map(normal,confusions)):
        raise ValueError('同义词与易混概念不能重合')
    ids=list(dict.fromkeys(evidence_ids))
    if not 1<=len(ids)<=6:raise ValueError('术语需有1至6个来源片段')
    evidence=[store.evidence(eid) for eid in ids]
    if any(e['course']!=course or not e['active'] or e.get('policy',{}).get('excluded') for e in evidence):
        raise ValueError('术语来源跨课程、失效或停用')
    matched=[f for f in forms if any(contains(e['text'],f) for e in evidence)]
    # A matching Chinese label alone cannot substantiate an invented English name.
    if not any(not re.search(r'[\u3400-\u9fff]',f) for f in matched):
        raise ValueError('尚无英文名称或别名的字面来源，未建立术语映射')
    cid='concept-'+digest([course,normal(canonical)])[:32]
    return store.save('course_concept',{'course':course,'canonical':canonical,'zh':zh,'aliases':forms,
        'confusions':confusions,'evidence_ids':ids,'matched_forms':matched,
        'status':'terminology_candidate','knowledge_confirmed':False},cid)

def catalog(store,course):
    entries=[e for e in store.list('course_concept') if e['course']==course]
    if not entries:return [],0
    sources={s['id']:s for s in store.sources(course)}
    policies={(p['course'],p['origin']):p for p in store.list('source_policy')}
    ids=sorted({eid for e in entries for eid in e['evidence_ids']})
    with store.connect() as db:
        rows=db.execute('SELECT id,source_id,text FROM evidence WHERE id IN ('+','.join('?' for _ in ids)+')',ids).fetchall()
    available={r['id']:r['text'] for r in rows if r['source_id'] in sources and not
        policies.get((course,sources[r['source_id']]['origin']),{}).get('excluded')}
    result=[]
    for e in entries:
        live=[eid for eid in e['evidence_ids'] if eid in available]
        if live and any(contains(available[eid],f) for eid in live for f in e['matched_forms']):
            result.append({k:e[k] for k in ('id','course','canonical','zh','aliases','confusions','matched_forms','status','knowledge_confirmed')} |
                          {'evidence_ids':live,'notice':'名称关联候选；字面命中不等于定义或同义关系已独立核验'})
    result.sort(key=lambda e:normal(e['canonical']))
    return result,len(entries)-len(result)

def match_concepts(entries,query):
    hits=[]
    for e in entries:
        forms=[f for f in e['aliases'] if contains(query,f)]
        if forms:hits.append((e,forms))
    owners={}
    for e,forms in hits:
        for f in forms:owners.setdefault(normal(f),set()).add(e['id'])
    ambiguous=[f for f,v in owners.items() if len(v)>1]
    return hits,ambiguous

def expand_query(store,course,query):
    entries,_=catalog(store,course);hits,ambiguous=match_concepts(entries,query)
    extras=[];matched=[]
    for e,forms in hits[:4]:
        matched.append({'id':e['id'],'canonical':e['canonical'],'zh':e['zh'],'matched_forms':forms,
                        'aliases':e['aliases'],'evidence_ids':e['evidence_ids']})
        if not any(normal(f) not in ambiguous for f in forms):continue
        extras.extend(e['aliases'][:6])
    extras=list(dict.fromkeys(x for x in extras if not contains(query,x)))[:6]
    return {'query':query,'expanded_query':query+' '+ ' '.join(extras) if extras else query,
            'variants':[query,*extras],'concepts':matched,'ambiguous_forms':ambiguous,
            'notice':'同课术语关联用于检索；相关但不同的概念不合并。歧义别名不自动展开。'}

def list_concepts(store,course,offset=0,query=''):
    entries,stale=catalog(store,course)
    if query:entries=[e for e in entries if any(contains(f,query) or contains(query,f) for f in e['aliases'])]
    return {'course':course,'items':entries[offset:offset+20],'next_offset':offset+20 if offset+20<len(entries) else None,
            'total_matching':len(entries),'stale_entries':stale,
            'scope':'有当前出处的术语候选，不是完整考纲，也不代表学生已经掌握。'}

def prepare_term_practice(store,course,concept_id):
    entries,_=catalog(store,course)
    e=next((e for e in entries if e['id']==concept_id),None)
    if e is None:raise ValueError('术语不属于本课、来源失效或尚未登记，请重新列出课程术语')
    from .context import pack_evidence
    from .tutoring import public_evidence
    raw=[store.evidence(eid) for eid in e['evidence_ids']]
    packed,budget=pack_evidence(raw,6000);selected={x['id'] for x in packed}
    english=[x for x in e['aliases'] if not re.search(r'[\u3400-\u9fff]',x)]
    variants=[x for x in english if normal(x)!=normal(e['canonical'])]
    variants.sort(key=lambda x:normal(x) not in {normal(f) for f in e['matched_forms']})
    prompts=[{'kind':'definition','question':'Define '+e['canonical']+'.'},
             {'kind':'wording_transfer','question':'Explain '+(variants[0] if variants else e['canonical'])+' in your own words.'},
             {'kind':'mechanism','question':'Give an example and a boundary condition for '+e['canonical']+'.'}]
    return {'concept':e,'prompts':prompts,'confusion_check':e['confusions'],
            'evidence':[public_evidence(x) for x in raw if x['id'] in selected],'budget':budget,
            'instruction':'每次只问一题并等待作答。分别检验名称识别、机制理解和迁移，不能替学生编自评或把三题一次完成。'}

def topic_key(store,course,topic,entries=None):
    entries=catalog(store,course)[0] if entries is None else entries
    matches=[e for e in entries if normal(topic) in {normal(f) for f in e['aliases']}]
    return 'concept:'+matches[0]['id'] if len(matches)==1 else normal(topic)
