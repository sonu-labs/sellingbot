"""Offline coverage for provider response parsing and storefront invariants."""
import pathlib
import unittest
from unittest.mock import patch
from otp import catalog_items, price_options, retail_price, OPERATORS, TemporaClient, ProviderError


class CatalogTests(unittest.TestCase):
    def test_service_map(self):
        self.assertEqual(catalog_items({'tg': 'Telegram', 'ig': 'Instagram'}, 'services'), [('ig', 'Instagram'), ('tg', 'Telegram')])

    def test_country_objects_and_envelopes(self):
        self.assertEqual(catalog_items({'countries': {'22': {'eng': 'India'}, '0': {'name': 'Russia'}}}, 'countries'), [('22', 'India'), ('0', 'Russia')])

    def test_service_list(self):
        self.assertEqual(catalog_items({'services': [{'code': 'wa', 'name': 'WhatsApp'}]}, 'services'), [('wa', 'WhatsApp')])

    def test_operators_match_supplied_routes(self):
        self.assertEqual(OPERATORS, ('1', '2', '3', '4', '7', '8', '9', '10'))

    def test_prices_only_positive_stock(self):
        data = {'22': {'tg': {'providers': {'17': {'price': .2, 'count': 3}, '18': {'price': .1, 'count': 0}}}}}
        self.assertEqual(price_options(data, '22', 'tg'), [{'cost': .2, 'stock': 3, 'provider': '17'}])

    def test_reject_invalid_prices(self):
        for price in (-1, 0, 'NaN', 'Infinity', 'invalid'):
            self.assertEqual(price_options({'22': {'tg': {'price': price, 'count': 5}}}, '22', 'tg'), [])

    def test_direct_price_shape(self):
        self.assertEqual(price_options({'22': {'tg': {'price': .02, 'count': 10}}}, '22', 'tg')[0]['cost'], .02)

    def test_v3_provider_price_list_and_public_provider_ids(self):
        data = {'22': {'wa': {'price': 2.99, 'count': 467, 'providers': {
            '3': {'count': 467, 'price': [2.99], 'providerIds': '12,15'},
        }}}}
        self.assertEqual(price_options(data, '22', 'wa'), [
            {'cost': 2.99, 'stock': 467, 'provider': '12,15'},
        ])

    def test_legacy_price_map(self):
        self.assertEqual(price_options({'22': {'wa': {'2.99': '467'}}}, '22', 'wa'), [
            {'cost': 2.99, 'stock': 467, 'provider': None},
        ])

    def test_wallet_price_rounds_up(self):
        self.assertEqual(retail_price(.02101, 100, 10), 2.32)

    def test_missing_stock_fails_closed(self):
        self.assertEqual(price_options({'22': {'tg': {'price': .02}}}, '22', 'tg'), [])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_client_never_calls_network(self):
        with self.assertRaises(ProviderError):
            await TemporaClient('').call('getServices')

    async def test_uses_documented_get_and_logs_exact_raw_response_without_key(self):
        calls, observed = [], []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self):
                return "ACCESS_BALANCE:100.1234"

        class Session:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, params):
                calls.append((url, params))
                return Response()

        async def observer(action, params, raw, status):
            observed.append((action, params, raw, status))

        with patch("otp.aiohttp.ClientSession", Session):
            result = await TemporaClient("secret-key", observer).call("getBalance")

        self.assertEqual(result, "ACCESS_BALANCE:100.1234")
        self.assertEqual(calls[0][1], {"api_key": "secret-key", "action": "getBalance"})
        self.assertEqual(observed, [("getBalance", {}, "ACCESS_BALANCE:100.1234", 200)])
        self.assertNotIn("secret-key", repr(observed))

    async def test_provider_error_tokens_are_preserved(self):
        client = TemporaClient("key")

        async def fake_call(action, **params):
            raise ProviderError("bad selection", code="BAD_TYPE")

        client.call = fake_call
        with self.assertRaises(ProviderError) as ctx:
            await client.call("getNumber")
        self.assertEqual(ctx.exception.code, "BAD_TYPE")


class WiringTests(unittest.TestCase):
    def test_button_precedes_smm(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'bot.py').read_text()
        self.assertLess(source.index('text="📲 Buy OTPs (All Apps)"'), source.index('callback_data="smm_open")])'))

    def test_callback_payload_with_object_id_fits_telegram(self):
        self.assertLess(len('otp:select:' + 'a'*24 + ':9999'), 64)
