"""The scripted dip has to clear the real signal rules before the mock loop runs."""

import unittest

from trader.config import load_config
from trader.e2e.series import signal_blockers


class ShapeTests(unittest.TestCase):
    def test_dip_clears_and_flat_does_not(self):
        cfg = load_config("config/default.yaml")
        self.assertEqual(signal_blockers(cfg, "dip"), [])
        self.assertTrue(signal_blockers(cfg, "flat"))
