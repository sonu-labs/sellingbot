"""Tests for the manual-only SMM admin flow and panel helpers."""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from smm_panel import normalize_currency, render_description_block  # noqa: E402

SMM_SOURCE = (ROOT / "smm.py").read_text(encoding="utf-8")


class SMMPanelCurrencyTests(unittest.TestCase):
    def test_normalizes_panel_currency(self):
        self.assertEqual(normalize_currency("INR"), "INR")
        self.assertEqual(normalize_currency("₹"), "INR")
        self.assertEqual(normalize_currency("rupees"), "INR")
        self.assertEqual(normalize_currency("USD"), "USD")
        self.assertEqual(normalize_currency("US$"), "USD")
        self.assertEqual(normalize_currency(None), "USD")
        self.assertEqual(normalize_currency("", default="INR"), "INR")
        self.assertEqual(normalize_currency("unknown", default="INR"), "INR")


class SMMDescriptionTests(unittest.TestCase):
    def test_description_renders_in_blockquote_with_line_breaks(self):
        rendered = render_description_block("Use a public link.\nDo not send passwords.")
        self.assertEqual(
            rendered,
            "<blockquote>📝 <b>Service description</b>\n"
            "Use a public link.\nDo not send passwords.</blockquote>",
        )

    def test_description_is_html_escaped(self):
        rendered = render_description_block('Warning: <b>never</b> share &amp; reuse keys')
        self.assertIn("&lt;b&gt;never&lt;/b&gt;", rendered)
        self.assertIn("&amp;amp;", rendered)
        self.assertNotIn("<b>never</b>", rendered)

    def test_empty_or_name_only_description_is_hidden(self):
        self.assertEqual(render_description_block(""), "")
        self.assertEqual(render_description_block(None), "")
        self.assertEqual(render_description_block("Same Name", "same name"), "")


class SMMAdminManualFlowTests(unittest.TestCase):
    def test_bulk_import_controls_and_callbacks_are_removed(self):
        for removed in (
            "smma_import",
            "smma_imp:",
            "Import Services",
            "Import Catalog",
            "Import Markup",
            "Auto-match",
            "Entire catalog",
            "import_provider_catalog",
            "import_markup_percent",
        ):
            self.assertNotIn(removed, SMM_SOURCE)

    def test_manual_service_entry_flow_is_available(self):
        for required in (
            "smma_addsvc:",
            "smma_addsvc_panel:",
            "waiting_service_provider_id",
            "waiting_service_name",
            "waiting_service_description",
            "waiting_edit_description",
            "waiting_service_price",
            "waiting_service_min",
            "waiting_service_max",
            "provider_service_id",
            "price_per_1000",
            "Selling Rate per 1,000",
            "Minimum Quantity",
            "Maximum Quantity",
            "Service Description",
            "Edit Description",
            "smma_editdesc:",
            '"service_description"',
            '"description": data.get("service_description", "")',
        ):
            self.assertIn(required, SMM_SOURCE)

    def test_purchase_page_shows_admin_description_in_blockquote(self):
        self.assertIn("from smm_panel import normalize_currency, render_description_block", SMM_SOURCE)
        self.assertIn("def provider_description_block(svc: dict) -> str:", SMM_SOURCE)
        self.assertIn("render_description_block(svc.get(\"description\"), svc.get(\"name\"))", SMM_SOURCE)
        self.assertIn("+ provider_description_block(svc)", SMM_SOURCE)

    def test_provider_panel_and_order_lifecycle_handlers_are_preserved(self):
        for required in (
            "smma_addnicepanel",
            "smma_addpanel",
            "smma_setkey",
            "smma_panelbal",
            "smm_confirm",
            "def refund_order",
            "smma_checkorder",
            "smm_background_checker",
        ):
            self.assertIn(required, SMM_SOURCE)

    def test_customer_categories_are_limited_to_telegram_and_instagram(self):
        self.assertIn('{"slug": "telegram"', SMM_SOURCE)
        self.assertIn('{"slug": "instagram"', SMM_SOURCE)
        self.assertNotIn("youtube", SMM_SOURCE.lower())


if __name__ == "__main__":
    unittest.main()
