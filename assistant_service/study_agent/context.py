"""Budget complete evidence chunks and relevant, explicitly owned memory."""
from __future__ import annotations
import json
from .storage import terms, ALIASES

def evidence_payload(item):
    return {k:item[k] for k in ("id","title","kind","text") if k in item} | {
        "locator":{k:v for k,v in item.get("locator",{}).items() if k in {"page","slide","section","start","end","time_basis"}},
        "reliability":item.get("reliability","original_unchecked"),
        "selected_reference":bool(item.get("policy",{}).get("trusted")),
        "external_provenance":{k:v for k,v in item.get("source_meta",{}).items()
            if k in {"coverage","doi","provider","publication_status","family","retrieved_at"}}}

def pack_evidence(items, budget=13000):
    result=[];seen=set();used=0;dropped=[]
    for item in items:
        if item["id"] in seen:continue
        seen.add(item["id"]);clean=evidence_payload(item)
        size=len(json.dumps(clean,ensure_ascii=False,separators=(",",":")))
        if used+size>budget:
            dropped.append({"id":item["id"],"reason":"本轮上下文预算，仍可继续检索"});continue
        result.append(clean);used+=size
    return result,{"characters":used,"budget":budget,"selected":[x["id"] for x in result],"deferred":dropped}

def history_context(history,question,budget=2000):
    rows=history[-6:];result=[];used=0
    for i,row in enumerate(reversed(rows)):
        # Two recent answers support pronouns; older turns contribute topics only.
        item={"question":row.get("question","")[:280]}
        if i<2:
            item["answer"]=row.get("answer","")[:650]
            if row.get("next_question"):item["next_question"]=row["next_question"][:160]
        size=len(json.dumps(item,ensure_ascii=False))
        if used+size>budget:break
        result.insert(0,item);used+=size
    return result

def select_memory(items,question,limit=4,*,fallback=False):
    wanted=terms(question+" "+" ".join(value for key,value in ALIASES.items() if key in question))
    scored=[]
    for item in items:
        text=" ".join(str(item.get(k,"")) for k in ("title","detail","topic","question","text"))
        overlap=len(wanted & terms(text))
        if overlap or fallback:scored.append((overlap,item.get("updated_at",0),item))
    scored.sort(key=lambda v:(-v[0],-v[1]))
    return [x[2] for x in scored[:limit]]
