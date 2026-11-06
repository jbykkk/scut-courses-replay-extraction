import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from assistant_service.study_agent.storage import StudyStore, digest
from assistant_service.study_agent.research import search_public_references, read_public_reference, prepare_evidence_comparison, validate_concept
from assistant_service.study_agent.knowledge import set_policy
from assistant_service.study_agent.mcp import Server, TOOLS
from assistant_service.study_agent.operations import choose_operation, validate_operation
from scripts.prepare_workbuddy import install_local


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.store = StudyStore(self.root)
        self.dns = patch('assistant_service.study_agent.research.public_reference_url', side_effect=lambda u: u)
        self.dns.start(); self.addCleanup(self.dns.stop); self.addCleanup(self.tmp.cleanup)

    def source(self, origin='lecture', course='操作系统', kind='asr', text='条件变量需要在循环中检查谓词。', meta=None):
        result = self.store.source(origin=origin, course=course, title='lesson', kind=kind, fingerprint=digest([text, origin]),
            chunks=[{'text': text, 'locator': {'start': 12, 'end': 25, 'path': 'private/path'}}], meta=meta or {})
        return result['id'] + ':0'

    def client(self, data, status=200):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(status, json=data)
        self.requests = []
        client = httpx.Client(transport=httpx.MockTransport(handler)); self.addCleanup(client.close)
        return client

    def paper_data(self, abstract='A condition variable is used with a predicate and a mutex.'):
        return {'message': {'items': [{'title': ['Conditions and synchronization'], 'DOI': '10.1234/test',
            'abstract': abstract, 'published': {'date-parts': [[2025]]}, 'type': 'journal-article'}]}}

    def test_short_keyword_validation_rejects_private_input(self):
        for value in ('C:/Users/example', '我的老师说条件变量总是成立', 'password sk-secret', 'alice@example.org', '202612345678'):
            with self.assertRaises(ValueError): validate_concept(value)
        self.assertEqual(validate_concept(' condition variables '), 'condition variables')

    def test_discovery_is_not_knowledge_and_cache_avoids_repeat_network(self):
        client = self.client(self.paper_data())
        a = search_public_references(self.store, 'condition variables', client=client)
        b = search_public_references(self.store, 'condition variables', client=client)
        self.assertTrue(b['cached']); self.assertEqual(len(self.requests), 1)
        self.assertIn('query.bibliographic=condition+variables', str(self.requests[0].url))
        self.assertEqual(self.store.sources(), []); self.assertEqual(self.store.list('knowledge'), [])
        self.assertTrue(any(i['coverage']=='abstract_only' for i in a['items']))

    def test_metadata_only_does_not_create_evidence(self):
        result = search_public_references(self.store, 'synchronization', client=self.client(self.paper_data('')))
        value = read_public_reference(self.store, '操作系统', result['items'][0]['id'])
        self.assertEqual(value['coverage'], 'metadata_only'); self.assertEqual(value['evidence'], [])
        self.assertEqual(self.store.sources(), [])

    def test_abstract_keeps_coverage_doi_and_pending_status(self):
        result = search_public_references(self.store, 'synchronization', client=self.client(self.paper_data()))
        read = read_public_reference(self.store, '操作系统', result['items'][0]['id'])
        e = self.store.evidence(read['evidence_ids'][0])
        self.assertEqual(e['source_meta']['coverage'], 'abstract_only')
        self.assertEqual(e['source_meta']['family'], 'doi:10.1234/test')
        self.assertFalse(e['policy']['trusted']); self.assertEqual(self.store.list('knowledge'), [])
        brief = prepare_evidence_comparison(self.store, '操作系统', '条件变量', [self.source(), e['id']])
        self.assertEqual(brief['independent_families'], 2)
        self.assertIn('摘要', brief['notices'][-1]); self.assertEqual(brief['memory_status'], 'not_saved')

    def test_same_doi_and_same_lesson_are_not_independent_sources(self):
        a = self.source('a', kind='external', meta={'doi':'10.1234/test'})
        b = self.source('b', kind='external', meta={'doi':'https://doi.org/10.1234/TEST'})
        self.assertEqual(prepare_evidence_comparison(self.store, '操作系统', '条件变量', [a, b])['independent_families'], 1)
        with self.assertRaises(ValueError):
            prepare_evidence_comparison(self.store, '操作系统', '条件变量', [a, a])
        meta={'course_id':'7', 'sub_id':'8'}
        a = self.source('c', meta=meta); b=self.source('d', kind='school_subtitle', meta=meta)
        self.assertEqual(prepare_evidence_comparison(self.store, '操作系统', '条件变量', [a, b])['independent_families'], 1)

    def test_new_search_does_not_renew_cached_body_age(self):
        found = search_public_references(self.store, 'condition variables', client=self.client(self.paper_data()))
        item = next(i for i in found['items'] if i['provider'] == 'curated_textbook')
        fetched = time.time() - 1000
        eid = self.source(item['url'], kind='external', meta={'coverage':'bounded_full_text', 'retrieved_at':fetched})
        with patch('assistant_service.study_agent.research.read_reference') as reader:
            value = read_public_reference(self.store, '操作系统', item['id'])
            reader.assert_not_called()
        self.assertEqual(value['evidence_ids'], [eid])
        self.assertEqual(self.store.evidence(eid)['source_meta']['retrieved_at'], fetched)

    def test_comparison_does_not_leak_local_origin_paths(self):
        a = self.source('C:/private/student/book.pdf', kind='material')
        b = self.source('lecture')
        brief = prepare_evidence_comparison(self.store, '操作系统', '条件变量', [a, b])
        self.assertNotIn('C:/private', json.dumps(brief))
        self.assertEqual(brief['independent_families'], 2)

    def test_focused_read_returns_bounded_source_specific_context_without_more_network(self):
        found = search_public_references(self.store, 'condition variables', client=self.client(self.paper_data()))
        item = next(i for i in found['items'] if i['provider'] == 'curated_textbook')
        chunks = [{'text': f'while wait predicate Mesa discussion {i}', 'locator': {'page': i + 1}}
                  for i in range(5)]
        source = self.store.source(origin=item['url'], course='操作系统', title='Condition variables', kind='external',
            fingerprint=digest(chunks), chunks=chunks, meta={'coverage':'bounded_full_text', 'retrieved_at':time.time()})
        # Many highly relevant records from another source must not crowd out the chosen book.
        self.store.source(origin='other', course='操作系统', title='Other', kind='material', fingerprint='other',
            chunks=[{'text':'while wait predicate Mesa', 'locator':{'page':i}} for i in range(120)])
        for indexed in (True, False):
            self.store.indexed = indexed
            with patch('assistant_service.study_agent.research.read_reference') as reader:
                result = read_public_reference(self.store, '操作系统', item['id'], query='while wait predicate Mesa')
                reader.assert_not_called()
            self.assertEqual(result['total_chunks'], 5)
            self.assertEqual(len(result['evidence']), 3)
            self.assertEqual(result['evidence_ids'], [e['id'] for e in result['evidence']])
            self.assertTrue(all(e['id'].startswith(source['id'] + ':') for e in result['evidence']))
            self.assertTrue(all(e['locator']['page'] and 'Mesa' in e['text'] for e in result['evidence']))
            self.assertNotIn('private/path', json.dumps(result))
        self.assertEqual(self.store.list('knowledge'), [])

    def test_focused_read_respects_exclusion_empty_search_and_old_clients(self):
        found = search_public_references(self.store, 'synchronization', client=self.client(self.paper_data()))
        rid = found['items'][0]['id']
        original = read_public_reference(self.store, '操作系统', rid)
        self.assertTrue(original['evidence_ids'])
        empty = read_public_reference(self.store, '操作系统', rid, query='nonexistenttopic')
        self.assertEqual(empty['evidence'], [])
        self.assertIn('不能据此断言', empty['notice'])
        set_policy(self.store, original['id'], role='reference', excluded=True)
        blocked = read_public_reference(self.store, '操作系统', rid, query='predicate')
        self.assertEqual(blocked['evidence'], [])
        self.assertEqual(self.store.search('predicate', '其他课', source_id=original['id']), [])
        self.assertEqual(self.store.search('predicate', '操作系统', source_id="' OR 1=1 --"), [])
        with self.assertRaises(ValueError):
            read_public_reference(self.store, '操作系统', rid, query='a'*201)

    def test_research_operation_marks_missing_classroom_and_bounds_context(self):
        import threading
        from types import SimpleNamespace
        from assistant_service.study_agent.operations import OperationQueue
        found = search_public_references(self.store, 'synchronization', client=self.client(self.paper_data()))
        self.source('different-concept', text='线程共享地址空间。')
        queue = OperationQueue(self.store, object(), SimpleNamespace(root=self.root), object(), threading.BoundedSemaphore(1))
        plan = {'tool':'research_topic', 'arguments':{'concept':'synchronization'}}
        for idx, local in enumerate((False, True)):
            if local: self.source('condition-lecture', text='条件变量在循环中检查谓词。')
            job = queue.submit(plan, course='操作系统', question='查文献核对条件变量', turn_id=str(idx))
            with patch('assistant_service.study_agent.operations.search_public_references', return_value=found), \
                    patch('assistant_service.study_agent.operations.run_agent', return_value={'claims':[], 'uncertainties':[]}) as reason:
                queue.execute(job)
            result = self.store.get(job['id'])
            self.assertEqual(result['status'], 'completed')
            self.assertLessEqual(len(reason.call_args.kwargs['focus']), 6)
            self.assertEqual(reason.call_args.kwargs['max_steps'], 2)
            if local:
                self.assertIn('已对照', result['message'])
            else:
                self.assertIn('缺少对应课堂原文', result['message'])
                self.assertTrue(any('没有检索到对应' in w for w in result['result']['uncertainties']))
            self.assertEqual(self.store.list('knowledge'), [])

    def test_retired_cross_course_and_excluded_evidence_fail_closed(self):
        a=self.source('a'); b=self.source('b', course='神经科学')
        with self.assertRaises(ValueError): prepare_evidence_comparison(self.store, '操作系统', 'x', [a,b])
        b=self.source('b'); set_policy(self.store,b.split(':')[0],role='transcript',excluded=True)
        with self.assertRaises(ValueError): prepare_evidence_comparison(self.store, '操作系统', 'x', [a,b])
        set_policy(self.store,b.split(':')[0],role='transcript')
        self.source('a', text='新版本内容')
        with self.assertRaises(ValueError): prepare_evidence_comparison(self.store, '操作系统', 'x', [a,b])

    def test_retraction_notice_and_expired_candidates_are_not_support(self):
        data=self.paper_data(); data['message']['items'][0]['update-to']=[{'type':'retraction'}]
        found=search_public_references(self.store,'synchronization',client=self.client(data))
        rid=found['items'][0]['id']
        with self.assertRaises(ValueError): read_public_reference(self.store,'操作系统',rid)
        item=self.store.get(rid); self.store.save('research_candidate',{**item,'retrieved_at':0},rid)
        with self.assertRaises(ValueError): read_public_reference(self.store,'操作系统',rid)

    def test_outage_and_oversize_fail_without_caching_success(self):
        client=self.client({},503)
        for _ in range(2):
            r=search_public_references(self.store,'condition variables',client=client)
            self.assertTrue(r['warnings']); self.assertTrue(r['items'])
        self.assertEqual(len(self.requests),2)
        client=self.client({'large':'x'*700000})
        self.assertTrue(search_public_references(self.store,'spectral theorem',client=client)['warnings'])

    def test_europepmc_distinguishes_preprint_and_keeps_abstract(self):
        client=self.client({'resultList':{'result':[{'id':'PPR12','source':'PPR','title':'A neural model','doi':'10.1234/neuro', 'abstractText':'<p>Model assumptions are conditional.</p>'}]}})
        r=search_public_references(self.store,'neural models','life_sciences',client=client)
        self.assertEqual(r['items'][0]['publication_status'],'preprint')
        self.assertEqual(r['items'][0]['coverage'],'abstract_only')
        self.assertIn('www.ebi.ac.uk',str(self.requests[0].url))

    def test_mcp_array_schema_status_and_private_locator(self):
        a=self.source(); s=Server(self.store)
        s.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        s.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        def call(name,args): return s.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':args}})
        self.assertFalse(call('study_connection_status',{})['result']['isError'])
        result=call('read_study_evidence',{'evidence_id':a})
        self.assertNotIn('private/path',json.dumps(result)); self.assertNotIn('sk-',json.dumps(result))
        self.assertIn('error',call('prepare_evidence_comparison',{'course':'操作系统','question':'x','evidence_ids':'not-an-array'}))
        self.assertFalse(next(t for t in TOOLS if t['name']=='read_public_reference')['annotations']['readOnlyHint'])

    def test_research_routing_uses_only_fresh_request_and_typed_arguments(self):
        class Host:
            def choose_tool(self,q,c,t):
                self.schemas=t
                return {'tool':'research_topic','arguments':{'concept':'condition variables'}}
        host=Host(); p=choose_operation(host,'查公开教材对照课堂里的条件变量','操作系统')
        self.assertEqual(p['tool'],'research_topic'); self.assertEqual(len(host.schemas),1)
        with self.assertRaises(ValueError): validate_operation(p,'老师在哪讲过条件变量')
        self.assertEqual(validate_operation(p,'查文献核对条件变量')[1]['concept'],'condition variables')

    def test_installer_preserves_other_servers_is_idempotent_and_refuses_collision(self):
        target=self.root/'workbuddy'; target.mkdir()
        path=target/'mcp.json'; path.write_text(json.dumps({'mcpServers':{'other':{'command':'existing'}}}),encoding='utf-8')
        config={'mcpServers':{'scut-study':{'command':'python','args':['C:/repo/scripts/study_mcp.py']}}}
        output=self.root/'output'; output.mkdir()
        self.assertTrue(install_local(config,output,target)['changed'])
        self.assertFalse(install_local(config,output,target)['changed'])
        self.assertEqual(json.loads(path.read_text())['mcpServers']['other']['command'],'existing')
        current=json.loads(path.read_text()); current['mcpServers']['scut-study']={'command':'different'}
        path.write_text(json.dumps(current),encoding='utf-8')
        with self.assertRaises(ValueError): install_local(config,output,target)

    def test_correction_cannot_use_mirrors_of_the_same_paper(self):
        from assistant_service.study_agent.reasoner import Final, check_citations
        a=self.source('mirror-a', kind='external', meta={'doi':'10.1234/test'})
        b=self.source('mirror-b', kind='external', meta={'doi':'10.1234/test'})
        citations=[{'evidence_id':e,'quote':self.store.evidence(e)['text']} for e in [a,b]]
        result=Final.model_validate({'action':'finish','proposals':[{'kind':'correction','title':'纠正','detail':'对照','citations':citations}]})
        with self.assertRaises(ValueError): check_citations(result,{e:self.store.evidence(e) for e in [a,b]})

    def test_trace_storage_failure_does_not_disconnect_mcp(self):
        s=Server(self.store)
        s.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})
        s.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        with patch('assistant_service.telemetry.RuntimeLog',side_effect=OSError('locked')):
            result=s.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'study_connection_status','arguments':{}}})
        self.assertFalse(result['result']['isError'])


if __name__=='__main__': unittest.main()
