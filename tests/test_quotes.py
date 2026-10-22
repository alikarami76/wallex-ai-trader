import unittest

from trader.strategy.quotes import (
    execution_cost,
    fractional_kelly,
    inventory_skew,
    maker_buy_limit,
    maker_sell_limit,
)
from trader.types import OrderBook, BookLevel


def cfg(entry="limit_maker"):
    return {
        "fees": {"slippage_bps": 3},
        "execution": {"entry": entry, "exit": "limit_maker"},
        "risk": {
            "kelly_fraction": 0.25,
            "kelly_win_prob": 0.55,
            "kelly_payoff": 1.0,
        },
    }


class QuoteTests(unittest.TestCase):
    def test_cost_is_fees_plus_half_spread_plus_slippage(self):
        cost = execution_cost(cfg(), maker=0.0025, taker=0.003, spread_bps=10)
        half = 10 / 2 / 10_000
        self.assertAlmostEqual(cost, 0.0025 + 0.0025 + half + 0.0003)

    def test_marketable_entry_pays_the_taker_fee(self):
        cost = execution_cost(cfg("marketable_limit"), 0.0025, 0.003, spread_bps=0)
        self.assertAlmostEqual(cost, 0.003 + 0.0025 + 0.0003)

    def test_quarter_kelly_uses_the_prior_not_a_fitted_sample(self):
        # f* = 0.55 - 0.45/1 = 0.10; quarter-Kelly = 0.025
        self.assertAlmostEqual(fractional_kelly(cfg()), 0.025)
        self.assertIsNone(fractional_kelly({"risk": {}}))

    def test_inventory_skew_is_zero_when_flat_and_lowers_a_long_bid(self):
        self.assertEqual(inventory_skew(0, 0.01, 0.5, 24), 0.0)
        skew = inventory_skew(0.12, 0.01, 0.5, 24)
        self.assertAlmostEqual(skew, 0.12 * 0.5 * 0.0001 * 24)
        bid = maker_buy_limit(100.0, 100.2, 100.1, skew)
        self.assertLess(bid, 100.0)
        self.assertLess(bid, 100.2)

    def test_maker_quotes_do_not_cross(self):
        buy = maker_buy_limit(100.0, 100.2, 100.1, 0.0)
        sell = maker_sell_limit(100.0, 100.2, 100.1, 0.5)
        self.assertEqual(buy, 100.0)
        self.assertGreater(sell, 100.0)
        self.assertLessEqual(sell, 100.2)

    def test_top_of_book_imbalance_uses_best_quantities(self):
        book = OrderBook(
            "BTCTMN",
            bids=[BookLevel(100, 2), BookLevel(99, 100)],
            asks=[BookLevel(101, 1), BookLevel(102, 100)],
        )
        self.assertAlmostEqual(book.imbalance(), (2 - 1) / (2 + 1))


if __name__ == "__main__":
    unittest.main()
