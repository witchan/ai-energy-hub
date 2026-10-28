import io
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from decimal import Decimal
from unittest.mock import patch

import server


class CountryRateTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(server, 'country_rates_cache', {}))
        self.stack.enter_context(patch.object(server, 'country_rates_expires', 0.0))
        self.stack.enter_context(patch.object(server, 'country_rates_retry_after', 0.0))
        self.stack.enter_context(patch.object(server.urllib.request, 'urlopen', side_effect=AssertionError('external request blocked')))

    @staticmethod
    def payload(rate='6.674'):
        return {'code': 0, 'data': {'rates': [{'country': '美国', 'symbol': '$', 'rate': rate}]}}

    def test_public_request_has_no_authentication_and_short_timeout(self):
        with patch.object(server.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(self.payload()).encode())) as transport, patch.object(server, 'login') as login:
            self.assertEqual(server.get_country_rates(), {'美国': Decimal('6.674')})
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, server.UPSTREAM_BASE + '/site/country-rates')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(transport.call_args.kwargs['timeout'], 5)
        login.assert_not_called()

    def test_cache_reuses_feed_and_returns_independent_copy(self):
        with patch.object(server, 'upstream_api', return_value=self.payload()) as fetch:
            first = server.get_country_rates()
            first['美国'] = Decimal('1')
            self.assertEqual(server.get_country_rates()['美国'], Decimal('6.674'))
        fetch.assert_called_once_with('/site/country-rates', auth=False, timeout=5)

    def test_cache_refreshes_after_five_minutes(self):
        with patch.object(server.time, 'monotonic', return_value=10) as clock, patch.object(server, 'upstream_api', side_effect=[self.payload(), self.payload('6.8')]) as fetch:
            self.assertEqual(server.get_country_rates()['美国'], Decimal('6.674'))
            clock.return_value = 309
            self.assertEqual(server.get_country_rates()['美国'], Decimal('6.674'))
            clock.return_value = 310
            self.assertEqual(server.get_country_rates()['美国'], Decimal('6.8'))
        self.assertEqual(fetch.call_count, 2)

    def test_failed_refresh_hides_expired_rates_and_backs_off(self):
        with patch.object(server.time, 'monotonic', return_value=10) as clock, patch.object(server, 'upstream_api', side_effect=[self.payload(), TimeoutError(), self.payload('6.8')]) as fetch:
            server.get_country_rates()
            clock.return_value = 310
            self.assertEqual(server.get_country_rates(), {})
            clock.return_value = 339
            self.assertEqual(server.get_country_rates(), {})
            self.assertEqual(fetch.call_count, 2)
            clock.return_value = 340
            self.assertEqual(server.get_country_rates()['美国'], Decimal('6.8'))

    def test_malformed_or_empty_payloads_are_unavailable(self):
        for payload in [None, {}, {'code': 1}, {'code': 0, 'data': None}, {'code': 0, 'data': {'rates': {}}}, {'code': 0, 'data': {'rates': []}}]:
            with self.subTest(payload=payload), patch.object(server, 'country_rates_retry_after', 0), patch.object(server, 'upstream_api', return_value=payload):
                self.assertEqual(server.get_country_rates(), {})

    def test_invalid_rates_and_conflicting_duplicates_are_filtered(self):
        rows = [{'country': '日本', 'rate': '0.0425'}, {'country': '美国', 'rate': 6.674}, {'country': '美国', 'rate': 7}, None]
        rows.extend({'country': str(i), 'rate': value} for i, value in enumerate([None, True, 'NaN', 'Infinity', '-1', '0', 'bad', '1e100']))
        rows.extend([{'country': None, 'rate': 1}, {'country': ' ', 'rate': 1}])
        with patch.object(server, 'upstream_api', return_value={'code': 0, 'data': {'rates': rows}}):
            self.assertEqual(server.get_country_rates(), {'日本': Decimal('0.0425')})

    def test_parallel_catalog_requests_fetch_reference_feed_once(self):
        def feed(*args, **kwargs):
            time.sleep(.03)
            return self.payload()
        with patch.object(server, 'upstream_api', side_effect=feed) as fetch, ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: server.get_country_rates(), range(12)))
        self.assertTrue(all(item == {'美国': Decimal('6.674')} for item in results))
        self.assertEqual(fetch.call_count, 1)

    def test_country_names_override_shared_or_incorrect_currency_symbols(self):
        rates = {'美国': Decimal('6.674'), '瑞士': Decimal('8.0889'), '德国': Decimal('7.4776')}
        for name, symbol, expected in [('Apple/iTunes 🇺🇸 (美国)', '$', '美国'), ('Apple/iTunes 🇨🇭 (瑞士)', '€', '瑞士'), ('德国礼品卡', '€', '德国')]:
            with self.subTest(name=name):
                self.assertEqual(server.product_reference_rate({'name': name, 'symbol': symbol, 'group_tab': '代加'}, rates), (expected, rates[expected]))

    def test_explicit_names_and_longest_label_matching(self):
        rates = {'美国': Decimal('6.674'), '美元': Decimal('6.674'), '美元低误差': Decimal('6.674')}
        for detail, expected in [({'name': '礼品卡', 'country': '美国'}, '美国'), ({'name': '礼品卡', 'group_tab': '美国'}, '美国'), ({'name': '美元低误差礼品卡'}, '美元低误差')]:
            with self.subTest(detail=detail):
                self.assertEqual(server.product_reference_rate(detail, rates), (expected, rates[expected]))

    def test_missing_or_ambiguous_countries_do_not_guess_from_currency(self):
        rates = {'美国': Decimal('6.674'), '加拿大': Decimal('4.7663')}
        for detail in [{'name': '礼品卡', 'symbol': '$', 'group_tab': '代加'}, {'name': '美国加拿大组合'}, {'name': '(美国)(加拿大)'}, {'name': '礼品卡', 'country': '美国', 'country_name': '加拿大'}]:
            with self.subTest(detail=detail):
                self.assertEqual(server.product_reference_rate(detail, rates), ('', None))

    def test_us_twenty_uses_reference_rate_not_purchase_rate(self):
        with patch.object(server, 'PROFIT', Decimal('3')):
            quote = server.giftcard_quote({'display_buy_rate': '6.14', 'service_fee_rmb': 0}, 20, Decimal('6.674'))
        self.assertEqual(quote, {'amount': 20, 'original_price': 133.48, 'sell_price': 126.80})

    def test_missing_reference_rate_never_changes_payable(self):
        for rate in [None, 0, -1, True, 'NaN', 'Infinity', 'bad']:
            with self.subTest(rate=rate), patch.object(server, 'PROFIT', Decimal('3')):
                quote = server.giftcard_quote({'display_buy_rate': '6.14', 'service_fee_rmb': 0}, 20, rate)
                self.assertIsNone(quote['original_price'])
                self.assertEqual(quote['sell_price'], 126.80)


if __name__ == '__main__':
    unittest.main()
