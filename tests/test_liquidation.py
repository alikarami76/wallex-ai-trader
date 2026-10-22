import unittest
from datetime import datetime, timedelta

from trader.risk.manager import hours_until_flat, liquidation_phase
from trader.timeutil import TEHRAN


CFG = {
    "liquidation": {
        "deadline": "2026-10-23T23:50:00+03:30",
        "start_before_minutes": 60,
        "aggressive_before_minutes": 30,
        "flat_by": "2026-10-23T23:30:00+03:30",
        "min_hours_to_enter": 8,
    }
}


def tehran(hour: int, minute: int, day: int = 23) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TEHRAN)


class LiquidationTests(unittest.TestCase):
    def test_phases_around_the_deadline(self):
        self.assertEqual(liquidation_phase(tehran(22, 49), CFG), "normal")
        self.assertEqual(liquidation_phase(tehran(22, 50), CFG), "passive")
        self.assertEqual(liquidation_phase(tehran(23, 19), CFG), "passive")
        self.assertEqual(liquidation_phase(tehran(23, 20), CFG), "aggressive")
        self.assertEqual(liquidation_phase(tehran(23, 50), CFG), "aggressive")
        self.assertEqual(liquidation_phase(tehran(0, 10, day=24), CFG), "aggressive")

    def test_entry_window_closes_eight_hours_before_flat(self):
        flat = tehran(23, 30)
        self.assertGreater(hours_until_flat(flat - timedelta(hours=8, seconds=1), CFG), 8)
        self.assertAlmostEqual(hours_until_flat(flat - timedelta(hours=8), CFG), 8)
        self.assertLess(hours_until_flat(flat - timedelta(hours=8) + timedelta(seconds=1), CFG), 8)
        self.assertLess(hours_until_flat(tehran(22, 50), CFG), 8)


if __name__ == "__main__":
    unittest.main()
