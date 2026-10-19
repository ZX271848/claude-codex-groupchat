"""Real HTTP + fake workers: never logs into a provider or touches a live room."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qa.test_coordinator import A, C, FakeLane
from conversation import Conversation
from mobile import MobileCompanion, MobileError
from mobile_transport import TailscaleServe, TransportError


class MobileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mobile-tests-')
        self.root = Path(self.tmp.name)
        self.rooms = self.root / 'rooms'
        self.project = self.root / 'project'
        self.project.mkdir()
        self.patch = patch.object(C, 'ROOMS_DIR', str(self.rooms))
        self.patch.start()
        for rid in ('desktop', 'phone'):
            path = self.rooms / rid
            path.mkdir(parents=True)
            C.save_json(str(path / 'room.json'), {'name': rid, 'project': 'test', 'project_id': 'p',
                                                  'root': str(self.project), 'members': ['gpt', 'claude', 'gemini']})
        self.app = A.App()
        self.app.room = C.room_by_dir(str(self.rooms / 'desktop'))
        self.app.last, self.app.buf = 77, {'untouched': 'desktop'}
        for rid in ('desktop', 'phone'):
            self.app.sessions[rid] = Conversation(self.app, C.room_by_dir(str(self.rooms/rid)), FakeLane,
                A.line_text, A.images_of, lambda *a, **kw: None, start=False)
        self.projects = lambda: [{'id':'p', 'name':'Test', 'root':str(self.project), 'rooms':[
            {'id':d.name,'title':d.name,'when':'today','archived':False}
            for d in self.rooms.iterdir() if (d/'room.json').is_file()]}]
        self.mobile = MobileCompanion(self.app, self.root, self.projects, A.new_room_dir)
        self.mobile.transport = TailscaleServe()
        self.mobile.transport.host = 'test.example.ts.net:8443'
        self.mobile.transport.login = 'owner@example.invalid'
        self.mobile.transport.active = True
        self.mobile.transport.start = lambda port: None
        self.mobile.transport.stop = lambda: None
        self.mobile.start(0)
        self.port = self.mobile.server.server_port
        self.host = self.mobile.transport.host
        self.cookie = ''

    def tearDown(self):
        self.mobile.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def http(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        supplied = {'Host': self.host, 'Cookie': self.cookie, 'X-Forwarded-Host': self.host,
                    'X-Forwarded-Proto': 'https', 'Tailscale-User-Login': self.mobile.transport.login}
        if body is not None:
            supplied.update({'Origin':'https://'+self.host, 'Content-Type':'application/json'})
        supplied.update(headers or {})
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, supplied)
        response = connection.getresponse()
        data, status, info = response.read(), response.status, dict(response.getheaders())
        connection.close()
        if 'application/json' in info.get('Content-Type',''):
            data = json.loads(data)
        return status, data, info

    def pair(self):
        status, data, headers = self.http('/api/pair', {'code':self.mobile.code})
        self.assertEqual(status, 200, data)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        return headers

    def command(self, action='send', **extra):
        return {'action':action,'room':'phone','request_id':f'{int(time.time()*1000)}-{secrets.token_hex(16)}',
                'text':'hello from phone','route':'gpt','images':[],**extra}

    def drain(self, rid='phone'):
        session = self.app.sessions[rid]
        while not session.inbox.empty():
            session.process(session.inbox.get_nowait())

    def test_loopback_only_and_private_identity_required(self):
        self.assertEqual(self.mobile.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.mobile.desktop_status()['urls'], ['https://'+self.host+'/'])
        for headers in ({'Tailscale-User-Login':''}, {'Tailscale-User-Login':'outsider@example.invalid'},
                        {'X-Forwarded-Proto':'http'}, {'X-Forwarded-Host':'attacker.invalid'},
                        {'Tailscale-Funnel-Request':'?1'}):
            for path in ('/','/api/snapshot','/mobile.js'):
                self.assertEqual(self.http(path,headers=headers)[0],403)
        self.pair()
        self.assertEqual(self.http('/api/snapshot',headers={'Tailscale-User-Login':''})[0],403)
        self.mobile.transport.active=False
        self.assertEqual(self.http('/api/snapshot')[0],403)

    def test_old_plaintext_pairings_are_revoked(self):
        old_token='legacy-token'
        (self.root/'mobile-devices.json').write_text(json.dumps({self.mobile.digest(old_token):time.time()+1000}))
        upgraded=MobileCompanion(self.app,self.root,self.projects,A.new_room_dir)
        with self.assertRaises(MobileError):upgraded.authenticate(old_token)
        self.assertEqual(json.loads((self.root/'mobile-devices.json').read_text())['version'],2)

    def test_transport_failure_leaves_no_listener(self):
        self.mobile.stop()
        with patch.object(self.mobile.transport,'start',side_effect=TransportError('closed')):
            with self.assertRaises(MobileError):self.mobile.start(0)
        self.assertIsNone(self.mobile.server)

    def test_auth_required_for_every_api(self):
        self.assertEqual(self.http('/')[0], 200)
        for path, body in [('/api/snapshot',None),('/api/action',self.command()),('/api/image?room=phone&path=x',None),('/api/logout',{})]:
            self.assertEqual(self.http(path, body)[0],401)
        self.assertEqual(self.http('/../app.py')[0],404)

    def test_pair_one_use_cookie_and_hashed_storage(self):
        code = self.mobile.code
        headers = self.pair()
        self.assertTrue(headers['Set-Cookie'].startswith('__Host-gc_mobile='))
        self.assertIn('Secure;', headers['Set-Cookie'])
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.http('/api/pair', {'code':code})[0],401)
        saved = (self.root/'mobile-devices.json').read_text()
        self.assertNotIn(self.cookie.split('=',1)[1], saved)
        self.assertEqual((self.root/'mobile-devices.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.http('/api/snapshot')[0],200)

    def test_pair_expiry_rate_limit_refresh(self):
        self.mobile.code_until = time.time()-1
        self.assertEqual(self.http('/api/pair', {'code':self.mobile.code})[0],401)
        for _ in range(7): self.http('/api/pair', {'code':'invalid'})
        self.mobile.refresh_code()  # refresh does not bypass rate limiting
        self.assertEqual(self.http('/api/pair', {'code':self.mobile.code})[0],429)

    def test_host_and_origin_rejected_even_with_cookie(self):
        self.pair()
        for headers in ({'Host':'attacker.example'}, {'Host':'127.0.0.1'}, {'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.http('/api/snapshot', headers=headers)[0],403)
        for origin in ('null','http://'+self.host,'https://attacker.example',''):
            self.assertEqual(self.http('/api/action', self.command(), {'Origin':origin})[0],403)
        self.assertEqual(self.http('/api/action', self.command(), {'Content-Type':'text/plain'})[0],415)

    def test_revoke_and_logout(self):
        self.pair()
        self.mobile.revoke()
        self.assertEqual(self.http('/api/snapshot')[0],401)
        self.pair()
        self.assertEqual(self.http('/api/logout',{})[0],200)
        self.assertEqual(self.http('/api/snapshot')[0],401)

    def test_two_clients_same_session_independent_cursors(self):
        self.pair()
        room = self.app.sessions['phone'].room
        C.append(room,'user','visible','client')
        C.append(room,'gpt','secret internal note','client',visibility='internal')
        first = self.http('/api/snapshot?room=phone')[1]
        self.assertEqual([r['text'] for r in first['lines']], ['visible'])
        self.assertEqual(first['cursor'],2)
        C.append(room,'gpt','public response','client')
        delta = self.http('/api/snapshot?room=phone&after=2')[1]
        full = self.http('/api/snapshot?room=phone')[1]
        self.assertEqual(len(delta['lines']),1)
        self.assertEqual(len(full['lines']),2)
        self.assertEqual(self.app.rid,'desktop')
        self.assertEqual(self.app.last,77)
        self.assertEqual(self.app.buf,{'untouched':'desktop'})
        self.assertEqual(len(self.app.sessions),2)

    def test_send_concurrent_retry_and_restart_never_redelivers(self):
        self.pair()
        body = self.command()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _:self.http('/api/action',body),range(6)))
        self.assertTrue(all(result[0]==200 for result in results))
        self.assertEqual(self.app.sessions['phone'].inbox.qsize(),1)
        self.drain()
        self.assertEqual(len(self.app.sessions['phone'].lane('gpt').asks),1)
        row = C.read_after(self.app.sessions['phone'].room,0)[0]
        self.assertEqual(row['mobile_request_id'],body['request_id'])
        self.assertEqual(self.app.sessions['desktop'].lane('gpt').asks,[])
        restarted = MobileCompanion(self.app,self.root,self.projects,A.new_room_dir)
        self.assertTrue(restarted.action(body)['recorded'])
        self.assertTrue(self.app.sessions['phone'].inbox.empty())
        self.assertEqual(self.http('/api/action',{**body,'text':'different'})[0],409)

    def test_uncertain_admission_after_restart_is_not_replayed(self):
        body = self.command()
        self.mobile.action(body)  # queued but worker has not yet recorded it
        restarted = MobileCompanion(self.app,self.root,self.projects,A.new_room_dir)
        with self.assertRaises(MobileError) as caught: restarted.action(body)
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(self.app.sessions['phone'].inbox.qsize(),1)

    def test_mode_stop_and_new_room_do_not_change_desktop_selection(self):
        self.mobile.action(self.command('mode',mode='collaboration'))
        self.drain()
        self.assertEqual(self.app.sessions['phone'].mode,'collaboration')
        self.assertEqual(self.app.sessions['desktop'].mode,'normal')
        self.mobile.action(self.command())
        self.drain()
        self.mobile.action(self.command('stop',model='gpt'))
        self.drain()
        self.assertFalse(self.app.sessions['phone'].lane('gpt').busy)
        body = self.command('new_room',project='p')
        one = self.mobile.action(body)
        self.assertEqual(one,self.mobile.action(body))
        two = self.mobile.action(self.command('new_room',project='p'))
        self.assertNotEqual(one['room'],two['room'])
        self.assertEqual(self.app.rid,'desktop')

    def test_room_traversal_and_archived_room(self):
        for rid in ('../project','phone/..','..','/tmp',None):
            with self.assertRaises(MobileError):self.mobile.snapshot(rid,0) if rid else self.mobile.room(rid)
        (self.rooms/'link').symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(MobileError):self.mobile.room('link')
        C.save_json(str(self.rooms/'phone/room.json'),{'archived':True})
        with self.assertRaises(MobileError):self.mobile.snapshot('phone')

    def test_attachment_scope_and_image_symlink_escape(self):
        data=b'\x89PNG\r\n\x1a\nexample'
        result=self.mobile.action(self.command('attach',data='data:image/png;base64,'+base64.b64encode(data).decode()))
        self.assertIn('name',result)
        received,kind=self.mobile.image('phone','attachments/'+result['name'])
        self.assertEqual(received,data)
        self.assertEqual(kind,'image/png')
        self.assertIn('error',self.mobile.action(self.command(images=['../../secret.png'])))
        outside=self.root/'private.png';outside.write_bytes(data)
        (self.project/'escape.png').symlink_to(outside)
        with self.assertRaises(MobileError):self.mobile.image('phone','escape.png')
        self.assertIn('error',self.mobile.action(self.command('attach',data='data:image/svg+xml;base64,AAAA')))

    def test_unknown_actions_and_expired_requests(self):
        self.assertIn('error',self.mobile.action(self.command('mobile_control')))
        with self.assertRaises(MobileError):self.mobile.action(self.command(request_id='0-'+secrets.token_hex(16)))


if __name__ == '__main__':
    unittest.main()
