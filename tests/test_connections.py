import http.client
import unittest
from unittest.mock import MagicMock,patch
from urllib.request import Request
from sparky import connections

class ConnectionTests(unittest.TestCase):
    def setUp(self):
        connections.http_pool.clear();self.addCleanup(connections.http_pool.clear)
    def test_leases_are_exclusive_expire_and_bound_idle_capacity(self):
        pool=connections.IdlePool(limit=1,ttl=10)
        a,b=MagicMock(),MagicMock()
        with patch('sparky.connections.time.monotonic',return_value=1):pool.put('key',a)
        with patch('sparky.connections.time.monotonic',return_value=2):
            self.assertIs(pool.take('key'),a);self.assertIsNone(pool.take('key'))
            pool.put('key',a);pool.put('other',b)
        a.close.assert_called_once()
        with patch('sparky.connections.time.monotonic',return_value=13):self.assertIsNone(pool.take('other'))
        b.close.assert_called_once()
    @patch('sparky.connections.request.getproxies',return_value={})
    @patch('sparky.connections.select.select',return_value=([],[],[]))
    @patch('sparky.connections.http.client.HTTPSConnection')
    def test_asr_and_brain_reuse_fully_consumed_connection_but_keys_are_isolated(self,factory,select,proxy):
        con=factory.return_value;r=con.getresponse.return_value
        r.read.return_value=b'{}';r.status=200;r.will_close=False
        for path in ('audio/transcriptions','chat/completions'):
            req=Request('https://api.openai.com/v1/'+path,data=b'{}',headers={'Authorization':'Bearer fixture'})
            self.assertEqual(connections.urlopen(req,40,None).read(),b'{}')
        self.assertEqual(factory.call_count,1);self.assertEqual(con.request.call_count,2)
        connections.urlopen(Request('https://api.openai.com/v1/models',headers={'Authorization':'Bearer other'}),40,None)
        self.assertEqual(factory.call_count,2)
    @patch('sparky.connections.request.getproxies',return_value={})
    @patch('sparky.connections.http.client.HTTPSConnection')
    def test_broken_post_is_closed_without_ambiguous_retry(self,factory,proxy):
        factory.return_value.getresponse.side_effect=http.client.RemoteDisconnected()
        with self.assertRaises(OSError):connections.urlopen(Request('https://api.openai.com/v1/chat/completions',data=b'{}'),20,None)
        self.assertEqual(factory.call_count,1);factory.return_value.close.assert_called_once()
        self.assertEqual(connections.http_pool.idle,[])
    @patch('sparky.connections.request.getproxies',return_value={'https':'https://configured-proxy'})
    @patch('sparky.connections.request.urlopen')
    def test_respects_existing_proxy_configuration(self,upstream,proxy):
        req=Request('https://api.openai.com/v1/models')
        connections.urlopen(req,20,None)
        upstream.assert_called_once_with(req,timeout=20,context=None)

    @patch('sparky.connections.request.proxy_bypass',return_value=False)
    @patch('sparky.connections.request.getproxies',return_value={'https':'http://127.0.0.1:1082'})
    @patch('sparky.connections.http.client.HTTPSConnection')
    def test_reuses_configured_connect_proxy_without_bypassing_it(self,factory,proxy,bypass):
        con=factory.return_value;r=con.getresponse.return_value
        r.read.return_value=b'{}';r.status=200;r.will_close=False
        connections.urlopen(Request('https://api.openai.com/v1/models'),20,None)
        factory.assert_called_once_with('127.0.0.1',1082,timeout=20,context=None)
        con.set_tunnel.assert_called_once_with('api.openai.com',443)
