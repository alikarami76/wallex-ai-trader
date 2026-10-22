import unittest

from trader.strategy.parser import parse_agent_output
from trader.strategy.signals import Candidate
from trader.strategy.agent import fallback_decision


class ParserTests(unittest.TestCase):
    def test_plain_and_fenced_json(self):
        text = """```json
        {"veto_all": false, "regime": "trend", "comment": "one name",
         "decisions": [{"action": "buy", "symbol": "btctmn", "size_fraction": 0.08,
                        "confidence": 0.72, "veto": false, "rationale": "24h trend is intact"}]}
        ```"""
        parsed = parse_agent_output(text)
        self.assertEqual(parsed.decisions[0].action, "BUY")
        self.assertEqual(parsed.decisions[0].symbol, "BTCTMN")
        self.assertAlmostEqual(parsed.decisions[0].size_fraction, 0.08)
        self.assertFalse(parsed.veto_all)

    def test_garbage_raises_for_the_caller_to_fall_back(self):
        with self.assertRaises(ValueError):
            parse_agent_output("I think we should buy bitcoin.")

    def test_rejects_overconfident_and_unknown_actions(self):
        with self.assertRaises(ValueError):
            parse_agent_output('{"decisions": [{"action": "YOLO", "symbol": "BTCTMN", "confidence": 0.5}]}')
        with self.assertRaises(ValueError):
            parse_agent_output('{"decisions": [{"action": "BUY", "symbol": "BTCTMN", "confidence": 1.4}]}')

    def test_veto_all_flag(self):
        parsed = parse_agent_output('{"veto_all": true, "regime": "hostile", "decisions": []}')
        self.assertTrue(parsed.veto_all)
        self.assertEqual(parsed.decisions, [])

    def test_fallback_picks_the_highest_score_and_labels_itself(self):
        first = _candidate("ETH TMN", 0.2)
        first.symbol = "ETHTMN"
        second = _candidate("BTCTMN", 0.8)
        decision = fallback_decision([second, first])
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.decisions[0].symbol, "BTCTMN")
        self.assertIn("DETERMINISTIC_FALLBACK", decision.decisions[0].rationale)
        self.assertGreaterEqual(decision.decisions[0].confidence, 0.55)
        empty = fallback_decision([])
        self.assertEqual(empty.decisions, [])


def _candidate(symbol: str, score: float) -> Candidate:
    return Candidate(
        symbol=symbol, setup="uptrend_pullback", size_fraction=0.12, score=score,
        expected_gross=0.009, round_trip_cost=0.0058, roc_trend=0.02, roc_pullback=-0.008,
        close=100, bar_time=0, facts={},
    )


if __name__ == "__main__":
    unittest.main()
