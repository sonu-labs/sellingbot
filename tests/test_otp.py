"""Offline coverage for provider response parsing and storefront invariants."""
import pathlib
import unittest
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

    def test_wallet_price_rounds_up(self):
        self.assertEqual(retail_price(.02101, 100, 10), 2.32)

    def test_missing_stock_fails_closed(self):
        self.assertEqual(price_options({'22': {'tg': {'price': .02}}}, '22', 'tg'), [])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_client_never_calls_network(self):
        with self.assertRaises(ProviderError):
            await TemporaClient('').call('getServices')


class WiringTests(unittest.TestCase):
    def test_button_precedes_smm(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'bot.py').read_text()
        self.assertLess(source.index('text="📲 Buy OTPs (All Apps)"'), source.index('callback_data="smm_open")])'))

    def test_callback_payload_with_object_id_fits_telegram(self):
        self.assertLess(len('otp:select:' + 'a'*24 + ':9999'), 64)
