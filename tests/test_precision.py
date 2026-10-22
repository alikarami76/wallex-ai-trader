import unittest

from trader.wallex.precision import OrderRejected, build_order_body, format_number, round_price, round_qty


class PrecisionTests(unittest.TestCase):
    def test_qty_rounds_down_to_step(self):
        self.assertEqual(str(round_qty(1.239, 2)), "1.23")
        self.assertEqual(str(round_qty(1500.9, 0)), "1500")
        self.assertEqual(format_number(round_qty(0.000000019, 8), 8), "0.00000001")

    def test_passive_buy_rounds_price_down_and_sell_up(self):
        self.assertEqual(str(round_price(270547.6, 0, "down")), "270547")
        self.assertEqual(str(round_price(270547.1, 0, "up")), "270548")
        self.assertEqual(str(round_price(1.21119, 4, "down")), "1.2111")
        self.assertEqual(str(round_price(1.21111, 4, "up")), "1.2112")

    def test_limit_body_is_strings_and_checks_min_notional(self):
        body = build_order_body(
            symbol="BTCTMN",
            side="BUY",
            order_type="LIMIT",
            quantity=0.00001234,
            price=22_154_099_984.7,
            client_id="SC_test_1",
            price_decimals=0,
            qty_decimals=8,
            min_qty=1e-8,
            min_notional=50_000,
            price_direction="down",
        )
        self.assertEqual(body["price"], "22154099984")
        self.assertEqual(body["quantity"], "0.00001234")
        self.assertIsInstance(body["price"], str)
        self.assertNotIn("stop_Price", body)

    def test_market_order_omits_price(self):
        body = build_order_body(
            symbol="USDTTMN",
            side="SELL",
            order_type="MARKET",
            quantity=1.239,
            price=270000,
            client_id="SC100",
            price_decimals=0,
            qty_decimals=2,
            min_qty=0.01,
            min_notional=50_000,
        )
        self.assertNotIn("price", body)
        self.assertEqual(body["quantity"], "1.23")
        self.assertEqual(body["type"], "MARKET")

    def test_rejects_small_notional_bad_id_and_tiny_qty(self):
        with self.assertRaises(OrderRejected):
            build_order_body(
                symbol="USDTTMN", side="BUY", order_type="LIMIT", quantity=0.1,
                price=270000, client_id="SC1", price_decimals=0, qty_decimals=2,
                min_qty=0.01, min_notional=50_000, price_direction="down",
            )
        with self.assertRaises(OrderRejected):
            build_order_body(
                symbol="USDTTMN", side="BUY", order_type="LIMIT", quantity=1,
                price=270000, client_id="bad-id", price_decimals=0, qty_decimals=2,
                min_qty=0.01, min_notional=50_000, price_direction="down",
            )
        with self.assertRaises(OrderRejected):
            build_order_body(
                symbol="PEPETMN", side="BUY", order_type="LIMIT", quantity=0.4,
                price=1.2, client_id="SC1", price_decimals=4, qty_decimals=0,
                min_qty=1, min_notional=50_000, price_direction="up",
            )


if __name__ == "__main__":
    unittest.main()
