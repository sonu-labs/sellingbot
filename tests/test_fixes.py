"""Regression tests for the formatting / premium-emoji / rate fixes.

These tests deliberately do not import bot.py (it needs a live MongoDB and a
real bot token).  They cover the transport layer (rich_messages), the
currency helper (i18n) and source-level invariants for the whole bot.
"""

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import rich_messages as rm  # noqa: E402
from i18n import USD_RATE, format_balance  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY_FILES = [p for p in ROOT.glob("*.py")]

EMOJI_CLUSTER = re.compile(
    r"[\U0001F1E6-\U0001F1FF]{1,2}"
    r"|[\U0001F000-\U0001FAFF\u2600-\u27BF](?:\ufe0f)?"
    r"(?:\u200d[\U0001F000-\U0001FAFF\u2600-\u27BF](?:\ufe0f)?)*"
)


def source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class RateTests(unittest.TestCase):
    def test_rate_is_100_everywhere(self):
        self.assertEqual(USD_RATE, 100.0)
        self.assertEqual(rm.USD_TO_INR, 100.0)
        # $100 must display as ₹10,000 worth / ₹10,000 -> $100.00
        self.assertEqual(format_balance({"currency": "USD", "balance": 10000}), "$100.00")
        self.assertEqual(rm.convert_currency("₹152.02", "USD"), "$1.52")
        self.assertIn("USDT_TO_INR = 100", source("recharge_flow.py"))
        self.assertIn("exchange_rate=100.0", source("server3.py"))

    def test_no_old_rate_left_in_code(self):
        for name in ("bot.py", "recharge_flow.py", "i18n.py", "rich_messages.py", "server3.py"):
            text = source(name)
            self.assertNotIn("* 95.0", text, name)
            self.assertNotIn("/ 95.0", text, name)
            self.assertNotIn("USDT_TO_INR = 90", text, name)


class PremiumEmojiTests(unittest.TestCase):
    def test_unmapped_emoji_becomes_premium_fallback(self):
        out = rm.to_rich_html("test 🎲 flag 🇷🇺 ok ✅")
        stripped = re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", "", out)
        self.assertIsNone(EMOJI_CLUSTER.search(stripped))
        self.assertIn("<tg-emoji", out)

    def test_every_source_emoji_is_premium_mappable_or_fallback_safe(self):
        for path in PY_FILES:
            if path.name == "rich_messages.py":
                continue
            for token in set(EMOJI_CLUSTER.findall(path.read_text(encoding="utf-8"))):
                if token in rm._KEEP_PLAIN_SYMBOLS:
                    continue
                self.assertIsNotNone(
                    rm.emoji_id(token), f"{path.name}: unmapped emoji {token!r} in source"
                )

    def test_button_label_has_no_normal_emoji(self):
        from aiogram.types import InlineKeyboardButton

        for label in ("👨‍💻 Support", "🎲 Random", "🇷🇺 Russia", "S1: 🟢 ON", "💳 Recharge"):
            btn = rm.premium_button(InlineKeyboardButton(text=label, callback_data="x"))
            self.assertIsNone(EMOJI_CLUSTER.search(btn.text), label)
            self.assertTrue(btn.icon_custom_emoji_id, label)

    def test_country_list_buttons_have_no_premium_emoji(self):
        # Owner request: country lists are plain (no premium icon per country
        # row), everywhere else buttons keep their premium icon.
        from aiogram.types import InlineKeyboardButton

        for raw in ("India 🇮🇳", "🌍 United States", "India"):
            label = rm.plain_country_label(raw)
            self.assertIsNone(EMOJI_CLUSTER.search(label))
            btn = rm.premium_button(
                InlineKeyboardButton(text=f"{label} (₹100.00)", callback_data="country:x")
            )
            self.assertIsNone(btn.icon_custom_emoji_id, raw)
            self.assertIn(label, btn.text)

        other = rm.premium_button(
            InlineKeyboardButton(text="💳 Recharge", callback_data="recharge")
        )
        self.assertTrue(other.icon_custom_emoji_id)

    def test_callback_toast_has_no_emoji(self):
        from aiogram.methods import AnswerCallbackQuery

        method = AnswerCallbackQuery(
            callback_query_id="1", text="❌ Country not found", show_alert=True
        )
        upgraded = rm._upgrade_method(method)
        self.assertEqual(upgraded.text, "Country not found")


class BrandingAndCommissionTests(unittest.TestCase):
    def test_official_bot_username_only(self):
        for path in PY_FILES:
            self.assertNotIn("TGAStore1Bot", path.read_text(encoding="utf-8"), path.name)
        self.assertIn("TGAStoreAPIBot", source("config.py"))

    def test_no_deposit_commission_label(self):
        for name in ("bot.py", "recharge_flow.py"):
            self.assertNotIn("Deposit Commission", source(name), name)
        self.assertIn("Referral Commission", source("bot.py"))
        self.assertIn("Referral Commission", source("recharge_flow.py"))
        # The admin-set percentage key is kept so existing settings survive.
        self.assertIn("refer_commission_percent", source("bot.py"))

    def test_no_hardcoded_server2_keys(self):
        text = source("bot.py")
        self.assertNotRegex(text, r'TGLION_API_KEY\s*=\s*"[^"]+"')
        self.assertNotRegex(text, r'TGPVA_API_KEY\s*=\s*"[^"]+"')
        self.assertIn("TGLION_API_KEY", source("config.py"))


class AccountFormatTests(unittest.TestCase):
    def test_account_page_fields_each_on_own_line(self):
        text = source("bot.py")
        for field in ("<b>Name:</b>", "<b>Username:</b>", "<b>User ID:</b>",
                      "<b>Your Balance:</b>", "<b>Currency:</b>",
                      "<b>Bot:</b>", "<b>Sales Log:</b>"):
            self.assertIn(field, text)
        # Fields are separated by a blank line (<br><br>), not crammed together.
        self.assertIn("<br><br>", text)


if __name__ == "__main__":
    unittest.main()
