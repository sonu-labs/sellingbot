import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from admin_analytics import safe_amount, summarize_analytics, utc_naive  # noqa: E402


class AdminAnalyticsTests(unittest.TestCase):
    def test_bad_amounts_and_aware_dates_are_safe(self):
        self.assertEqual(safe_amount("not-a-number"), 0.0)
        self.assertEqual(safe_amount(float("nan")), 0.0)
        aware = datetime(2026, 10, 18, 12, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(utc_naive(aware), datetime(2026, 10, 18, 10))

    def test_summary_counts_wallet_account_and_smm_activity(self):
        now = datetime(2026, 10, 18, 12, 0, 0)
        metrics = summarize_analytics(
            total_users=3,
            user_docs=[
                {"joined_at": datetime(2026, 10, 18, 9)},
                {"joined_at": datetime(2026, 10, 14, 9, tzinfo=timezone.utc)},
                {"joined_at": "bad-date"},
            ],
            transactions=[
                {"status": "paid", "amount": "100", "created_at": datetime(2026, 10, 18, 8)},
                {"status": "approved", "amount": "50", "created_at": datetime(2026, 10, 13, 8)},
                {"status": "failed", "amount": 900, "created_at": datetime(2026, 10, 18, 8)},
                {"status": "success", "amount": "bad", "created_at": None},
            ],
            withdrawals=[{"status": "paid", "amount": "20"}, {"status": "pending", "amount": 500}],
            account_orders=[
                {"status": "purchased", "price": "30", "created_at": datetime(2026, 10, 18, 9), "country": "India"},
                {"status": "purchased", "price": 70, "created_at": datetime(2026, 10, 15, 9), "country": "India"},
                {"status": "failed", "price": 500, "country": "USA"},
            ],
            smm_orders=[
                {"status": "Pending", "status_norm": "pending", "price": "40", "created_at": datetime(2026, 10, 18, 10), "refunded": False},
                {"status": "Canceled", "status_norm": "canceled", "price": 10, "created_at": datetime(2026, 10, 14, 10), "refunded": True},
            ],
            now=now,
        )
        self.assertEqual(metrics["users_today"], 1)
        self.assertEqual(metrics["users_week"], 2)
        self.assertEqual(metrics["deposit_total"], 150)
        self.assertEqual(metrics["deposit_today"], 100)
        self.assertEqual(metrics["withdrawal_total"], 20)
        self.assertEqual(metrics["account_orders"], 2)
        self.assertEqual(metrics["account_today_revenue"], 30)
        self.assertEqual(metrics["account_week_revenue"], 100)
        self.assertEqual(metrics["top_country"], "India")
        self.assertEqual(metrics["smm_orders"], 2)
        self.assertEqual(metrics["smm_net"], 40)
        self.assertEqual(metrics["smm_refunds"], 10)
        self.assertEqual(metrics["smm_open_orders"], 1)
        self.assertEqual(metrics["smm_today_net"], 40)

    def test_otp_margin_math_and_empty_collections(self):
        now = datetime(2026, 10, 18, 12, 0, 0)
        # Empty OTP collections should not raise and should return zeros
        metrics = summarize_analytics(
            total_users=0,
            user_docs=[],
            transactions=[],
            withdrawals=[],
            account_orders=[],
            smm_orders=[],
            otp_orders=[],
            now=now,
        )
        self.assertEqual(metrics["otp_orders"], 0)
        self.assertEqual(metrics["otp_net"], 0)
        self.assertEqual(metrics["otp_margin"], 0)
        self.assertEqual(metrics["otp_margin_percent"], 0)
        self.assertEqual(metrics["top_countries"], [])
        # With OTP orders, check margin and refund exclusion
        metrics = summarize_analytics(
            total_users=2,
            user_docs=[{"joined_at": datetime(2026, 10, 18, 9)}],
            transactions=[{"status": "paid", "amount": "100", "created_at": datetime(2026, 10, 18, 8)}],
            withdrawals=[],
            account_orders=[{"status": "purchased", "price": 50, "created_at": datetime(2026, 10, 18, 9), "country": "USA"}],
            smm_orders=[],
            otp_orders=[
                {"status": "completed", "price": 110, "cost": 0.5, "live_cost": 0.5, "created_at": datetime(2026, 10, 18, 10), "user_id": 1, "service_name": "Telegram", "country_name": "India", "route": "auto"},
                {"status": "completed", "price": 220, "cost": 1.0, "created_at": datetime(2026, 10, 18, 11), "user_id": 2, "service_name": "WhatsApp", "country_name": "USA", "route": "1"},
                {"status": "cancelled", "price": 110, "cost": 0.5, "refunded": True, "created_at": datetime(2026, 10, 18, 11), "user_id": 1},
            ],
            now=now,
            rate=100.0,
        )
        # Gross = 440, refunded 110, net 330
        self.assertEqual(metrics["otp_gross"], 440)
        self.assertEqual(metrics["otp_refunded_amount"], 110)
        self.assertEqual(metrics["otp_net"], 330)
        # Cost total = 2.0 (0.5+1.0+0.5) but refunded still counts? We count all for cost
        self.assertAlmostEqual(metrics["otp_cost_total"], 2.0)
        # Provider cost INR = 2.0*100=200, margin = 330-200=130
        self.assertAlmostEqual(metrics["otp_margin"], 130.0)
        self.assertAlmostEqual(metrics["otp_margin_percent"], 130/330*100, places=1)
        self.assertEqual(metrics["otp_today_count"], 3)
        # live_cost missing on legacy order should fallback to cost
        metrics2 = summarize_analytics(
            total_users=1, user_docs=[], transactions=[], withdrawals=[], account_orders=[], smm_orders=[],
            otp_orders=[{"status": "completed", "price": 110, "cost": 0.5, "created_at": datetime(2026, 10, 18, 10)}],
            now=now, rate=100.0
        )
        self.assertEqual(metrics2["otp_live_cost_total"], 0.5)
        self.assertEqual(metrics2["otp_top_app"], "Unknown")
        # Top countries for account orders
        self.assertEqual(metrics["top_countries"][0][0], "USA")

    def test_empty_collections_gracefully(self):
        now = datetime(2026, 10, 18, 12, 0, 0)
        metrics = summarize_analytics(
            total_users=0, user_docs=[], transactions=[], withdrawals=[], account_orders=[], smm_orders=[], otp_orders=[], now=now
        )
        for key in ["total_users", "deposit_total", "account_orders", "smm_orders", "otp_orders", "total_revenue"]:
            self.assertIn(key, metrics)
            self.assertIsNotNone(metrics[key])


if __name__ == "__main__":
    unittest.main()
