import json,sqlite3,tempfile,time,unittest
from pathlib import Path
from assistant_service.study_agent.storage import StudyStore
from assistant_service.study_agent.concepts import register_concept,catalog,expand_query,list_concepts,prepare_term_practice
from assistant_service.study_agent.tutoring import prepare_context,open_session,record_progress,learning_gaps
from assistant_service.study_agent.knowledge import set_policy
from assistant_service.study_agent.classroom_search import find_classroom_mentions
from assistant_service.study_agent.ingest import sync_sessions
from assistant_service.study_agent.index_state import index_status,refresh_index

class TerminologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=StudyStore(self.root)
        self.doc=self.source('slides','f-I curve maps constant current to stationary firing rate. Setup time differs from hold time. SOP differs from POS.')
        self.eid=self.doc['id']+':0'
        self.concept=register_concept(self.store,course='Neuro',canonical='neuronal gain function',zh='神经元增益函数',
            aliases=['gain function','f-I curve'],evidence_ids=[self.eid],confusions=['local slope'])
    def tearDown(self):self.tmp.cleanup()
    def source(self,origin,text,course='Neuro',fingerprint='1'):
        return self.store.source(origin=origin,course=course,title='Lecture',kind='material',fingerprint=fingerprint,
                                 chunks=[{'text':text,'locator':{'page':1}}])
    def test_equivalent_wording_retrieves_source_and_separates_confusion(self):
        value=prepare_context(self.store,'Neuro','What is a neuronal gain function?')
        self.assertEqual(value['evidence'][0]['id'],self.eid)
        self.assertIn('f-I curve',value['terminology']['expanded_query'])
        self.assertEqual(expand_query(self.store,'Neuro','local slope')['concepts'],[])
        self.assertEqual(expand_query(self.store,'Other','neuronal gain function')['expanded_query'],'neuronal gain function')
        self.assertEqual(self.store.list('knowledge'),[])
    def test_name_change_also_finds_timed_classroom_quote(self):
        s={'id':'speech','course_title':'Neuro','title':'Lecture 1','mode':'subtitle','time_basis':'video',
           'segments':[{'start':60,'end':65,'text':'The f-I curve relates current and firing rate.'}]}
        sync_sessions(self.store,[s])
        value=find_classroom_mentions(self.store,'Neuro','neuronal gain function')
        self.assertIn('f-I curve',value['items'][0]['quote'])
        self.assertEqual(value['items'][0]['start'],60)
    def test_related_words_cannot_be_declared_both_alias_and_confusion(self):
        with self.assertRaises(ValueError):register_concept(self.store,course='Neuro',canonical='SOP',zh='积之和',
            aliases=['POS'],confusions=['POS'],evidence_ids=[self.eid])
        with self.assertRaises(ValueError):register_concept(self.store,course='Neuro',canonical='invented name',zh='未知',
            aliases=[],evidence_ids=[self.eid])
    def test_aliases_are_word_bounded_and_ambiguities_do_not_expand(self):
        register_concept(self.store,course='Neuro',canonical='Setup time',zh='建立时间',aliases=['STA'],evidence_ids=[self.eid])
        register_concept(self.store,course='Neuro',canonical='Hold time',zh='保持时间',aliases=['STA'],evidence_ids=[self.eid])
        r=expand_query(self.store,'Neuro','STA')
        self.assertEqual(r['expanded_query'],'STA');self.assertEqual(r['ambiguous_forms'],['sta'])
        self.assertEqual(expand_query(self.store,'Neuro','station')['concepts'],[])
    def test_stale_or_excluded_source_stops_alias_use(self):
        self.source('slides','New document','Neuro','2')
        self.assertEqual(catalog(self.store,'Neuro'),([],1))
        with self.assertRaises(ValueError):prepare_term_practice(self.store,'Neuro',self.concept['id'])
    def test_policy_and_course_boundaries_are_enforced(self):
        other=self.source('other','f-I curve','Other')
        with self.assertRaises(ValueError):register_concept(self.store,course='Neuro',canonical='f-I curve',zh='增益',aliases=[],evidence_ids=[other['id']+':0'])
        set_policy(self.store,self.doc['id'],role='reference',excluded=True)
        self.assertFalse(expand_query(self.store,'Neuro','gain function')['concepts'])
    def test_practice_changes_wording_without_automatic_grading(self):
        r=prepare_term_practice(self.store,'Neuro',self.concept['id'])
        self.assertIn('f-I curve',r['prompts'][1]['question'])
        self.assertEqual([p['kind'] for p in r['prompts']],['definition','wording_transfer','mechanism'])
        self.assertFalse(r['concept']['knowledge_confirmed']);self.assertEqual(self.store.list('practice'),[])
    def test_latest_rating_supersedes_an_alias_of_same_concept(self):
        s=open_session(self.store,'Neuro','Learn gain','start')
        s=record_progress(self.store,s['id'],'a',0,'f-I curve',[self.eid],self_rating=2)
        s=record_progress(self.store,s['id'],'b',1,'neuronal gain function',[self.eid],self_rating=3)
        self.assertEqual(learning_gaps(self.store,time.time()+1),[])
    def test_course_refresh_keeps_other_courses_and_reports_real_freshness(self):
        a={'id':'a','course_title':'Neuro','title':'day1','mode':'subtitle','segments':[{'start':1,'end':2,'text':'f-I curve'}]}
        b={**a,'id':'b','course_title':'Other'}
        sync_sessions(self.store,[a,b])
        with sqlite3.connect(self.root/'assistant.sqlite3') as db:
            db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,data TEXT)')
            a2={**a,'id':'a2','title':'day2'}
            db.executemany('INSERT INTO sessions VALUES(?,?)',[(s['id'],json.dumps(s)) for s in [a,a2,b]])
        db.close()
        self.assertTrue(index_status(self.store,'Neuro')['needs_refresh'])
        r=refresh_index(self.store,'Neuro')
        self.assertFalse(r['after']['needs_refresh']);self.assertEqual(r['after']['saved_lessons'],2)
        self.assertTrue(any(s['origin']=='session:b' for s in self.store.sources('Other')))
        self.assertEqual(refresh_index(self.store,'Neuro')['result']['imported'],0)
        self.assertEqual(self.store.list('knowledge'),[])
    def test_missing_classroom_store_is_not_reported_as_success(self):
        self.assertFalse(index_status(self.store,'Neuro')['record_store_available'])
        with self.assertRaises(ValueError):refresh_index(self.store,'Neuro')
    def test_single_character_chinese_label_keeps_english_source_requirement(self):
        doc=self.source('library','Library declaration','DSD')
        item=register_concept(self.store,course='DSD',canonical='Library',zh='库',aliases=[],evidence_ids=[doc['id']+':0'])
        self.assertEqual(item['zh'],'库')
        self.assertEqual(expand_query(self.store,'DSD','Library')['concepts'][0]['canonical'],'Library')

if __name__=='__main__':unittest.main()
