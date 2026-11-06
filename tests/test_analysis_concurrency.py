import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_service.analysis import Analyzer
from assistant_service.manager import Manager
from assistant_service.settings import Settings
from assistant_service.store import Store
from assistant_service.telemetry import current


class AnalysisConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name))
        self.store = Store(self.settings.root)
        self.manager = Manager(self.settings, self.store)
        self.settings.data['analysis_parallelism'] = 3

    def tearDown(self):
        self.manager.close()
        self.store.db.close()
        self.temp.cleanup()

    def lesson(self, count=3):
        sid = self.store.create(dict(mode='subtitle', course_id='1', sub_id='2'))['id']
        self.store.update(sid, stopped=True, status='complete', segments=[
            dict(id=str(i), start=i*60, end=(i+1)*60, text='x'*6200) for i in range(count)])
        self.store.enqueue(sid, 'summary', {}, lane='analysis')
        return sid

    @staticmethod
    def result(pack):
        s = pack[0]
        return dict(overview=s['id'], events=[], topics=[dict(title=s['id'], start=s['start'],
                    end=s['end'], detail='test', segment_ids=[s['id']])])

    def test_parallel_parts_save_in_completion_order_and_publish_in_video_order(self):
        sid = self.lesson(6)
        barrier = threading.Barrier(3)
        release_first, saved = threading.Event(), threading.Event()
        lock, active, peak, traces = threading.Lock(), 0, 0, set()
        errors = []
        def analyze(pack, **kwargs):
            nonlocal active, peak
            index = int(pack[0]['id'])
            with lock:
                active += 1
                peak = max(peak, active)
                traces.add(current().id)
            try:
                if index < 3:
                    barrier.wait(timeout=10)
                if index == 0:
                    if not release_first.wait(10):
                        raise RuntimeError('first part was never released')
                return self.result(pack)
            finally:
                with lock:
                    active -= 1
        real_update = self.store.update
        def update(*args, **kwargs):
            result = real_update(*args, **kwargs)
            if kwargs.get('summary', {}).get('partial'):
                saved.set()
            return result
        def execute():
            try:
                self.manager.execute(self.store.claim('analysis'))
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.manager.analyzer, 'analyze', side_effect=analyze), \
             patch.object(self.manager.analyzer, 'synthesize', return_value=dict(title='test', overview='test', groups=[])) as outline, \
             patch.object(self.manager.analyzer, 'consolidate', return_value=[]), \
             patch.object(self.store, 'update', side_effect=update):
            thread = threading.Thread(target=execute)
            thread.start()
            try:
                self.assertTrue(saved.wait(10), 'parallel parts did not save while the first part waited')
                partial = self.store.get(sid)
                self.assertTrue(partial['summary']['partial'])
                self.assertNotEqual(partial['summary']['topics'][0]['title'], '0')
            finally:
                release_first.set()
                thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([t['title'] for t in outline.call_args.args[0]], list(map(str, range(6))))
        session = self.store.get(sid)
        self.assertEqual(peak, 3)
        self.assertEqual(len(traces), 1, 'parallel requests lost their parent execution trace')
        self.assertEqual(session['analysis_status'], 'complete')
        self.assertEqual([t['title'] for t in session['summary']['topics']], list(map(str, range(6))))
        self.assertEqual(session['analysis_done'], 6)

    def test_failed_part_keeps_other_inflight_results_and_retries_only_the_missing_part(self):
        sid = self.lesson()
        barrier = threading.Barrier(3)
        def analyze(pack, **kwargs):
            barrier.wait(timeout=10)
            if pack[0]['id'] == '0':
                raise ValueError('temporary failure')
            return self.result(pack)
        with patch.object(self.manager.analyzer, 'analyze', side_effect=analyze):
            self.manager.execute(self.store.claim('analysis'))
        self.assertEqual(self.store.get(sid)['analysis_done'], 2)
        self.assertEqual(self.store.get(sid)['analysis_status'], 'failed')
        cached = json.loads(next((self.settings.root/'sessions'/sid/'analysis-cache').glob('*.json')).read_text())
        self.assertEqual(set(cached), {'1', '2'})
        self.store.enqueue(sid, 'summary', {}, lane='analysis')
        with patch.object(self.manager.analyzer, 'analyze', side_effect=lambda pack, **kw: self.result(pack)) as analyze, \
             patch.object(self.manager.analyzer, 'synthesize', return_value=dict(title='test', overview='test', groups=[])), \
             patch.object(self.manager.analyzer, 'consolidate', return_value=[]):
            self.manager.execute(self.store.claim('analysis'))
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(analyze.call_args.args[0][0]['id'], '0')
        self.assertEqual(self.store.get(sid)['analysis_status'], 'complete')

    def test_more_courses_start_without_restart_and_lowering_limit_keeps_active_work(self):
        self.settings.data['analysis_workers'] = 2
        ids = []
        for i in range(4):
            sid = self.store.create(dict(mode='subtitle', course_id='1', sub_id=str(i)))['id']
            self.store.update(sid, stopped=True, status='complete', segments=[
                dict(id=str(i), start=0, end=60, text='test')])
            self.store.enqueue(sid, 'summary', {}, lane='analysis')
            ids.append(sid)
        condition, release = threading.Condition(), threading.Event()
        started, completed = set(), set()
        def analyze(pack, **kwargs):
            with condition:
                started.add(pack[0]['id'])
                condition.notify_all()
            if not release.wait(15):
                raise RuntimeError('test did not release the model request')
            return self.result(pack)
        real_update = self.store.update
        def update(sid, **kwargs):
            value = real_update(sid, **kwargs)
            if kwargs.get('analysis_status') == 'complete':
                with condition:
                    completed.add(sid)
                    condition.notify_all()
            return value
        with patch.object(self.settings, 'key', return_value='test-key'), \
             patch.object(self.manager.analyzer, 'analyze', side_effect=analyze), \
             patch.object(self.manager.analyzer, 'synthesize', return_value=dict(title='test', overview='test', groups=[])), \
             patch.object(self.manager.analyzer, 'consolidate', return_value=[]), \
             patch.object(self.manager.mobile, 'start'), \
             patch.object(self.store, 'update', side_effect=update):
            try:
                self.manager.start()
                with condition:
                    self.assertTrue(condition.wait_for(lambda: len(started) == 2, timeout=10))
                self.settings.data['analysis_workers'] = 4
                self.manager.configure_workers()
                with condition:
                    self.assertTrue(condition.wait_for(lambda: len(started) == 4, timeout=10))
                self.settings.data['analysis_workers'] = 1
                self.manager.configure_workers()
                release.set()
                with condition:
                    self.assertTrue(condition.wait_for(lambda: len(completed) == 4, timeout=10))
                self.assertEqual(completed, set(ids))
            finally:
                release.set()
                self.manager.shutdown.set()
                for thread in self.manager.threads:
                    thread.join(10)

    def test_settings_api_validates_and_persists_concurrency(self):
        from fastapi.testclient import TestClient
        from assistant_service.app import create_app
        client = TestClient(create_app(self.settings, self.store, self.manager, run_workers=False))
        client.headers['Authorization'] = 'Bearer ' + self.settings.data['token']
        try:
            response = client.post('/api/settings', json=dict(analysis_workers=6, analysis_parallelism=4))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['analysis_workers'], 6)
            self.assertEqual(Settings(self.settings.root).data['analysis_parallelism'], 4)
            for invalid in [dict(analysis_workers=0), dict(analysis_workers=9), dict(analysis_parallelism=0), dict(analysis_parallelism=5)]:
                self.assertEqual(client.post('/api/settings', json=invalid).status_code, 422)
            self.assertEqual(self.settings.data['analysis_workers'], 6)
        finally:
            client.close()

    def test_pool_replacement_waits_for_other_requests_before_closing_old_client(self):
        analyzer = self.manager.analyzer
        with analyzer._borrow_client('openai') as old:
            analyzer._reset_client('openai')
            self.assertFalse(old.is_closed)
            self.assertIsNot(analyzer._client('openai'), old)
        self.assertTrue(old.is_closed)
        self.assertFalse(analyzer.retired_clients)


if __name__ == '__main__':
    unittest.main()
