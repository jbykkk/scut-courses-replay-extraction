"""Versioned prompt files, also exposed by the local execution inspector."""
from pathlib import Path
import hashlib

ROOT = Path(__file__).parent / "prompts"
PURPOSES = {
    "classroom": "回答老师在哪些课时讲过这个问题。先用1–3条claim解释，再引用跨课时原句；引文要短且完整。教材可用于核对概念，但不能据此说老师讲过。课时、时间和回看链接由本机按引文生成，不在reply中猜写。只有转写时须保留识别误差提醒。没检索到原文只说已导入记录中未找到，不说老师没讲。exercise为null。",
    "ask": "普通追问先用1–3条带引文的解释回答关键点；只缺必要信息才追问。exercise为null。",
    "verify": "逐项比较条件、符号和结论，标出一致/冲突/待核对并给双方出处，不推测原文遗漏。exercise为null。",
    "summary": "围绕核心问题、推导关系、适用边界、未解决的疑问深入总结，避免逐句缩写。exercise为null。",
    "connect": "解释概念的因果、前置或对照关系与适用条件。connections格式为{concept_a,concept_b,relationship,citations}，每条至少两处不同证据。exercise为null。",
    "practice": "只出一道题。exercise为{question,expected_answer,citations}，参考答案和必要假设必须有原文支持。claims/proposals/connections为空；reply不透露答案，不把练习新建成任务。",
}

def read(name):
    return (ROOT / (name + ".txt")).read_text(encoding="utf-8").strip()

def study_prompt(purpose):
    # The long, stable common prefix stays identical across turns for provider caching.
    return read("study-v2") + "\n本轮任务：" + PURPOSES.get(purpose, PURPOSES["ask"])

def catalog():
    labels = {"study-v2":"学习问答 · 通用规则", "route-v1":"工具选择", "audit-v1":"出处支持度核对",
              "vision-v1":"图片识读", "feedback-v1":"练习反馈", "classroom-v1":"课堂分析",
              "classroom-review-v1":"课堂笔记 · 双语复习讲义", "events-v1":"课堂事项提取"}
    return [{"id":name, "title":title, "file":"assistant_service/study_agent/prompts/"+name+".txt",
             "text":read(name), "version":hashlib.sha256(read(name).encode()).hexdigest()[:12]}
            for name,title in labels.items() if (ROOT/(name+".txt")).exists()]
