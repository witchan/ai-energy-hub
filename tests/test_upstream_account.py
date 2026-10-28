import io
import json
import os
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

import server


class SharedUpstreamAccountTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.credentials = {
            'UPSTREAM_USER': 'shared-fixture-user',
            'UPSTREAM_PASS': 'shared-fixture-password',
            'UPSTREAM_PAY_PASS': 'shared-fixture-payment',
        }
        self.stack.enter_context(patch.dict(os.environ, self.credentials, clear=True))
        for name, value in self.credentials.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, 'UPSTREAM_BASE', 'https://upstream.invalid/api/v1'))
        self.stack.enter_context(patch.object(server, 'upstream_token', None))
        self.stack.enter_context(patch.object(server, 'token_expire', 0))
        self.calls = []
        self.auth_calls = []
        self.lock = threading.Lock()
        self.open = self.stack.enter_context(patch.object(server.urllib.request, 'urlopen', side_effect=self.fake_open))

    @staticmethod
    def response(data):
        return io.BytesIO(json.dumps(data).encode())

    def record(self, request):
        path = request.full_url[len(server.UPSTREAM_BASE):].split('?', 1)[0]
        item = {'path': path, 'method': request.get_method(),
                'body': json.loads(request.data) if request.data else None,
                'authorization': request.get_header('Authorization')}
        with self.lock:
            self.calls.append(item)
        return item

    def fake_open(self, request, timeout):
        item = self.record(request)
        if item['path'] == '/auth/login':
            with self.lock:
                self.auth_calls.append(item)
                number = len(self.auth_calls)
            return self.response({'code': 0, 'data': {'token': f'shared-token-{number}'}})
        return self.response({'code': 0, 'data': {'list': [], 'plans': [], 'id': 55, 'balance': 100}})

    @staticmethod
    def unauthorized(request):
        return urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {},
                                      io.BytesIO(b'{"code":401,"msg":"expired"}'))

    def test_shared_credentials_allow_each_business_combination(self):
        for giftcard, ai in ((True, True), (True, False), (False, True)):
            with self.subTest(giftcard=giftcard, ai=ai), patch.object(server, 'ENABLE_GIFTCARD', giftcard), patch.object(server, 'ENABLE_AI', ai):
                server.validate_config()

    def test_missing_shared_credentials_are_rejected_even_with_legacy_ai(self):
        legacy = {'AI_' + key: 'unused-legacy-fixture' for key in self.credentials}
        for missing in self.credentials:
            for giftcard, ai in ((True, True), (True, False), (False, True)):
                env = {**self.credentials, **legacy}
                env.pop(missing)
                with self.subTest(missing=missing, giftcard=giftcard, ai=ai), patch.dict(os.environ, env, clear=True), patch.object(server, 'ENABLE_GIFTCARD', giftcard), patch.object(server, 'ENABLE_AI', ai):
                    with self.assertRaisesRegex(RuntimeError, missing):
                        server.validate_config()

    def test_both_businesses_disabled_are_rejected(self):
        with patch.object(server, 'ENABLE_GIFTCARD', False), patch.object(server, 'ENABLE_AI', False):
            with self.assertRaisesRegex(RuntimeError, '至少启用一种业务'):
                server.validate_config()

    def test_both_catalogs_reuse_one_login(self):
        server.get_products()
        server.get_ai_plans()
        self.assertEqual(len(self.auth_calls), 1)
        self.assertEqual(self.auth_calls[0]['body'], {
            'username': self.credentials['UPSTREAM_USER'], 'password': self.credentials['UPSTREAM_PASS']})
        self.assertIsNone(self.auth_calls[0]['authorization'])
        self.assertEqual([call['authorization'] for call in self.calls[1:]], ['Bearer shared-token-1'] * 2)

    def test_concurrent_business_requests_login_once(self):
        barrier = threading.Barrier(12)
        def request(i):
            barrier.wait(timeout=5)
            return server.get_products() if i % 2 else server.get_ai_plans()
        with ThreadPoolExecutor(12) as pool:
            list(pool.map(request, range(12)))
        self.assertEqual(len(self.auth_calls), 1)
        requests = [call for call in self.calls if call['path'] != '/auth/login']
        self.assertEqual(len(requests), 12)
        self.assertTrue(all(call['authorization'] == 'Bearer shared-token-1' for call in requests))

    def test_shared_wallet_and_mutations_use_one_session_and_payment_password(self):
        server.create_order(9, 10, 1, 'level_2')
        server.create_ai_order(11)
        server.create_recharge(13)
        server.get_recharge_status(55)
        server.upstream_api('/wallet')
        server.submit_account(7, 'account@example.invalid', 'fixture-customer-password')
        server.submit_2fa(7, '123456')
        server.submit_ai_token(8, '{"fixture":true}')
        server.get_order_status(7)
        server.get_ai_order_status(8)
        self.assertEqual(len(self.auth_calls), 1)
        calls = [call for call in self.calls if call['path'] != '/auth/login']
        self.assertEqual(len(calls), 10)
        self.assertTrue(all(call['authorization'] == 'Bearer shared-token-1' for call in calls))
        orders = [call for call in calls if call['path'] in ('/orders', '/orders/ai-membership')]
        self.assertEqual(len(orders), 2)
        self.assertTrue(all(call['body']['payment_password'] == self.credentials['UPSTREAM_PAY_PASS'] for call in orders))
        self.assertEqual(next(call for call in calls if call['path'] == '/wallet/recharge')['body']['amount'], 13)

    def test_unauthorized_mutation_is_not_replayed_and_next_request_refreshes(self):
        def fail_order(request, timeout):
            if request.full_url.endswith('/orders/ai-membership'):
                self.record(request)
                raise self.unauthorized(request)
            return self.fake_open(request, timeout)
        self.open.side_effect = fail_order
        self.assertEqual(server.create_ai_order(11)['code'], 401)
        self.assertIsNone(server.upstream_token)
        self.assertEqual(sum(call['path'] == '/orders/ai-membership' for call in self.calls), 1)
        server.get_products()
        server.get_ai_plans()
        self.assertEqual(len(self.auth_calls), 2)
        self.assertEqual(self.calls[-1]['authorization'], 'Bearer shared-token-2')
        self.assertEqual(sum(call['path'] == '/orders/ai-membership' for call in self.calls), 1)

    def test_delayed_401_does_not_clear_newer_shared_session(self):
        server.upstream_token = 'old-token'
        server.token_expire = time.time() + 3600
        def stale_response(request, timeout):
            self.record(request)
            with server.token_lock:
                server.upstream_token = 'newer-token'
                server.token_expire = time.time() + 3600
            raise self.unauthorized(request)
        self.open.side_effect = stale_response
        self.assertEqual(server.get_ai_plans()['code'], 401)
        self.assertEqual(server.upstream_token, 'newer-token')
        self.open.side_effect = self.fake_open
        server.get_products()
        self.assertEqual(self.calls[-1]['authorization'], 'Bearer newer-token')
        self.assertEqual(self.auth_calls, [])

    def test_failed_login_does_not_dispatch_business_request(self):
        def fail_login(request, timeout):
            self.record(request)
            return self.response({'code': 1, 'msg': 'fixture-login-failed'})
        self.open.side_effect = fail_login
        with self.assertRaisesRegex(RuntimeError, '上游账户登录失败'):
            server.create_ai_order(11)
        self.assertEqual([call['path'] for call in self.calls], ['/auth/login'])

    def test_expired_session_refreshes_once_for_both_businesses(self):
        server.upstream_token = 'expired-token'
        server.token_expire = time.time() - 1
        server.get_ai_plans()
        server.get_products()
        self.assertEqual(len(self.auth_calls), 1)
        self.assertEqual(server.upstream_token, 'shared-token-1')
        self.assertEqual(self.calls[-1]['authorization'], 'Bearer shared-token-1')


if __name__ == '__main__':
    unittest.main()
