import json
import tempfile
import threading
import time
import unittest
import httpx
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from assistant_service.app import create_app
from assistant_service.settings import Settings
from assistant_service.store import Store
from assistant_service.study_agent.storage import StudyStore, digest
from assistant_service.study_agent.knowledge import set_policy, remember_claim, relevant_memory, align_class
from assistant_service.study_agent.learning import review_candidates, next_review_time, answer_feedback
from assistant_service.study_agent.reviews import create_review, update_review
from assistant_service.study_agent.operations import OperationQueue, choose_operation, validate_operation
from assistant_service.study_agent.operations import tool_schemas
from assistant_service.study_agent.reasoner import run_agent, Provider
from assistant_service.study_agent.exports import save_pack
from assistant_service.study_agent.references import public_reference_url, search_reference
from assistant_service.study_agent.planner import TZ


class Classroom:
    def __init__(self, records=None): self.values=records or []
    def records(self): return self.values
    def get(self, sid): return next(s for s in self.values if s["id"]==sid)


class FakeProvider:
    def __init__(self, evidence): self.evidence=evidence; self.calls=0
    def complete(self, messages, timeout):
        self.calls+=1
        e=self.evidence
        return json.dumps({"action":"finish","claims":[{"text":"线程共享进程的资源，但各自保留执行状态。", "citations":[{"evidence_id":e["id"],"quote":e["text"][:40]}]}],"proposals":[],"uncertainties":[]})


