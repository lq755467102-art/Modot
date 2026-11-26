"""Actual HTTP failure injection through the production response boundary."""
import json
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from loop.provider import OpenAICompatProvider, Msg, ProviderError, Interrupted, FallbackProvider

OK = b'data: {"choices":[{"delta":{"content":"recovered"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


class RecoveryTests(unittest.TestCase):
    def fixture(self, responses, retries=1):
        observed=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(self.rfile.read(int(self.headers.get('Content-Length',0))))
                code, body = responses[min(len(observed)-1,len(responses)-1)]
                self.send_response(code)
                self.send_header('Content-Type','text/event-stream')
                if callable(body):
                    self.end_headers()
                    body(self)
                    return
                self.send_header('Content-Length',str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        t=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);t.start()
        self.addCleanup(lambda: (server.shutdown(),server.server_close(),t.join()))
        pv=object.__new__(OpenAICompatProvider)
        pv._opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        pv.timeout=2;pv.retries=retries;pv.recovery_seconds=2;pv.proxy='offline'
        pv._body=lambda *a,**kw: dict(messages=[dict(role='user',content='fixture')])
        pv._request=lambda body: urllib.request.Request('http://127.0.0.1:%d/'%server.server_port,data=json.dumps(body).encode())
        cm=patch('loop.context_manager.prepare',side_effect=lambda p,b,s:b);cm.start();self.addCleanup(cm.stop)
        delay=patch('loop.provider.backoff',return_value=.01);delay.start();self.addCleanup(delay.stop)
        return pv,observed

    def test_empty_embedded_error_truncation_and_503_recover(self):
        for first in ((200,b''),(200,b'data: {"error":{"type":"server_error"}}\n\n'),
                      (200,b'data: {"choices":['),(503,b'busy')):
            with self.subTest(first=first):
                pv,seen=self.fixture([first,(200,OK)])
                self.assertEqual(pv.chat_stream([Msg('user','fixture')],[]).text,'recovered')
                self.assertEqual(len(seen),2); self.assertEqual(seen[0],seen[1])

    def test_sse_request_deadline_closes_heartbeat_stream_without_replaying(self):
        disconnected = threading.Event()

        def unfinished(response):
            response.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
            until = time.monotonic() + 1.25
            while time.monotonic() < until:
                try:
                    response.wfile.write(b": keep-alive\n\n")
                except OSError:
                    disconnected.set()
                    return
                time.sleep(.02)

        provider, seen = self.fixture([(200, unfinished), (200, OK)])
        with self.assertRaises(ProviderError) as caught:
            provider.chat_stream([Msg("user", "fixture")], [], request_timeout=.3)
        self.assertEqual(caught.exception.kind, "request_deadline")
        self.assertEqual(len(seen), 1)
        self.assertTrue(disconnected.wait(.5))
        self.assertEqual(provider.chat_stream([], []).text, "recovered")

    def test_partial_tool_not_returned_and_text_is_reset(self):
        body=b'data: {"choices":[{"delta":{"content":"partial","tool_calls":[{"index":0,"function":{"name":"write_file","arguments":"{bad"}}]}}]}\n\n'
        pv,seen=self.fixture([(200,body),(200,OK)])
        deltas=[]
        result=pv.chat_stream([],[],on_delta=lambda k,t:deltas.append((k,t)))
        self.assertEqual(result.tool_calls,[]); self.assertEqual(result.text,'recovered')
        self.assertIn(('interrupted',''),deltas);self.assertEqual(len(seen),2)

    def test_auth_no_retry_and_exhaustion_is_distinct(self):
        pv,seen=self.fixture([(401,b'invalid')])
        with self.assertRaises(ProviderError) as err: pv.chat_stream([],[])
        self.assertEqual(err.exception.kind,'auth');self.assertEqual(len(seen),1)
        pv,seen=self.fixture([(200,b'')])
        with self.assertRaises(ProviderError) as err: pv.chat_stream([],[])
        self.assertEqual(err.exception.kind,'recovery_exhausted');self.assertEqual(len(seen),2)

    def test_stop_during_backoff_starts_no_new_request(self):
        pv,seen=self.fixture([(503,b'busy'),(200,OK)])
        with self.assertRaises(Interrupted): pv.chat_stream([],[],should_stop=lambda: bool(seen))
        self.assertEqual(len(seen),1)

    def test_fallback_wrapper_returns_reply_not_generator(self):
        pv,seen=self.fixture([(200,OK)])
        self.assertEqual(FallbackProvider([pv],quiet=True).chat_stream([],[]).text,'recovered')

    def test_nonstream_empty_and_bad_tool_args_recover(self):
        complete=json.dumps({'choices':[{'message':{'content':'recovered'},'finish_reason':'stop'}]}).encode()
        for broken in (b'{}',b'{broken',json.dumps({'choices':[{'message':{'tool_calls':[{'function':{'name':'edit','arguments':'{bad'}}]}}]}).encode()):
            pv,seen=self.fixture([(200,broken),(200,complete)])
            self.assertEqual(pv.chat([],[]).text,'recovered')
            self.assertEqual(len(seen),2)

    def test_recovery_deadline_and_balance_are_not_silent_empty_success(self):
        pv,seen=self.fixture([(503,b'busy')],retries=100)
        pv.recovery_seconds=.025
        with self.assertRaises(ProviderError) as error:pv.chat_stream([],[])
        self.assertEqual(error.exception.kind,'recovery_exhausted')
        self.assertLess(len(seen),10)
        pv,seen=self.fixture([(402,b'insufficient balance')])
        with self.assertRaises(ProviderError) as error:pv.chat_stream([],[])
        self.assertEqual(error.exception.kind,'balance');self.assertEqual(len(seen),1)

    def test_reasoning_only_retries_but_budget_exhaustion_does_not(self):
        for finish,kind in (('stop','empty_response'),('length','budget')):
            body=('data: '+json.dumps({'choices':[{'delta':{'reasoning_content':'thinking'},'finish_reason':finish}]})+'\n\ndata: [DONE]\n\n').encode()
            pv,seen=self.fixture([(200,body),(200,OK)])
            if kind=='empty_response':
                self.assertEqual(pv.chat_stream([],[]).text,'recovered');self.assertEqual(len(seen),2)
            else:
                with self.assertRaises(ProviderError) as error:pv.chat_stream([],[])
                self.assertEqual(error.exception.kind,kind);self.assertEqual(len(seen),1)

    def test_deepseek_thinking_history_preserves_assistant_summary_and_reasoning_fields(self):
        provider = OpenAICompatProvider(base_url='https://api.deepseek.com', api_key='fixture',
                                       model='deepseek-flash')
        provider.reasoning_effort = 'low'
        summary = '[context-update:working-summary:fixture]\nWorking summary'
        history = [Msg('user', 'User task'), Msg('assistant', 'Earlier answer', reasoning='retained'),
                   Msg('assistant', summary)]
        final = dict(choices=[dict(message=dict(content='recovered'), finish_reason='stop')])
        with patch.object(provider, '_once', return_value=final) as transport:
            self.assertEqual(provider.chat(history, []).text, 'recovered')
        sent = json.loads(transport.call_args.args[0].data)['messages']
        self.assertEqual(sent[-2]['reasoning_content'], 'retained')
        self.assertEqual(sent[-1], dict(role='assistant',content=summary,reasoning_content=''))

if __name__=='__main__': unittest.main()
