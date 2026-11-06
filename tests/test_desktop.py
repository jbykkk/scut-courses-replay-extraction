import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from assistant_service.desktop import router, school_url
from assistant_service.settings import Settings


class Lessons:
    def list(self):
        return [{"course_title":"演示课", "page_url":"https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=2&token=private"}]


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=Settings(Path(self.tmp.name))
        self.app=FastAPI();self.app.include_router(router(self.settings,Lessons()))
        self.client=TestClient(self.app)
    def tearDown(self):
        self.client.close();self.tmp.cleanup()
    def pulse(self,**kw):
        body={"client":"browser-a","version":"0.13.0","school_tabs":1,**kw}
        return self.client.post('/api/desktop/heartbeat',json=body)
    def test_open_tab_is_not_logged_in_or_readable(self):
        self.pulse()
        s=self.client.get('/api/desktop/status').json()
        self.assertTrue(s['extension_connected']);self.assertEqual(s['readable_courses'],[])
        self.assertEqual(s['school_state'],'needs_login_or_course')
    def test_heartbeat_expires_and_cannot_survive_restart(self):
        self.pulse(courses=['演示课'])
        with patch('assistant_service.desktop.time.time',return_value=10**12):
            self.assertFalse(self.client.get('/api/desktop/status').json()['extension_connected'])
        app=FastAPI();app.include_router(router(self.settings,Lessons()))
        with TestClient(app) as c:self.assertFalse(c.get('/api/desktop/status').json()['extension_connected'])
    def test_claim_once_and_preserve_expected_course(self):
        self.pulse();r=self.client.post('/api/desktop/school',json={'course':'演示课'}).json()
        self.assertTrue(r['via_extension']);self.assertNotIn('token',r['url'])
        self.assertIn('course_id=1',r['url'])
        duplicate=self.client.post('/api/desktop/school',json={'course':'演示课'}).json()
        self.assertEqual(r['id'],duplicate['id'])
        url='/api/desktop/browser-requests/'+r['id']+'/claim'
        self.assertEqual(200,self.client.post(url,json={'client':'browser-a'}).status_code)
        self.assertEqual(409,self.client.post(url,json={'client':'browser-b'}).status_code)
        self.assertEqual([],self.client.get('/api/desktop/browser-requests').json())
        self.assertEqual('演示课',self.client.get('/api/desktop/status').json()['pending'][0]['course'])
        self.pulse(courses=['演示课'])
        self.assertEqual([],self.client.get('/api/desktop/status').json()['pending'])
    def test_no_extension_uses_direct_open_without_later_duplicate(self):
        r=self.client.post('/api/desktop/school',json={}).json()
        self.assertFalse(r['via_extension']);self.pulse()
        self.assertEqual([],self.client.get('/api/desktop/browser-requests').json())
    def test_handoff_url_is_fixed_and_sanitized(self):
        for url in ['https://evil.test/livingroom?course_id=1&sub_id=2','file:///C:/secret','https://video.jw.scut.edu.cn@evil.test/']:
            with self.assertRaises(ValueError):school_url(url)
        r=self.client.post('/api/desktop/school',json={'course':'演示课','external':True}).json()
        self.assertTrue(r['url'].startswith('https://video-jw-443.webvpn.scut.edu.cn/'))
        self.assertNotIn('token',r['url'])
    def test_first_launch_choice_persists(self):
        self.client.post('/api/desktop/welcomed')
        self.assertTrue((self.settings.root/'desktop-welcomed').is_file())

    def test_installed_versions_share_data_and_vault_outside_program(self):
        install=Path(self.tmp.name)/'program';install.mkdir();(install/'installed.json').write_text('{}')
        user=Path(self.tmp.name)/'user'
        env={**os.environ,'LOCALAPPDATA':str(user)};env.pop('SCUT_ASSISTANT_HOME',None)
        with patch('assistant_service.settings.ROOT',install),patch.dict(os.environ,env,clear=True):
            first=Settings();first.data['hotwords']='saved setting';first.save()
            second=Settings()
            self.assertEqual(second.root,user/'SCUTClassroomAssistant/data')
            self.assertEqual(second.data['hotwords'],'saved setting')
            self.assertEqual(second.data['token'],first.data['token'])
            self.assertEqual(second.vault,user/'SCUTClassroomAssistant/credentials.json')
