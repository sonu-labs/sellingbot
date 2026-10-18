import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from smm_catalog import (  # noqa: E402
    detect_platform,
    normalize_currency,
    normalize_provider_services,
    selling_rate_inr,
)


class SMMCatalogTests(unittest.TestCase):
    def test_detects_only_telegram_and_instagram_categories(self):
        self.assertEqual(
            detect_platform({"category": "Instagram Followers", "name": "Fast delivery"}),
            "instagram",
        )
        self.assertEqual(
            detect_platform({"category": "Telegram Channel Views", "name": "1000 views"}),
            "telegram",
        )
        self.assertIsNone(detect_platform({"category": "YouTube Views", "name": "Video views"}))
        self.assertIsNone(detect_platform({"category": "Instagram + Telegram", "name": "Combo"}))

    def test_normalizes_api_v2_catalog_and_skips_custom_order_types(self):
        payload = [
            {
                "service": 12,
                "name": "Instagram Views",
                "description": "Original provider description: gradual delivery.",
                "category": "Instagram Views",
                "type": "Default",
                "rate": "0.15",
                "min": "50",
                "max": "100000",
            },
            {
                "service": 13,
                "name": "Instagram Custom Comments",
                "category": "Instagram",
                "type": "Custom Comments",
                "rate": "1.5",
                "min": "1",
                "max": "500",
            },
        ]
        result = normalize_provider_services(payload)
        self.assertIsNotNone(result)
        services, invalid, unsupported = result
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["service_id"], "12")
        self.assertEqual(services[0]["min"], 50)
        self.assertEqual(services[0]["description"], "Original provider description: gradual delivery.")
        without_description = normalize_provider_services([{
            "service": 14, "name": "Telegram Views", "type": "Default",
            "rate": 0.5, "min": 10, "max": 500,
        }])[0][0]
        self.assertEqual(without_description["description"], "")
        self.assertEqual((invalid, unsupported), (0, 1))

    def test_invalid_service_fields_are_counted_not_imported(self):
        result = normalize_provider_services([
            {"service": 1, "name": "Missing rate", "min": 1, "max": 100}
        ])
        self.assertEqual(result, ([], 1, 0))
        self.assertIsNone(normalize_provider_services({"error": "bad key"}))

    def test_panel_currency_and_inr_selling_rate(self):
        self.assertEqual(normalize_currency("INR"), "INR")
        self.assertEqual(normalize_currency("USD"), "USD")
        self.assertEqual(selling_rate_inr("2.00", "USD", 20), 240.0)
        self.assertEqual(selling_rate_inr("2.00", "INR", 20), 2.4)
        self.assertEqual(selling_rate_inr("0.01", "USD", 0), 1.0)


if __name__ == "__main__":
    unittest.main()
