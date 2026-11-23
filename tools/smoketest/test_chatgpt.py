"""Offline regression tests for the ChatGPT connection and assistant transport."""
import base64
import hashlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from core import agent, chatgpt_auth as auth, chatgpt_responses as responses, settings


def token_set(access='mock-access', refresh='mock-refresh', scope=auth.SCOPES):
    return {'access_token': access, 'refresh_token': refresh, 'token_type': 'Bearer',
            'expires_in': 3600, 'scope': scope, 'id_token': 'mock-identity'}


def sse(*events):
    return io.BytesIO(''.join(f'data: {json.dumps(event)}\r\n\r\n' for event in events).encode())


class ChatGPTTests(unittest.TestCase):
    client = 'oaiapp_sample'

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        self.enterContext(patch.object(settings, 'CONFIG_DIR', directory))

    def connect(self, tokens=None, client=None, subject='mock-subject'):
        client = client or self.client
        with patch.object(auth, '_browser_code', return_value=(client, 'nonce', {})), \
                patch.object(auth, 'get_json', return_value=tokens or token_set()), \
                patch.object(auth, '_identity', return_value={'sub': subject}):
            auth._sign_in('', False)

    def test_callback_pkce_state_and_client(self):
        observations = {}

        def browser(url):
            q = parse_qs(urlsplit(url).query)
            observations.update({k: v[0] for k, v in q.items()})
            callback = urlsplit(q['redirect_uri'][0])

            def respond():
                for state in (q['state'][0] + 'bad', q['state'][0]):
                    conn = http.client.HTTPConnection(callback.hostname, callback.port, timeout=2)
                    conn.request('GET', callback.path + '?' + urlencode({
                        'state': state, 'code': 'fake-code', 'client_id': self.client,
                    }))
                    result = conn.getresponse()
                    observations.setdefault('statuses', []).append(result.status)
                    result.read()
                    conn.close()

            worker = threading.Thread(target=respond, daemon=True)
            observations['worker'] = worker
            worker.start()
            return True

        with patch.object(auth.webbrowser, 'open', side_effect=browser):
            client, nonce, grant = auth._browser_code('urn:uuid:mock', {}, False, lifetime=4)
        observations['worker'].join(2)
        self.assertEqual(observations['statuses'], [400, 200])
        self.assertEqual(observations['client_id'], auth.DYNAMIC_CLIENT)
        self.assertEqual(observations['nonce'], nonce)
        self.assertEqual(grant['redirect_uri'], observations['redirect_uri'])
        self.assertEqual(client, self.client)
        digest = hashlib.sha256(grant['code_verifier'].encode()).digest()
        self.assertEqual(observations['code_challenge'],
                         base64.urlsafe_b64encode(digest).decode().rstrip('='))

    def test_identity_verifies_signature_and_nonce(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid='mock-key', use='sig', alg='RS256')
        claims = {'iss': auth.ISSUER, 'sub': 'subject', 'aud': [self.client],
                  'exp': int(time.time()) + 60, 'nonce': 'original'}
        token = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'mock-key'})
        with patch.object(auth, 'get_json', return_value={'keys': [jwk]}):
            self.assertEqual(auth._identity(token, self.client, 'original', '')['sub'], 'subject')
            with self.assertRaises(auth.ChatGPTError):
                auth._identity(token, self.client, 'different', '')
            with self.assertRaises(auth.ChatGPTError):
                auth._identity(token, 'oaiapp_wrong', 'original', '')
            other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            forged = jwt.encode(claims, other_key, algorithm='RS256', headers={'kid': 'mock-key'})
            with self.assertRaises(auth.ChatGPTError):
                auth._identity(forged, self.client, 'original', '')

    def test_missing_plan_scope_and_reuse(self):
        self.connect(token_set(scope='openid profile'))
        state = auth.status()
        self.assertTrue(state['signedIn'])
        self.assertFalse(state['permission'])
        self.assertNotIn('mock-refresh', json.dumps(state))
        with self.assertRaises(auth.ChatGPTError):
            auth.access_token(self.client)
        with patch.object(auth, '_browser_code', return_value=(self.client, 'nonce', {})) as browser, \
                patch.object(auth, 'get_json', return_value=token_set()), \
                patch.object(auth, '_identity', return_value={'sub': 'mock-subject'}):
            self.assertTrue(auth._sign_in(self.client, True))
        self.assertEqual(browser.call_args.args[1]['client_id'], self.client)
        self.assertTrue(auth.status()['permission'])

    def test_encrypted_storage_refresh_and_switch(self):
        self.connect()
        blob = (self.directory / 'chatgpt.dpapi').read_bytes()
        self.assertNotIn(b'mock-refresh', blob)
        generation = auth.status()['connection']
        with auth._locked_store() as (db, path):
            db['profiles'][self.client]['expires_at'] = 0
            auth._save(db, path)
        replacement = token_set('fresh-access', 'fresh-refresh')
        del replacement['scope']
        with patch.object(auth, 'get_json', return_value=replacement) as fetch:
            self.assertEqual(auth.access_token(self.client, generation), 'fresh-access')
            self.assertEqual(auth.access_token(self.client, generation), 'fresh-access')
            self.assertEqual(fetch.call_count, 1)
            self.assertNotIn('scope', fetch.call_args.kwargs['data'])
        self.connect(client='oaiapp_other')
        with self.assertRaises(auth.ChatGPTError):
            auth.access_token(self.client, generation)
        auth.select(self.client)
        with patch.object(auth, '_revoke', side_effect=auth.ChatGPTError('offline')) as revoke:
            self.assertFalse(auth.sign_out(self.client))
            revoke.assert_called_once_with(self.client, 'fresh-refresh')
        self.assertFalse(auth.status()['signedIn'])
        self.assertEqual(len(auth.status()['accounts']), 2)

    def test_responses_tool_roundtrip(self):
        self.connect()
        generation = auth.status()['connection']
        messages = [{'role': 'system', 'content': 'instructions'},
                    {'role': 'user', 'content': 'Find stages'}]
        tools = [agent._tool('search_stages', 'Find stages')]
        call = {'type': 'function_call', 'namespace': responses.NAMESPACE,
                'name': 'search_stages', 'call_id': 'call_123', 'arguments': '{"keyword":"OS-1"}'}
        reasoning = {'type': 'reasoning', 'id': 'rs_123', 'summary': [], 'encrypted_content': 'mock'}
        terminal = {'type': 'response.completed', 'response': {'output': [reasoning, call]}}
        connection = (self.client, generation, 'model')
        with patch.object(auth, 'open_request', return_value=sse(terminal)) as post:
            text, calls, finish, output = responses.stream(connection, messages, tools)
        self.assertEqual((text, finish, calls[0]['id']), ('', 'tool_calls', 'call_123'))
        self.assertEqual(post.call_args.args[0], auth.RESOURCE + '/responses')
        self.assertEqual(post.call_args.kwargs['token'], 'mock-access')
        payload = post.call_args.kwargs['data']
        self.assertFalse(payload['store'])
        self.assertTrue(payload['stream'])
        self.assertEqual(payload['instructions'], 'instructions')
        self.assertEqual(payload['tools'][0]['type'], 'namespace')
        self.assertNotIn('temperature', payload)
        messages.extend([{'role': 'assistant', '_chatgpt_output': output, 'tool_calls': calls},
                         {'role': 'tool', 'tool_call_id': 'call_123', 'content': '[]'}])
        result = responses._payload('model', agent._sanitize(messages), tools)['input']
        self.assertEqual(result[-3:], [reasoning, call, {
            'type': 'function_call_output', 'call_id': 'call_123', 'output': '[]',
        }])

    def test_limits_and_incomplete_stream_do_not_fallback(self):
        self.connect()
        sess = agent.Session()
        sess.chatgpt_connection = (self.client, auth.status()['connection'], 'model')
        sess.messages = [{'role': 'user', 'content': 'Hello'}]
        failed = {'type': 'response.failed', 'response': {'error': {
            'code': 'subscription_sharing_usage_limit_exceeded',
        }}}
        delta = {'type': 'response.output_text.delta', 'delta': 'partial'}
        with patch.object(auth, 'open_request', return_value=sse(delta)), \
                patch.object(agent, '_plain') as fallback:
            agent._loop(sess)
            self.assertIn('中断', sess.error)
            fallback.assert_not_called()
        with patch.object(auth, 'open_request', return_value=sse(delta, failed)), \
                patch.object(agent, '_do_call') as tool:
            agent._loop(sess)
            tool.assert_not_called()
            self.assertIn('用量上限', sess.error)
        self.assertTrue(auth.status()['limited'])
        with patch.object(auth, 'open_request') as post:
            with self.assertRaises(auth.ChatGPTError):
                responses.stream(sess.chatgpt_connection, sess.messages, [])
            post.assert_not_called()

    def test_existing_settings_default_to_key(self):
        result = settings._norm_agent({'baseUrl': 'https://api.deepseek.com/v1',
                                       'model': 'deepseek-flash', 'autoApply': True})
        self.assertEqual(result['provider'], 'api_key')
        self.assertEqual(result['model'], 'deepseek-flash')
        self.assertTrue(result['autoApply'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
