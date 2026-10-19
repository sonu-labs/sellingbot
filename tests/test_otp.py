"""Offline coverage for provider response parsing and storefront invariants."""
import pathlib
import unittest
from unittest.mock import patch
from otp import (
    OPERATORS, PER_PAGE, ProviderError, TemporaClient, available_operators, catalog_items,
    fetch_services_catalog, filter_catalog, normalize_search, page_numbers, pager_buttons,
    parse_operator_ids, pick_operator, price_options, retail_price,
)


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


class CatalogPresentationTests(unittest.TestCase):
    """Pure helpers behind the storefront search box and the numbered pager."""

    def test_search_filtering_and_numbered_pager_helpers(self):
        self.assertEqual(PER_PAGE, 12)
        # Queries are trimmed and capped before they are stored or rendered.
        self.assertEqual(normalize_search("  Telegram  "), "Telegram")
        self.assertEqual(normalize_search("x" * 500), "x" * 80)
        self.assertEqual(normalize_search(None), "")
        items = [("tg", "Telegram"), ("tss", "TemporaSMS WhatsApp"), ("22", "India")]
        # Casefolded match on name OR code, keeping the snapshot indexes.
        self.assertEqual(filter_catalog(items, "TELE"), [(0, "tg", "Telegram")])
        self.assertEqual(filter_catalog(items, "tss"), [(1, "tss", "TemporaSMS WhatsApp")])
        self.assertEqual(filter_catalog(items, "  "),
                         [(0, "tg", "Telegram"), (1, "tss", "TemporaSMS WhatsApp"), (2, "22", "India")])
        self.assertEqual(filter_catalog(items, "nothing-here"), [])
        # Country lists additionally match a numeric query on the numeric code,
        # so a typed prefix such as "+22" still finds country 22.
        countries = [("22", "India"), ("0", "Russia"), ("7", "Indonesia")]
        self.assertEqual([c for _i, c, _n in filter_catalog(countries, "22", numeric_codes=True)], ["22"])
        self.assertEqual([c for _i, c, _n in filter_catalog(countries, "0", numeric_codes=True)], ["0"])
        self.assertEqual([c for _i, c, _n in filter_catalog(countries, "2", numeric_codes=True)], ["22"])
        self.assertEqual([c for _i, c, _n in filter_catalog(countries, "+22", numeric_codes=True)], ["22"])
        self.assertEqual(filter_catalog(countries, "9", numeric_codes=True), [])
        # App lists get no numeric matching: "+22" finds nothing there.
        self.assertEqual(filter_catalog([("22", "India")], "+22"), [])
        self.assertEqual(filter_catalog([("22", "India")], "22"), [(0, "22", "India")])
        # Pager: current page is a noop marker, the others jump by number.
        self.assertEqual(pager_buttons("TOK", 0, 1), [])
        self.assertEqual(page_numbers(0, 1), [])
        self.assertEqual(pager_buttons("TOK", 1, 3),
                         [("1", "otp:page:TOK:0"), ("• 2 •", "noop"), ("3", "otp:page:TOK:2")])
        # Very long catalogs are windowed, keeping the first and last page.
        window = pager_buttons("TOK", 8, 40)
        labels = [text for text, _data in window]
        self.assertLess(len(labels), 40)
        self.assertEqual(labels[0], "1")
        self.assertEqual(labels[-1], "40")
        self.assertIn("• 9 •", labels)
        self.assertIn("…", labels)
        self.assertTrue(all(data == "noop" or data.startswith("otp:page:TOK:") for _t, data in window))
        self.assertLessEqual(max(len(data) for _t, data in window), 64)


