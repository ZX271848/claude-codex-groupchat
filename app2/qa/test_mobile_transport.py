"""Transport policy tests: no installed Tailscale or provider accounts needed."""
from copy import deepcopy
from email.message import Message
import unittest
from unittest.mock import Mock

from mobile_transport import TailscaleServe, TransportError


class PrivateTransportTest(unittest.TestCase):
    def setUp(self):
        self.transport = TailscaleServe()
        self.status = {'BackendState': 'Running', 'Self': {'Online': True, 'UserID': 1,
                       'DNSName': 'test.example.ts.net.'}, 'User': {'1': {'LoginName': 'owner@example.invalid'}},
                       'CertDomains': ['test.example.ts.net']}
        self.config = {'TCP': {'8443': {'HTTPS': True}},
                       'Web': {'test.example.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8765'}}}}}
        self.transport.run = Mock(return_value='')

    def start(self, before=None, after=None):
        self.transport.read = Mock(side_effect=[self.status, before or {}, after if after is not None else self.config])
        self.transport.start(8765)

    def test_only_private_https_is_configured(self):
        self.start()
        self.transport.run.assert_called_once_with('serve', '--bg', '--https=8443', 'http://127.0.0.1:8765')
        self.assertTrue(self.transport.active)
        self.assertEqual(self.transport.login, 'owner@example.invalid')

    def test_missing_login_dns_or_https_fail_closed(self):
        for change in ('login', 'dns', 'https', 'offline'):
            with self.subTest(change=change):
                status = deepcopy(self.status)
                if change == 'login':status['User'] = {}
                if change == 'dns':status['Self']['DNSName'] = 'evil.example.com'
                if change == 'https':status['CertDomains'] = []
                if change == 'offline':status['BackendState'] = 'Stopped'
                self.transport.read = Mock(return_value=status)
                with self.assertRaises(TransportError):self.transport.start(8765)
                self.transport.run.assert_not_called()

    def test_public_and_foreground_config_rejected(self):
        for config in ({'AllowFunnel': {'other.example.ts.net:443': True}},
                       {'Foreground': {'session': {'AllowFunnel': {'x': True}}}},
                       {'Foreground': {'session': self.config}},
                       {'TCP': {'8443': {'TCPForward': '127.0.0.1:8765'}}}):
            with self.subTest(config=config):
                with self.assertRaises(TransportError):self.start(before=config)
                self.transport.run.assert_not_called()

    def test_other_handlers_never_overwritten(self):
        config=deepcopy(self.config)
        config['Web']['test.example.ts.net:8443']['Handlers']['/other']={'Text':'another service'}
        with self.assertRaises(TransportError):self.start(before=config)
        self.transport.run.assert_not_called()

    def test_configuration_is_verified_after_start(self):
        for config in ({}, {'AllowFunnel': {'x': True}}):
            with self.assertRaises(TransportError):self.start(after=config)

    def test_stop_only_removes_owned_mapping(self):
        self.start()
        self.transport.run.reset_mock()
        self.transport.read = Mock(return_value={'TCP': {'8443': {'HTTPS': True}}, 'Web': {}})
        self.transport.stop()
        self.assertFalse(self.transport.active)
        self.transport.run.assert_not_called()
        self.transport.read = Mock(return_value=self.config)
        self.transport.stop()
        self.transport.run.assert_called_once_with('serve', '--bg', '--https=8443', 'off')

    def test_identity_is_strict_and_cannot_arrive_from_lan(self):
        self.start()
        headers=Message()
        for key,value in {'Host':self.transport.host,'X-Forwarded-Host':self.transport.host,
                          'X-Forwarded-Proto':'https','Tailscale-User-Login':self.transport.login}.items():headers[key]=value
        self.assertTrue(self.transport.accepts(headers,'127.0.0.1'))
        for peer in ('192.168.1.10','100.100.100.100','::1'):
            self.assertFalse(self.transport.accepts(headers,peer))
        headers['Tailscale-User-Login']=self.transport.login
        self.assertFalse(self.transport.accepts(headers,'127.0.0.1'))
