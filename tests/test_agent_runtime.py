import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from assistant_service.app import create_app
from assistant_service.settings import Settings
from assistant_service.telemetry import RuntimeLog, execution, emit, phase, safe, usage_of
from assistant_service.study_agent.context import history_context, pack_evidence, select_memory
from assistant_service.study_agent.ingest import import_file
from assistant_service.study_agent.knowledge import set_policy, remember_claim, relevant_memory
from assistant_service.study_agent.operations import choose_operation, OperationQueue
from assistant_service.study_agent.reasoner import Provider, run_agent
from assistant_service.study_agent.storage import StudyStore


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.settings=Settings(self.root,vault_root=self.root/'vault')
        self.store=StudyStore(self.root)

    def tearDown(self):self.temp.cleanup()

    def source(self,text="线程共享进程地址空间。",fingerprint="v1",origin="book",course="操作系统"):
        value=self.store.source(origin=origin,course=course,title="教材",kind="material",fingerprint=fingerprint,
                                chunks=[{"text":text,"locator":{"page":4}}])
        return self.store.evidence(value['id']+':0')

    def provider(self, handler):
        p=Provider(self.settings,httpx.MockTransport(handler))
        p.config=lambda:{'model':'test','base_url':'https://model.test','key':'private-test-secret','disable_thinking':True}
        self.addCleanup(p.close);return p

    def test_trace_is_readable_before_run_finishes_and_cursors_are_incremental(self):
        log=RuntimeLog(self.root)
        with execution(self.root,'chat','问题','操作系统') as run:
            emit('tool.start','检索',{'arguments':{'query':'线程'}})
            snap=log.detail(run.id)
            self.assertEqual(snap['status'],'running')
            last=snap['events'][-1]['seq']
            self.assertEqual(log.detail(run.id,last)['events'],[])
            emit('tool.finish','完成',{'count':1})
            self.assertEqual(len(log.detail(run.id,last)['events']),1)
        self.assertEqual(log.detail(run.id)['metrics']['tool_calls'],1)

    def test_requests_usage_and_secret_redaction(self):
        calls=[]
        def reply(request):
            calls.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"action":"finish"}','reasoning_content':'hidden-private-reasoning'}}],
                'usage':{'prompt_tokens':120,'completion_tokens':20,'prompt_cache_hit_tokens':96}})
        provider=self.provider(reply)
        client=provider.client
        with execution(self.root,'chat','检查') as run:
            with phase('audit',800):
                for _ in range(2):provider.complete([{'role':'system','content':'规则 private-test-secret'},{'role':'user','content':'hi'}],10)
        detail=RuntimeLog(self.root).detail(run.id);serialized=json.dumps(detail)
        self.assertNotIn('private-test-secret',serialized);self.assertNotIn('hidden-private-reasoning',serialized)
        starts=[e for e in detail['events'] if e['kind']=='model.start']
        self.assertEqual(starts[0]['data']['max_output_tokens'],800)
        self.assertEqual(detail['metrics']['input_tokens'],240)
        self.assertEqual(detail['metrics']['cached_input_tokens'],192)
        self.assertEqual(detail['metrics']['model_calls'],2)
        self.assertIs(provider.client,client);self.assertEqual(len(calls),2)

    def test_unknown_usage_stays_unknown_and_images_not_logged(self):
        self.assertIsNone(usage_of({'choices':[]}))
        self.assertIsNone(usage_of({'usage':{'prompt_tokens':10,'completion_tokens':5}})['cached_input_tokens'])
        value=safe({'api_key':'private','image_url':{'url':'data:image/png;base64,aaaa'},'reasoning_content':'private'})
        self.assertEqual(value['api_key'],'[redacted]');self.assertNotIn('reasoning_content',value)
        self.assertNotIn('base64',value['image_url']['url'])

    def test_concurrent_runs_do_not_share_context(self):
        ids=[]
        def work(name):
            with execution(self.root,'chat',name) as run:
                ids.append(run.id);emit('marker',name,{'owner':name})
        threads=[threading.Thread(target=work,args=(x,)) for x in ('first','second')]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        log=RuntimeLog(self.root)
        for rid in ids:
            d=log.detail(rid);self.assertEqual([e['data']['owner'] for e in d['events'] if e['kind']=='marker'],[d['label']])

    def test_exact_support_cache_invalidates_on_source_text_model_and_prompt(self):
        calls=[]
        def reply(request):
            calls.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'checks':[{'id':'claim-0','status':'supported','reason':'原文支持'}]})}}]})
        provider=self.provider(reply);item=self.source();c={'evidence_id':item['id'],'quote':'线程共享进程地址空间'}
        result={'claims':[{'text':'线程共享地址空间','citations':[c]}]}
        self.assertEqual(provider.audit_result(result,{item['id']:item}),provider.audit_result(result,{item['id']:item}))
        self.assertEqual(len(calls),1)
        provider.audit_result(result,{item['id']:{**item,'text':item['text']+'仅限同一进程。'}})
        self.assertEqual(len(calls),2)
        with patch('assistant_service.study_agent.reasoner.read_prompt',return_value='更新的检查规则'):
            provider.audit_result(result,{item['id']:item})
        self.assertEqual(len(calls),3)
        old=provider.config();provider.config=lambda:{**old,'model':'other'}
        provider.audit_result(result,{item['id']:item});self.assertEqual(len(calls),4)

    def test_invalid_checks_are_never_cached(self):
        calls=[]
        provider=self.provider(lambda r:(calls.append(r) or httpx.Response(200,json={'choices':[{'message':{'content':'{"checks":[]}'}}]})))
        item=self.source();result={'claims':[{'text':'线程共享','citations':[{'evidence_id':item['id'],'quote':'线程共享'}]}]}
        for _ in range(2):
            with self.assertRaises(ValueError):provider.audit_result(result,{item['id']:item})
        self.assertEqual(len(calls),2)

    def test_context_deduplication_budget_and_memory_selection(self):
        item=self.source();items,stats=pack_evidence([item,item],4000)
        self.assertEqual(len(items),1);self.assertEqual(items[0]['text'],item['text'])
        self.assertNotIn('source_meta',items[0]);self.assertNotIn('origin',items[0])
        empty,_=pack_evidence([item],1);self.assertEqual(empty,[])
        history=history_context([{'question':'question '+str(i),'answer':'x'*1600} for i in range(6)],'question')
        self.assertLessEqual(len(json.dumps(history,ensure_ascii=False)),2100)
        self.assertEqual(sum('answer' in h for h in history),2)
        memory=select_memory([{'title':'线程要求'},{'title':'神经元'}],'线程是什么意思')
        self.assertEqual(len(memory),1)

    def test_followup_search_does_not_repeat_source_text_in_prompt(self):
        item=self.source();messages=[]
        class Fake:
            def complete(inner,payload,timeout):
                messages.append(json.loads(payload[-1]['content']) if payload[-1]['content'].startswith('{') else {})
                return json.dumps({'action':'search','query':'线程'} if len(messages)==1 else {'action':'finish','claims':[]})
        result=run_agent(self.store,Fake(),course='操作系统',question='线程',focus=[item])
        self.assertEqual(len(messages[0]['source_data']),1)
        self.assertEqual(messages[1]['source_data'],[])
        self.assertIn(item['id'],messages[1]['already_read'])
        with self.store.connect() as db:saved=json.loads(db.execute('SELECT data FROM runs').fetchone()[0])
        self.assertNotIn('evidence',saved);self.assertIn(item['id'],saved['evidence_ids'])
        self.assertTrue(result['trace_id'])

    def test_index_survives_restart_and_respects_new_versions_courses_and_exclusions(self):
        old=self.source();self.assertTrue(self.store.indexed)
        self.source('线程不能跨课程引用',origin='other',fingerprint='other',course='其他课')
        new=self.source('线程共享资源的新版本',fingerprint='v2')
        self.store=StudyStore(self.root)
        hits=self.store.search('线程','操作系统')
        self.assertEqual([e['id'] for e in hits],[new['id']])
        self.assertFalse(self.store.evidence(old['id'])['active'])
        set_policy(self.store,new['source_id'],role='reference',excluded=True)
        self.assertEqual(self.store.search('线程','操作系统'),[])

    def test_existing_database_backfill_and_fallback(self):
        item=self.source()
        with self.store.connect() as db:db.execute('DROP TABLE evidence_search')
        store=StudyStore(self.root);indexed=store.search('线程','操作系统')
        store.indexed=False;self.assertEqual(indexed,store.search('线程','操作系统'))
        self.assertEqual(indexed[0]['id'],item['id'])

    def test_unchanged_file_skips_document_extraction(self):
        path=self.root/'material.txt';path.write_text('线程共享地址空间。',encoding='utf-8')
        import_file(self.store,self.root,path.name,'操作系统')
        with patch('assistant_service.study_agent.ingest.extract',side_effect=AssertionError('repeated extract')):
            self.assertTrue(import_file(self.store,self.root,path.name,'操作系统')['duplicate'])

    def test_stale_and_superseded_memory_is_not_retrieved(self):
        item=self.source();c=[{'evidence_id':item['id'],'quote':'线程共享进程地址空间'}]
        memory=remember_claim(self.store,course='操作系统',text='线程共享地址空间',citations=c,status='confirmed')
        self.assertEqual(len(relevant_memory(self.store,'操作系统','线程')),1)
        self.source('线程的新说明',fingerprint='v2')
        self.assertEqual(relevant_memory(self.store,'操作系统','线程'),[])

    def test_explicit_preference_does_not_use_model(self):
        class Never:
            def choose_tool(self,*a):raise AssertionError('unnecessary model request')
        p=choose_operation(Never(),'每天复习 20 分钟，晚上 8 点','操作系统')
        self.assertEqual(p['arguments'],{'daily_minutes':20,'preferred_hour':20})
        self.assertIsNone(choose_operation(None,'每天复习20分钟合适吗？','操作系统'))

    def test_runtime_endpoints_require_auth_and_return_prompt_catalog(self):
        app=create_app(settings=self.settings,run_workers=False)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/study/runtime').status_code,401)
            client.headers['Authorization']='Bearer '+self.settings.data['token']
            data=client.get('/api/study/runtime/catalog').json()
            self.assertTrue(any(x['id']=='study-v2' for x in data['prompts']))
            self.assertTrue(data['tools']);self.assertNotIn(self.settings.data['token'],json.dumps(data))
            self.assertEqual(client.get('/study-runtime.js').status_code,200)
            self.assertEqual(client.get('/study-runtime.css').status_code,200)
            with patch.object(app.state.store,'jobs',side_effect=AssertionError('heartbeat loaded full jobs')):
                self.assertTrue(client.get('/api/health?brief=true').json()['ok'])
        app.state.store.db.close()

    def test_failed_operation_is_not_marked_completed(self):
        queue=OperationQueue(self.store,None,self.settings,None,threading.BoundedSemaphore(1))
        job=self.store.save('operation',{'tool':'invalid','arguments':{},'course':'课','question':'测试','status':'queued'})
        result=queue.execute(job)
        self.assertEqual(RuntimeLog(self.root).detail(result['trace_id'])['status'],'failed')

    def test_browser_batch_keeps_progress_in_its_own_persistent_trace(self):
        queue=OperationQueue(self.store,None,self.settings,None,threading.BoundedSemaphore(1))
        job=queue.submit({'tool':'process_missing_lessons','arguments':{}},course='操作系统',question='处理本课未处理的回放',turn_id='test')
        log=RuntimeLog(self.root);self.assertEqual(log.state(job['trace_id']),'running')
        count=len(log.detail(job['trace_id'])['events'])
        queue.update(job,message=job['message'])
        self.assertEqual(len(log.detail(job['trace_id'])['events']),count)
        queue.update(job,items=[{'sub_id':'123','status':'done','title':'第一节'}],status='completed',message='1/1 已完成')
        self.assertEqual(log.state(job['trace_id']),'completed')
        self.assertEqual(log.detail(job['trace_id'])['events'][-2]['data']['changed_items'][0]['sub_id'],'123')

    def test_large_snapshots_are_compressed_and_roundtrip_without_truncating_sources(self):
        log=RuntimeLog(self.root);data={'source':'完整原文。'*6000}
        with execution(self.root,'chat','长输入') as run:emit('context.test','原文',data)
        record=next(e for e in log.detail(run.id)['events'] if e['kind']=='context.test')
        self.assertEqual(record['data'],data)
        with log.db() as db:raw=db.execute("SELECT data FROM runtime_events WHERE kind='context.test'").fetchone()[0]
        self.assertLess(len(raw),len(json.dumps(data,ensure_ascii=False))/4)


if __name__=='__main__':unittest.main()
