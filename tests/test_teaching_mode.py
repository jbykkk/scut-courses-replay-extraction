import json,tempfile,unittest
from pathlib import Path
from assistant_service.study_agent.storage import StudyStore
from assistant_service.study_agent.tutoring import open_session,get_session,prepare_context,record_progress
from assistant_service.study_agent.mcp import Server

class TeachingModeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=StudyStore(Path(self.temp.name))
        self.store.source(origin='course',course='DSD',title='Types',kind='material',fingerprint='v1',chunks=[{'text':'A signed vector has a width and a two complement interpretation.','locator':{'page':1}}])
    def tearDown(self):self.temp.cleanup()
    def test_explanation_mode_survives_resume_and_changes_teaching_contract(self):
        s=open_session(self.store,'DSD','Explain all lectures','start',teaching_mode='explain')
        resumed=get_session(self.store,'DSD',s['id']);self.assertEqual(resumed['teaching_mode'],'explain')
        c=prepare_context(self.store,'DSD','signed',s['id'])
        self.assertFalse(c['teaching_contract']['assessment']['ask_for_answer'])
        self.assertNotIn('一道题',c['teaching_contract']['structure'])
        self.assertIn('不要求学生作答',c['teaching_contract']['recognition'])
        self.assertEqual(self.store.list('learning_topic'),[])
    def test_mode_change_records_preference_without_fabricated_answer_or_rating(self):
        s=open_session(self.store,'DSD','Learn','start')
        s=record_progress(self.store,s['id'],'mode-change',0,'只讲解',[],teaching_mode='explain')
        self.assertEqual(s['teaching_mode'],'explain');self.assertEqual(s['recent_steps'][0]['answer_origin'],'not_provided')
        self.assertEqual(self.store.list('learning_topic'),[])
        r=record_progress(self.store,s['id'],'mode-change',0,'只讲解',[],teaching_mode='explain')
        self.assertEqual(r['revision'],1)
        with self.assertRaises(ValueError):record_progress(self.store,s['id'],'mode-change',1,'只讲解',[],teaching_mode='interactive')
    def test_stale_revision_cannot_overwrite_preference(self):
        s=open_session(self.store,'DSD','Learn','start')
        record_progress(self.store,s['id'],'first',0,'mode',[],teaching_mode='explain')
        with self.assertRaises(ValueError):record_progress(self.store,s['id'],'second',0,'mode',[],teaching_mode='interactive')
        self.assertEqual(get_session(self.store,'DSD',s['id'])['teaching_mode'],'explain')
    def test_legacy_session_default_and_request_retries_remain_compatible(self):
        s=open_session(self.store,'DSD','Learn','start');item=self.store.get(s['id'],'learning_session')
        item.pop('teaching_mode');self.store.save('learning_session',item,s['id'])
        self.assertEqual(open_session(self.store,'DSD','Learn','start')['teaching_mode'],'interactive')
        with self.assertRaises(ValueError):open_session(self.store,'DSD','Learn','start',teaching_mode='explain')
    def test_explicit_return_to_practice_is_resumable(self):
        s=open_session(self.store,'DSD','Learn','start',teaching_mode='explain')
        s=record_progress(self.store,s['id'],'practice',0,'练习',[],teaching_mode='interactive')
        self.assertTrue(prepare_context(self.store,'DSD','signed',s['id'])['teaching_contract']['assessment']['ask_for_answer'])
    def test_mcp_rejects_unknown_mode(self):
        server=Server(self.store);server.initialized=server.ready=True
        response=server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'open_learning_session','arguments':{'course':'DSD','goal':'Learn','request_id':'bad','teaching_mode':'exam'}}})
        self.assertEqual(response['error']['code'],-32602)
        self.assertEqual(self.store.list('learning_session'),[])

if __name__=='__main__':unittest.main()
