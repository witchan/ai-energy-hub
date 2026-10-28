import http.client
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from decimal import Decimal
from unittest.mock import patch

import server


class PercentagePriceTest(unittest.TestCase):
    def test_gift_country_visibility_uses_country_not_currency_or_product_id(self):
        for product in [{'name': 'Apple/iTunes 🇹🇷 (土耳其)'}, {'name': 'Apple (土耳其)'},
                        {'name': 'Apple 🇹🇷'}, {'name': 'Apple (Turkey)'},
                        {'name': 'Apple (Türkiye)'}, {'country_name': 'Turkiye'},
                        {'country_code': 'tr'}, {'country': 'TR'}, {'group_tab': '土耳其'}]:
            with self.subTest(product=product):
                self.assertFalse(server.giftcard_visible(product))
        for product in [{'name': 'Apple/iTunes 🇺🇸 (美国)'}, {'name': 'Apple/iTunes 🇯🇵 (日本)'},
                        {'name': 'Apple/iTunes 🇬🇧 (英国)'}, {'id': 1062954, 'symbol': '₺'},
                        {'name': None, 'country': None}, {}]:
            with self.subTest(product=product):
                self.assertTrue(server.giftcard_visible(product))

    def test_percentage_markup_examples(self):
        for cost, rate, expected in [
            ('99.50', '3', 102.50), ('123.45', '5', 130.45),
            ('10', '3', 11), ('80', '2.5', 82), ('33.33', '3', 34.33),
            ('333.34', '3', 344.34), ('0.01', '0.001', 1.01), ('19.99', '0', 19.99),
        ]:
            with self.subTest(cost=cost, rate=rate):
                self.assertEqual(server.sale_price(cost, rate), expected)

    def test_only_markup_is_rounded_to_whole_yuan(self):
        self.assertEqual(server.sale_price('100.01', '3'), 104.01)
        self.assertEqual(server.sale_price('100.99', '3'), 104.99)

    def test_integer_markup_is_not_rounded_up_again(self):
        self.assertEqual(server.sale_price('100', '7'), 107)
        self.assertEqual(server.sale_price('200', '0.5'), 201)

    def test_decimal_percentage_precision_at_integer_boundary(self):
        self.assertEqual(server.sale_price('200', '0.50000000000000001'), 202)
        self.assertEqual(server.sale_price('200', '0.49999999999999999'), 201)

    def test_currency_rounding_precedes_percentage_calculation(self):
        self.assertEqual(server.sale_price('100.004', '3'), 103)
        self.assertEqual(server.sale_price('100.005', '3'), 104.01)

    def test_invalid_percentage_values_are_rejected(self):
        for rate in ('NaN', 'Infinity', '-Infinity', 'bad', '3%', None, True):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                server.sale_price(100, rate)

    def test_negative_percentage_discounts_with_signed_ceiling(self):
        for cost, rate, expected in [('100', '-3', 97), ('99.50', '-3', 97.50),
                                     ('100.01', '-3', 97.01), ('123.45', '-5', 117.45),
                                     ('80', '-2.5', 78), ('10', '-0.001', 10)]:
            with self.subTest(cost=cost, rate=rate):
                self.assertEqual(server.sale_price(cost, rate), expected)

    def test_discount_requires_positive_cost_and_valid_rate(self):
        for cost in ('100', '100.01', '0.01'):
            for rate in ('-100', '-100.001', '-200'):
                with self.subTest(cost=cost, rate=rate), self.assertRaises(ValueError):
                    server.sale_price(cost, rate)
        for cost in ('0', '-0.01', '-100'):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                server.sale_price(cost, -3)

    def test_negative_configuration_needs_no_opt_in(self):
        credentials = {'UPSTREAM_USER': 'fixture', 'UPSTREAM_PASS': 'fixture', 'UPSTREAM_PAY_PASS': 'fixture'}
        for gift, ai in [(True, True), (True, False), (False, True)]:
            with self.subTest(gift=gift, ai=ai), patch.dict(os.environ, credentials, clear=True), patch.object(server, 'ENABLE_GIFTCARD', gift), patch.object(server, 'ENABLE_AI', ai), patch.object(server, 'PROFIT', Decimal('-3')), patch.object(server, 'AI_PROFIT', Decimal('-2.5')):
                server.validate_config()

    def test_percentage_at_or_below_minus_100_is_rejected_in_config(self):
        credentials = {'UPSTREAM_USER': 'fixture', 'UPSTREAM_PASS': 'fixture', 'UPSTREAM_PAY_PASS': 'fixture'}
        for name in ('PROFIT', 'AI_PROFIT'):
            for value in ('-100', '-101'):
                with self.subTest(name=name, value=value), patch.dict(os.environ, credentials, clear=True), patch.object(server, name, Decimal(value)):
                    with self.assertRaisesRegex(RuntimeError, '必须大于 -100'):
                        server.validate_config()

    def test_environment_negative_percentages_ignore_legacy_opt_in(self):
        code = 'import json,server; server.validate_config(); print(json.dumps([str(server.PROFIT),str(server.AI_PROFIT),server.sale_price(100,server.PROFIT)]))'
        for legacy in ('0', '1'):
            env = {**os.environ, 'PROFIT': '-3', 'AI_PROFIT': '-2.5', 'ALLOW_BELOW_COST': legacy,
                   'UPSTREAM_USER': 'fixture', 'UPSTREAM_PASS': 'fixture', 'UPSTREAM_PAY_PASS': 'fixture',
                   'ENABLE_GIFTCARD': '1', 'ENABLE_AI': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([sys.executable, '-c', code], cwd=Path(server.__file__).parent,
                                    env=env, text=True, capture_output=True, timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout), ['-3', '-2.5', 97])

    def test_gift_cost_includes_service_fee(self):
        cost = server.giftcard_cost({'display_buy_rate': '0.99', 'service_fee_rmb': '1.25'}, 100)
        self.assertEqual(cost, Decimal('100.25'))
        self.assertEqual(server.sale_price(cost, 3), 104.25)

    def test_gift_quote_preserves_original_price_and_currency_rounding(self):
        detail = {'display_buy_rate': '1.00005', 'service_fee_rmb': '1.25'}
        for rate, expected in [('3', 105.26), ('-3', 98.26), ('0', 101.26)]:
            with self.subTest(rate=rate), patch.object(server, 'PROFIT', Decimal(rate)):
                quote = server.giftcard_quote(detail, 100, Decimal('1.23455'))
                self.assertEqual(quote, {'amount': 100, 'original_price': 123.46, 'sell_price': expected})

    def test_original_price_excludes_fees_but_payable_still_includes_them(self):
        with patch.object(server, 'PROFIT', Decimal('0')):
            for fee, payable in [('0', 99), ('1.25', 100.25), ('10', 109)]:
                with self.subTest(fee=fee):
                    quote = server.giftcard_quote({'display_buy_rate': '0.99', 'service_fee_rmb': fee}, 100, Decimal('0.99'))
                    self.assertEqual(quote['original_price'], 99)
                    self.assertEqual(quote['sell_price'], payable)

    def test_environment_percentages_are_loaded_as_exact_decimals(self):
        env = {**os.environ, 'PROFIT': '0.50000000000000001', 'AI_PROFIT': '2.5',
               'ALLOW_BELOW_COST': '0', 'PYTHONDONTWRITEBYTECODE': '1'}
        code = 'import json,server; print(json.dumps([str(server.PROFIT),str(server.AI_PROFIT),server.sale_price(200,server.PROFIT)]))'
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(server.__file__).parent,
                                env=env, text=True, capture_output=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), ['0.50000000000000001', '2.5', 202])


class PricingFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(server, 'DB_FILE', str(Path(self.temp.name) / 'shop.db')))
        self.stack.enter_context(patch.object(server, 'RATE_LIMITER', server.RateLimiter()))
        for name, value in [('ENABLE_GIFTCARD', True), ('ENABLE_AI', True),
                            ('PROFIT', Decimal('3')), ('AI_PROFIT', Decimal('3'))]:
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server.urllib.request, 'urlopen', side_effect=AssertionError('external request blocked')))
        server.init_db()
        self.detail = {'id': 9, 'name': 'Apple/iTunes 🇺🇸 (美国)', 'amounts': [100], 'brand_levels': {'level_2': {}},
                       'display_buy_rate': '0.99', 'service_fee_rmb': '1.25'}
        self.plan = {'id': 11, 'name': 'fixture AI', 'enabled': True, 'available_stock': 5, 'price_rmb': '100.25'}
        self.stack.enter_context(patch.object(server, 'get_products', return_value=[self.detail]))
        self.country_rates = self.stack.enter_context(patch.object(server, 'get_country_rates', return_value={'美国': Decimal('6.674')}))
        self.stack.enter_context(patch.object(server, 'get_product_detail', return_value=self.detail))
        self.stack.enter_context(patch.object(server, 'get_ai_plans', return_value={'code': 0, 'data': {'plans': [self.plan]}}))
        ids = itertools.count(55)
        self.recharge = self.stack.enter_context(patch.object(server, 'create_recharge', side_effect=lambda amount: {
            'code': 0, 'data': {'id': next(ids), 'pay_data': {'qr_code': 'https://example.invalid/pay'}}}))
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        self.stack.close(); self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=5)
        try:
            conn.request('GET' if body is None else 'POST', path, body=None if body is None else json.dumps(body),
                         headers={'Content-Type': 'application/json', 'Idempotency-Key': __import__('secrets').token_hex(32), **(headers or {})})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
            self.assertEqual(payload['code'], 0)
            return payload['data']
        finally:
            conn.close()

    def test_gift_catalog_quote_matches_creation_and_payment(self):
        product = self.request('/api/products')[0]
        self.assertEqual(product['profit_percent'], 3)
        self.assertNotIn('profit', product)
        quote = product['quotes'][0]['sell_price']
        created = self.request('/api/create-order', {'product_id': 9, 'amount': 100})
        self.assertEqual(quote, 104.25)
        self.assertEqual(created['sell_price'], quote)
        self.recharge.assert_called_once_with(quote)
        saved = server.read_order(created['id'])
        self.assertEqual(saved['cost_price'], 100.25)
        self.assertEqual(saved['sell_price'], quote)

    def test_catalog_hides_turkey_even_without_reference_rates(self):
        turkey = dict(self.detail, id=10, name='Apple/iTunes 🇹🇷 (土耳其)')
        japan = dict(self.detail, id=12, name='Apple/iTunes 🇯🇵 (日本)')
        for rates in ({'美国': Decimal('6.674'), '日本': Decimal('0.0425'), '土耳其': Decimal('0.1343')}, {}):
            with self.subTest(rates=rates), patch.object(server, 'get_products', return_value=[self.detail, turkey, japan]):
                self.country_rates.return_value = rates
                products = self.request('/api/products')
                self.assertEqual([p['id'] for p in products], [9, 12])
                for product in products:
                    self.assertEqual(product['quotes'][0]['sell_price'], 104.25)
                if rates:
                    self.assertEqual(products[0]['quotes'][0]['original_price'], 667.40)
                else:
                    self.assertIsNone(products[0]['quotes'][0]['original_price'])
        with patch.object(server, 'get_products', return_value=[turkey]):
            self.country_rates.reset_mock()
            self.assertEqual(self.request('/api/products'), [])
            self.country_rates.assert_not_called()
        self.recharge.assert_not_called()

    def test_ai_plan_quote_matches_creation_and_payment(self):
        quote = self.request('/api/ai-plans')[0]['sell_price']
        created = self.request('/api/create-ai-order', {'plan_id': 11})
        self.assertEqual(quote, 104.25)
        self.assertEqual(created['sell_price'], quote)
        self.recharge.assert_called_once_with(quote)

    def test_ai_catalog_shows_all_enabled_plans_including_sold_out(self):
        plans = [dict(self.plan, id=i, provider_code='chatgpt' if i <= 4 else 'claude',
                      available_stock=1 if i <= 2 else 0) for i in range(1, 7)]
        disabled = dict(self.plan, id=7, enabled=False)
        with patch.object(server, 'get_ai_plans', return_value={'code': 0, 'data': {'plans': plans + [disabled]}}):
            visible = self.request('/api/ai-plans')
            self.assertEqual([p['id'] for p in visible], [1, 2, 3, 4, 5, 6])
            self.assertEqual(sum(p['provider_code'] == 'chatgpt' for p in visible), 4)
            self.assertEqual(sum(p['provider_code'] == 'claude' for p in visible), 2)
            self.assertEqual(sum(p['available_stock'] == 0 for p in visible), 4)
            self.assertTrue(all(p['sell_price'] == 104.25 for p in visible))
        self.recharge.assert_not_called()

    def test_disabled_or_missing_ai_plans_create_no_payment(self):
        for provider in ('chatgpt', 'claude'):
            for enabled, stock in [(False, 0), (False, 5)]:
                plan = dict(self.plan, provider_code=provider, enabled=enabled, available_stock=stock)
                with self.subTest(provider=provider, enabled=enabled, stock=stock), patch.object(server, 'get_ai_plans', return_value={'code': 0, 'data': {'plans': [plan]}}):
                    with self.assertRaisesRegex(ValueError, '套餐已下架或不存在'):
                        server.create_local_order({'plan_id': plan['id']}, ai=True)
        with patch.object(server, 'get_ai_plans', return_value={'code': 0, 'data': {'plans': []}}):
            with self.assertRaisesRegex(ValueError, '套餐已下架或不存在'):
                server.create_local_order({'plan_id': 999}, ai=True)
        self.recharge.assert_not_called()
        with server.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)

    def test_zero_stock_ai_quotes_create_payments_for_both_providers(self):
        with patch.object(server, 'create_ai_order') as dispatch:
            for provider in ('chatgpt', 'claude'):
                plan = dict(self.plan, provider_code=provider, available_stock=0)
                for rate, expected in [('3', 104.25), ('0', 100.25), ('-3', 97.25)]:
                    with self.subTest(provider=provider, rate=rate), patch.object(server, 'get_ai_plans', return_value={'code': 0, 'data': {'plans': [plan]}}), patch.object(server, 'AI_PROFIT', Decimal(rate)):
                        self.recharge.reset_mock()
                        quote = self.request('/api/ai-plans')[0]
                        created = self.request('/api/create-ai-order', {'plan_id': plan['id']})
                        self.assertEqual(quote['available_stock'], 0)
                        self.assertEqual(quote['sell_price'], expected)
                        self.assertEqual(created['sell_price'], expected)
                        self.recharge.assert_called_once_with(expected)
                        saved = server.read_order(created['id'])
                        self.assertEqual(saved['cost_price'], 100.25)
                        self.assertEqual(saved['ai_provider_code'], provider)
                        self.assertEqual(saved['fulfillment_state'], 'pending')
            dispatch.assert_not_called()

    def test_zero_stock_ai_paid_order_is_forwarded_once(self):
        for i, provider in enumerate(('chatgpt', 'claude')):
            plan = dict(self.plan, provider_code=provider, available_stock=0)
            with self.subTest(provider=provider), patch.object(server, 'get_ai_plans', return_value={'code': 0, 'data': {'plans': [plan]}}):
                created = self.request('/api/create-ai-order', {'plan_id': plan['id']})
                payment = {'code': 0, 'data': {'id': created['recharge_id'], 'status': 0, 'amount': created['sell_price']}}
                with patch.object(server, 'get_recharge_status', return_value=payment), patch.object(server, 'upstream_api', return_value={'code': 0, 'data': {'balance': 1000}}), patch.object(server, 'create_ai_order', return_value={'code': 0, 'data': {'id': 7001+i}}) as dispatch:
                    server.fulfill_order(created['id'])
                    dispatch.assert_not_called()
                    payment['data']['status'] = 1
                    server.fulfill_order(created['id'])
                    server.fulfill_order(created['id'])
                    dispatch.assert_called_once_with(plan['id'])
                    saved = server.read_order(created['id'])
                    self.assertEqual(saved['payment_verified'], 1)
                    self.assertEqual(saved['fulfillment_state'], 'created')
                    self.assertEqual(saved['upstream_order_id'], 7001+i)

    def test_catalog_original_price_is_unchanged_by_markup_or_discount(self):
        for rate, sale in [('3', 104.25), ('-3', 97.25), ('0', 100.25)]:
            with self.subTest(rate=rate), patch.object(server, 'PROFIT', Decimal(rate)):
                quote = self.request('/api/products')[0]['quotes'][0]
                self.assertEqual(quote['original_price'], 667.40)
                self.assertEqual(quote['sell_price'], sale)
        self.recharge.assert_not_called()

    def test_missing_reference_rate_does_not_block_order_or_change_payment(self):
        self.country_rates.return_value = {}
        product = self.request('/api/products')[0]
        self.assertIsNone(product['reference_rate'])
        self.assertIsNone(product['quotes'][0]['original_price'])
        self.assertEqual(product['quotes'][0]['sell_price'], 104.25)
        created = self.request('/api/create-order', {'product_id': 9, 'amount': 100})
        self.assertEqual(created['sell_price'], 104.25)
        self.recharge.assert_called_once_with(104.25)
        self.country_rates.assert_called_once_with()

    def test_each_business_uses_its_own_percentage(self):
        with patch.object(server, 'AI_PROFIT', Decimal('5')):
            gift = self.request('/api/create-order', {'product_id': 9, 'amount': 100})
            ai = self.request('/api/create-ai-order', {'plan_id': 11})
        self.assertEqual(gift['sell_price'], 104.25)
        self.assertEqual(ai['sell_price'], 106.25)

    def test_client_supplied_price_and_percentage_are_ignored(self):
        for path, body in [('/api/create-order', {'product_id': 9, 'amount': 100}),
                           ('/api/create-ai-order', {'plan_id': 11})]:
            result = self.request(path, {**body, 'sell_price': 1, 'cost_price': 0, 'profit': -100, 'profit_percent': -100})
            self.assertEqual(result['sell_price'], 104.25)
        self.assertEqual([call.args[0] for call in self.recharge.call_args_list], [104.25, 104.25])

    def test_discount_quotes_match_both_created_order_and_payment(self):
        with patch.object(server, 'PROFIT', Decimal('-3')), patch.object(server, 'AI_PROFIT', Decimal('-5')):
            gift_quote = self.request('/api/products')[0]['quotes'][0]['sell_price']
            ai_quote = self.request('/api/ai-plans')[0]['sell_price']
            gift = self.request('/api/create-order', {'product_id': 9, 'amount': 100})
            ai = self.request('/api/create-ai-order', {'plan_id': 11})
        self.assertEqual(gift_quote, 97.25)
        self.assertEqual(ai_quote, 95.25)
        self.assertEqual([gift['sell_price'], ai['sell_price']], [gift_quote, ai_quote])
        self.assertEqual([call.args[0] for call in self.recharge.call_args_list], [gift_quote, ai_quote])
        self.assertEqual(server.read_order(gift['id'])['cost_price'], 100.25)
        self.assertEqual(server.read_order(ai['id'])['cost_price'], 100.25)

    def test_existing_order_amount_is_unchanged_after_enabling_discount(self):
        old = self.request('/api/create-order', {'product_id': 9, 'amount': 100})
        with patch.object(server, 'PROFIT', Decimal('-3')):
            self.assertEqual(self.request('/api/products')[0]['quotes'][0]['sell_price'], 97.25)
            payment = self.request('/api/payment-info', {'order_id': old['id'], 'access_token': old['access_token']})
        self.assertEqual(old['sell_price'], 104.25)
        self.assertEqual(payment['sell_price'], 104.25)
        self.assertEqual(server.read_order(old['id'])['sell_price'], 104.25)
        self.recharge.assert_called_once_with(104.25)

    def test_discount_order_waits_for_full_upstream_cost_balance(self):
        with patch.object(server, 'PROFIT', Decimal('-3')):
            created = self.request('/api/create-order', {'product_id': 9, 'amount': 100})
        order = server.read_order(created['id'])
        payment = {'code': 0, 'data': {'id': order['recharge_id'], 'status': 1, 'amount': 97.25}}
        with patch.object(server, 'get_recharge_status', return_value=payment), patch.object(server, 'upstream_api', return_value={'code': 0, 'data': {'balance': 97.25}}) as wallet, patch.object(server, 'create_order', return_value={'code': 0, 'data': {'id': 7001}}) as create:
            waiting = server.fulfill_order(created['id'])
            self.assertEqual(waiting['payment_verified'], 1)
            self.assertEqual(waiting['fulfillment_state'], 'pending')
            create.assert_not_called()
            wallet.return_value = {'code': 0, 'data': {'balance': 100.25}}
            completed = server.fulfill_order(created['id'])
        create.assert_called_once()
        self.assertEqual(completed['fulfillment_state'], 'created')
        self.assertEqual(completed['sell_price'], 97.25)

    def test_existing_fixed_markup_order_amount_is_preserved(self):
        with patch.object(server, 'sale_price', side_effect=lambda cost, profit: float(server.money(cost) + server.money(profit))):
            old = self.request('/api/create-order', {'product_id': 9, 'amount': 100})
        self.assertEqual(old['sell_price'], 103.25)
        self.recharge.assert_called_once_with(103.25)
        with patch.object(server, 'PROFIT', Decimal('90')):
            server.init_db()
            self.assertEqual(self.request('/api/products')[0]['quotes'][0]['sell_price'], 191.25)
            order = self.request('/api/order/' + str(old['id']), headers={'Authorization': 'Bearer ' + old['access_token']})
            payment = self.request('/api/payment-info', {'order_id': old['id'], 'access_token': old['access_token']})
        self.assertEqual(order['sell_price'], 103.25)
        self.assertEqual(payment['sell_price'], 103.25)
        self.assertEqual(server.read_order(old['id'])['sell_price'], 103.25)


if __name__ == '__main__':
    unittest.main()