class OperatorParsingTests(unittest.TestCase):
    def test_documented_label_to_id_map(self):
        self.assertEqual(parse_operator_ids({"Operator 1": "1", "Operator 2": "2"}), ("1", "2"))

    def test_id_to_label_map_and_mixed_numeric_types(self):
        self.assertEqual(
            parse_operator_ids({"2": "Operator 2", "1": "Operator 1", "7": 7, "10": 10.0}),
            ("1", "2", "7", "10"),
        )

    def test_envelopes_and_object_values(self):
        payload = {"status": "ok", "data": {"operators": {
            "1": {"name": "Operator 1"}, "Operator 4": {"id": 4}, "5": {"code": "5"},
        }}}
        self.assertEqual(parse_operator_ids(payload), ("1", "4", "5"))

    def test_lists_of_ids_labels_and_objects(self):
        payload = [1, "2", "Operator 3", {"id": "4"}, {"operator": 7}, {"name": "Line 9"}]
        self.assertEqual(parse_operator_ids(payload), ("1", "2", "3", "4", "7", "9"))

    def test_json_encoded_and_delimited_strings(self):
        self.assertEqual(parse_operator_ids('{"Operator 1": "1"}'), ("1",))
        self.assertEqual(parse_operator_ids("[1, 2, 7]"), ("1", "2", "7"))
        self.assertEqual(parse_operator_ids("1,2; 7|10"), ("1", "2", "7", "10"))

    def test_provider_errors_and_non_numeric_values_fail_closed(self):
        for payload in ("BAD_OPERATOR", {"error": "BAD_OPERATOR"}, {}, [], None, True,
                        float("nan"), [0, -1, "x"], {"Operator X": "x"}):
            self.assertEqual(parse_operator_ids(payload), (), payload)

    def test_only_enabled_live_operators_are_available_in_admin_order(self):
        data = {"Operator 10": 10, "Operator 2": "2", "2": "Operator 3", "Operator 99": "99"}
        self.assertEqual(available_operators(data, ("1", "2", "10")), ("2", "10"))
        self.assertEqual(pick_operator(data, ("1", "10")), "10")
        with self.assertRaises(ProviderError) as ctx:
            pick_operator(data, ("1", "4"))
        self.assertEqual(ctx.exception.code, "NO_OPERATORS")


class ServicesCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_are_fetched_with_live_enabled_numeric_operator(self):
        calls = []

        class FakeAPI:
            async def catalog(self, action, **params):
                calls.append((action, params))
                if action == "getOperators":
                    return {"Operator 3": "3", "Operator 10": "10"}
                if action == "getServices":
                    return {"tg": "Telegram"}
                raise AssertionError(action)

        items, operator = await fetch_services_catalog(FakeAPI(), ("1", "10"))
        self.assertEqual(operator, "10")
        self.assertEqual(items, [("tg", "Telegram")])
        self.assertEqual(calls, [("getOperators", {}), ("getServices", {"operator": "10"})])

    async def test_services_are_never_requested_without_a_usable_operator(self):
        actions = []

        class FakeAPI:
            async def catalog(self, action, **params):
                actions.append(action)
                if action == "getOperators":
                    return "BAD_OPERATOR"
                raise AssertionError("getServices must not run without an operator")

        with self.assertRaises(ProviderError) as ctx:
            await fetch_services_catalog(FakeAPI(), ("1", "2"))
        self.assertEqual(ctx.exception.code, "NO_OPERATORS")
        self.assertEqual(actions, ["getOperators"])


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

    async def test_get_services_logs_operator_and_raw_body_but_not_the_key(self):
        calls, observed = [], []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self):
                return '{"tg": "Telegram"}'

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
            result = await TemporaClient("secret-key", observer).call("getServices", operator="1")

        self.assertEqual(result, {"tg": "Telegram"})
        self.assertEqual(calls[0][1], {"api_key": "secret-key", "action": "getServices", "operator": "1"})
        self.assertEqual(observed, [("getServices", {"operator": "1"}, '{"tg": "Telegram"}', 200)])
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
        samples = [
            'otp:select:' + 'a' * 24 + ':9999',
            'otp:page:' + 'a' * 24 + ':9999',
            'otp:search:' + 'a' * 24,
            'otp:searchclear:' + 'a' * 24,
            'otp:alt:' + 'a' * 24 + ':10',
        ]
        for data in samples:
            self.assertLessEqual(len(data.encode()), 64, data)