class OperationTests(unittest.TestCase):
    def setUp(self):
        # Windows TEMP lives under AppData, which real course-file tools reject.
        # Keep actual material fixtures in the workspace instead of weakening that boundary.
        fixtures=Path(__file__).resolve().parents[1]/".test-tmp"
        fixtures.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=fixtures); self.root=Path(self.temp.name)
        self.store=StudyStore(self.root)
        self.e=self.source()
        self.provider=FakeProvider(self.e)
        self.classroom=Classroom()
        self.queue=OperationQueue(self.store,self.provider,SimpleNamespace(root=self.root),self.classroom,threading.BoundedSemaphore(1))
    def tearDown(self): self.temp.cleanup()
    def source(self, kind="material", origin="book", text="线程共享进程地址空间资源，但是每个线程拥有独立程序计数器与栈。", locator=None, meta=None):
        r=self.store.source(origin=origin,course="操作系统",title=origin,kind=kind,fingerprint=digest(text),chunks=[{"text":text,"locator":locator or {"page":2}}],meta=meta)
        return self.store.evidence(r["id"]+":0")
    def job(self, tool, args, question="", turn="one"):
        return self.queue.submit({"tool":tool,"arguments":args},course="操作系统",question=question,turn_id=turn)
    def test_intent_routes_literal_path_batch_export_and_honours_negation(self):
        plan=choose_operation(self.provider,'读取 "C:\\课件\\OS week 2.pdf"',"操作系统")
        self.assertEqual(plan["arguments"]["path"],r"C:\课件\OS week 2.pdf")
        self.assertEqual(choose_operation(self.provider,"处理操作系统所有没处理的回放","操作系统")["tool"],"process_missing_lessons")
        self.assertIsNone(choose_operation(self.provider,"不要处理所有回放","操作系统"))
        self.assertEqual(choose_operation(self.provider,"生成线程复习 PDF，出处不足的不要编","操作系统")["tool"],"export_study_pack")
        self.assertEqual(choose_operation(self.provider,"读取教材 https://pages.cs.wisc.edu/~remzi/OSTEP/threads-intro.pdf","操作系统")["tool"],"lookup_reference")
        self.assertEqual(choose_operation(self.provider,"生成三道题 PDF 和 md","操作系统")["arguments"]["formats"],["pdf","md"])
    def test_batch_honours_a_named_new_course_and_refuses_ambiguous_scope(self):
        from assistant_service.study_agent.dialogue import converse
        result=converse(self.store,self.provider,self.root,conversation="scope",client_id="new-course",course="操作系统",
            question="帮我处理元宇宙导论尚未处理的每一节回放",operations=self.queue)
        job=self.store.get(result["result"]["operation_id"],"operation")
        self.assertEqual(job["course"],"元宇宙导论")
        self.store.source(origin="new",course="神经科学",title="neuro",kind="material",fingerprint="1",chunks=[{"text":"课堂内容","locator":{}}])
        result=converse(self.store,self.provider,self.root,conversation="scope",client_id="many-courses",course="操作系统",
            question="处理神经科学和操作系统尚未处理的全部回放",operations=self.queue)
        self.assertEqual(result["result"]["status"],"needs_course")
        self.assertNotIn("operation_id",result["result"])

    def test_tool_cannot_invent_path_or_execute_shell(self):
        with self.assertRaises(ValueError): validate_operation({"tool":"read_local_path","arguments":{"path":"C:/secret/file.txt"}},"读取我的课件")
        with self.assertRaises(ValueError): validate_operation({"tool":"run_shell","arguments":{"command":"whoami"}},"请解释线程")
        with self.assertRaises(ValueError): validate_operation({"tool":"process_missing_lessons","arguments":{}},"线程是什么意思")
        with self.assertRaises(ValueError): validate_operation({"tool":"set_review_preferences","arguments":{"daily_minutes":55}},"调一下复习时间")

    def test_native_tools_are_typed_and_credentials_never_enter_messages(self):
        settings=Settings(self.root, vault_root=self.root/"vault")
        provider=Provider(settings)
        captured=[]
        def response(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200,json={"choices":[{"message":{"tool_calls":[{"function":{"name":"set_review_preferences","arguments":'{"daily_minutes":20,"preferred_hour":19}'}}]}}]})
        provider.close()
        provider.client=httpx.Client(transport=httpx.MockTransport(response))
        with patch.object(provider,"config",return_value={"model":"test-model","base_url":"https://api.example.test","key":"test-only-secret","disable_thinking":True}):
            plan=provider.choose_tool("每天复习20分钟", "操作系统",tool_schemas())
        provider.close()
        self.assertEqual(validate_operation(plan,"每天复习20分钟")[1]["daily_minutes"],20)
        self.assertNotIn("test-only-secret",str(captured))
        self.assertEqual(captured[0]["tool_choice"],"auto")
        self.assertTrue(all(t["function"]["parameters"]["additionalProperties"] is False for t in captured[0]["tools"]))

    def test_browser_api_enforces_auth_course_lease_and_cancellation(self):
        settings=Settings(self.root,vault_root=self.root/"vault")
        classroom=Store(self.root)
        class Quiet:
            def close(self): classroom.db.close()
        app=create_app(settings,classroom,Quiet(),run_workers=False)
        q=app.state.study_operations
        job=q.submit({"tool":"process_missing_lessons","arguments":{}},course="操作系统",question="处理所有未处理的回放",turn_id="api-test")
        base="/api/study/browser/"+job["id"]
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/study/browser/work").status_code,401)
            client.headers["Authorization"]="Bearer "+settings.data["token"]
            self.assertEqual(client.post(base+"/claim",json={"client":"edge"}).status_code,200)
            self.assertEqual(client.post(base+"/claim",json={"client":"chrome"}).status_code,409)
            data={"client":"edge","course_id":"5","course_title":"其他课程","page_url":"https://video.jw.scut.edu.cn/livingroom?course_id=5&sub_id=3","items":[{"sub_id":"3","title":"第一课","status":"6"}]}
            self.assertEqual(client.post(base+"/catalogue",json=data).status_code,400)
            data["course_title"]="操作系统"
            self.assertEqual(client.post(base+"/catalogue",json=data).status_code,200)
            with q.gate:
                queued=client.post("/api/study/chat",json={"client_id":"during-export","course":"操作系统","question":"生成线程复习 PDF"})
            self.assertEqual(queued.status_code,200,queued.text)
            self.assertTrue(queued.json()["result"]["operation_id"])
            client.post("/api/study/operations/"+job["id"]+"/cancel")
            self.assertEqual(client.post(base+"/claim",json={"client":"edge"}).status_code,409)
            self.assertEqual(client.get("/api/study/browser/work").json(),[])
    def test_local_folder_import_checkpoint_and_idempotency(self):
        folder=self.root/"course";folder.mkdir();(folder/"lecture.md").write_text("线程资源课堂学习资料",encoding="utf-8")
        (folder/"api-key.txt").write_text("never import",encoding="utf-8")
        question="读取 "+str(folder)
        first=self.job("read_local_path",{"path":str(folder)},question)
        self.assertEqual(first["id"],self.job("read_local_path",{"path":str(folder)},question)["id"])
        self.queue.execute(first);result=self.store.get(first["id"])
        self.assertEqual(result["status"],"completed");self.assertEqual(len(result["items"]),1)
        self.assertFalse(self.store.search("never import","操作系统"))
    def test_source_exclusion_survives_new_version_and_old_quotes_stay(self):
        set_policy(self.store,self.e["source_id"],role="reference",excluded=True)
        self.assertEqual(self.store.search("线程","操作系统"),[])
        new=self.source(text="线程共享地址空间，新版解释保留每个线程自己的执行状态。")
        self.assertTrue(new["policy"]["excluded"])
        self.assertFalse(self.store.evidence(self.e["id"])["active"])
        self.assertIn("程序计数器",self.store.evidence(self.e["id"])["text"])
    def test_noisy_transcript_cannot_be_promoted_to_fact_or_reference(self):
        noisy=self.source(kind="asr",origin="speech",text="每个线程拥有完全独立的地址空间这是转写错误。")
        with self.assertRaises(ValueError):set_policy(self.store,noisy["source_id"],role="reference",trusted=True)
        with self.assertRaises(ValueError):remember_claim(self.store,course="操作系统",text="错误",citations=[{"evidence_id":noisy["id"],"quote":noisy["text"]}],status="confirmed")
        result=run_agent(self.store,FakeProvider(noisy),course="操作系统",question="线程地址空间",focus=[noisy])
        self.assertEqual(result["claims"][0]["verification"]["status"],"needs_check")
    def test_memory_is_relevant_bounded_and_invalidated_by_source_update(self):
        remember_claim(self.store,course="操作系统",text="线程共享地址空间",citations=[{"evidence_id":self.e["id"],"quote":self.e["text"]}],status="confirmed")
        self.assertEqual(len(relevant_memory(self.store,"操作系统","线程")),1)
        self.assertEqual(relevant_memory(self.store,"操作系统","突触"),[])
        self.source(text="新版：线程共享进程资源。")
        self.assertEqual(relevant_memory(self.store,"操作系统","线程"),[])
    def test_support_audit_filters_real_quotes_that_do_not_entail_the_explanation(self):
        class Audited(FakeProvider):
            def audit_result(self,result,evidence):
                return {"claim-0":{"status":"insufficient","reason":"原文没有支持这个额外的条件"}}
        result=run_agent(self.store,Audited(self.e),course="操作系统",question="线程")
        self.assertEqual(result["claims"],[])
        self.assertTrue(any("额外的条件" in s for s in result["uncertainties"]))
        self.assertEqual(self.store.list("knowledge"),[])
    def test_cancelled_export_does_not_continue_generating_other_questions(self):
        job=self.job("export_study_pack",{"kind":"practice","formats":["md"]},"生成练习 md")
        def answer(*args,**kwargs):
            self.queue.cancel(job["id"])
            return {"claims":[{"text":"测试"}],"support_status":"checked"}
        with patch("assistant_service.study_agent.operations.run_agent",side_effect=answer) as model:
            self.queue.execute(job)
        self.assertEqual(model.call_count,1)
        self.assertEqual(self.store.get(job["id"])["status"],"cancelled")
        self.assertFalse((self.root/"study-exports").exists())
    def test_browser_batch_skips_done_only_analyzes_existing_and_imports_missing(self):
        self.classroom.values=[{"id":"a"*32,"course_id":"1","sub_id":"10","mode":"subtitle","stopped":True,"segments":[{"text":"ok"}],"analysis_status":"complete","summary":{"partial":False}},
            {"id":"b"*32,"course_id":"1","sub_id":"11","mode":"replay","stopped":True,"segments":[{"text":"ok"}],"analysis_status":"failed"}]
        job=self.job("process_missing_lessons",{},"处理本课所有未处理的回放")
        again=self.job("process_missing_lessons",{},"处理本课所有未处理的回放",turn="two")
        self.assertEqual(job["id"],again["id"])
        rows=[{"sub_id":str(s),"title":str(s),"status":"6"} for s in (10,11,12)]
        rows.append({"sub_id":"13","title":"直播","status":"1"})
        result=self.queue.select_lessons(job["id"],course_id="1",course_title="操作系统",page_url="https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=10",items=rows)
        self.assertEqual([i["action"] for i in result["items"]],["skip","analyze","import"])
        with self.assertRaises(ValueError):self.queue.select_lessons(job["id"],course_id="1",course_title="别的课程",page_url="https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=10",items=rows)
        self.queue.cancel(job["id"]);self.assertEqual(self.queue.update(result,status="running")["status"],"cancelled")
    def test_alignment_keeps_timing_and_matches_only_same_lesson_screens(self):
        speech=self.source(kind="asr",origin="session:abc",text="讨论线程共享进程地址空间资源，线程拥有独立程序计数器与栈。",locator={"session_id":"abc","start":100,"end":150,"time_basis":"video"},meta={"sub_id":"20"})
        self.store.save("visual",{"course":"操作系统","course_url":"https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=20","items":[{"url":"https://video.jw.scut.edu.cn/play/ai3/ppt/a.jpg","seconds":95}]})
        result=align_class(self.store,"操作系统","线程地址空间")
        self.assertEqual(result[0]["status"],"candidate");self.assertEqual(result[0]["speech"]["locator"]["start"],100)
        self.assertEqual(result[0]["material"]["locator"]["page"],2);self.assertEqual(result[0]["visuals"][0]["seconds"],95)
    def test_review_budget_deadline_and_busy_time(self):
        now=datetime(2026,10,29,12,tzinfo=TZ).timestamp()
        self.store.save("review_preferences",{"daily_minutes":5,"preferred_hour":19},"review-preferences")
        cites=[{"evidence_id":self.e["id"],"quote":self.e["text"]}]
        for i in range(3):
            r=create_review(self.store,course="操作系统",topic=str(i),citations=cites,now=now-86400*3)
        reviews=review_candidates(self.store,now)
        self.assertEqual(sum(r["today"] for r in reviews),1)
        self.store.save("task",{"course":"操作系统","title":"习题提交","deadline":"2026-10-31","status":"accepted","completed":False})
        self.store.save("timetable",{"status":"accepted","busy":[{"start":"2026-10-30T18:00:00+08:00","end":"2026-10-30T20:00:00+08:00"}]})
        date=datetime.fromtimestamp(next_review_time(self.store,now,7,"操作系统"),TZ)
        self.assertEqual((date.day,date.hour,date.minute),(30,20,10))
    def test_answer_feedback_shortens_review_without_claiming_mastery(self):
        cites=[{"evidence_id":self.e["id"],"quote":self.e["text"]}]
        practice={"question":"共享什么？","citations":cites}
        provider=SimpleNamespace(complete=lambda *args:json.dumps({"summary":"资源归属说反了", "missing":["共享地址空间"],"suggested_rating":1,"citations":cites}))
        feedback=answer_feedback(self.store,provider,practice,"完全独立")
        self.assertEqual(feedback["status"],"model_checked")
        now=datetime(2026,10,29,12,tzinfo=TZ).timestamp()
        r=create_review(self.store,course="操作系统",topic="线程",citations=cites,now=now)
        r=update_review(self.store,r["id"],action="complete",rating=4,feedback=feedback,now=now)
        self.assertEqual(r["attempts"][-1]["effective_rating"],1)
        self.assertEqual(datetime.fromtimestamp(r["due_at"],TZ).day,30)
    def test_reference_rejects_private_targets_and_search_private_data(self):
        for url in ("http://127.0.0.1/a","https://foo.edu.evil.com/a","https://user:password@example.edu/a"):
            with self.assertRaises(ValueError):public_reference_url(url)
        with patch("socket.getaddrinfo",return_value=[(2,1,6,"",("127.0.0.1",443))]):
            with self.assertRaises(ValueError):public_reference_url("https://example.edu/a")
            with self.assertRaises(ValueError):public_reference_url("https://pages.cs.wisc.edu/a")
        with patch("socket.getaddrinfo",return_value=[(2,1,6,"",("198.18.0.20",443)),(10,1,6,"",("2001:2::228",443,0,0))]):
            self.assertTrue(public_reference_url("https://pages.cs.wisc.edu/a"))
            with self.assertRaises(ValueError):public_reference_url("https://unknown.edu/a")
        with self.assertRaises(ValueError):search_reference(self.store,"操作系统","C:/private/student.txt")
    def test_markdown_pdf_preserve_sources_and_uncertainty(self):
        result=run_agent(self.store,self.provider,course="操作系统",question="线程")
        files=save_pack(self.store,self.root,"a"*32,"操作系统","线程与进程",[result],["md","pdf"])
        self.assertEqual(len(files),2)
        text=(self.root/"study-exports"/("a"*32+".md")).read_text(encoding="utf-8")
        self.assertIn("尚未独立核验",text);self.assertIn("页 2",text)
        from pypdf import PdfReader
        pdf=PdfReader(self.root/"study-exports"/("a"*32+".pdf"))
        self.assertGreaterEqual(len(pdf.pages),1)


if __name__ == "__main__": unittest.main()
