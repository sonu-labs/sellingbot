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


if __name__ == "__main__":
    unittest.main()
