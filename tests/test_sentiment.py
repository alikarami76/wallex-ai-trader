import json
import unittest
import urllib.request
from pathlib import Path

from trader.sentiment import SentimentFeed, fear_greed_at, lexicon_score
from trader.strategy.agent import _price, llm_settings


class _Body:
    def __init__(self, text: str):
        self.text = text.encode()

    def read(self):
        return self.text

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def opener(request, timeout=0):
    url = request.full_url
    if "fng" in url:
        payload = {"data": [
            {"value": "25", "value_classification": "Fear", "timestamp": "1700000000"},
            {"value": "82", "value_classification": "Extreme Greed", "timestamp": "1700086400"},
        ]}
        return _Body(json.dumps(payload))
    if "reddit" in url or "cryptopanic" in url:
        raise urllib.error.URLError("blocked")
    rss = """<?xml version="1.0"?><rss><channel>
      <item><title>Bitcoin rally continues after the etf approval</title></item>
      <item><title>Exchange hack drains a smaller token</title></item>
    </channel></rss>"""
    return _Body(rss)


class SentimentTests(unittest.TestCase):
    def test_lexicon_scores_a_rally_and_a_hack(self):
        up, hits = lexicon_score("Bitcoin rally and etf approval")
        down, down_hits = lexicon_score("Exchange hack and bankruptcy")
        self.assertGreater(up, 0)
        self.assertGreater(hits, 0)
        self.assertLess(down, 0)
        self.assertGreater(down_hits, 0)
        self.assertEqual(lexicon_score("the weather is mild")[1], 0)

    def test_fear_greed_uses_only_values_known_after_the_lag(self):
        series = [(1_700_000_000, 25.0, "Fear"), (1_700_086_400, 82.0, "Extreme Greed")]
        value, change, label = fear_greed_at(series, 1_700_086_400)
        self.assertEqual((value, change, label), (25.0, None, "Fear"))
        value, change, label = fear_greed_at(series, 1_700_086_400 + 86400)
        self.assertEqual(value, 82.0)
        self.assertEqual(change, 57.0)
        self.assertEqual(label, "Extreme Greed")

    def test_failed_sources_do_not_raise_and_rss_still_scores(self):
        path = Path("/tmp/sentiment-test-cache.json")
        if path.exists():
            path.unlink()
        feed = SentimentFeed({"sentiment": {"cache_path": path, "cache_seconds": 0, "history_cache_seconds": 0}}, opener=opener)
        history = feed.load_history()
        self.assertEqual(len(history), 2)
        snap = feed.snapshot(["BTCTMN", "USDTTMN"])
        self.assertEqual(snap.fear_greed, 82.0)
        self.assertEqual(snap.fear_greed_change, 57.0)
        self.assertIn("reddit", snap.sources_failed)
        self.assertIn("cryptopanic", snap.sources_failed)
        self.assertIn("coindesk", snap.sources_ok)
        self.assertGreater(snap.news_score["BTCTMN"], 0)
        self.assertIsNone(snap.news_score["USDTTMN"])
        self.assertTrue(any("Bitcoin" in title for title in snap.headlines["BTCTMN"]))

    def test_usage_cost_overrides_the_token_estimate(self):
        priced = _price(
            {"cost": "3e-6", "prompt_tokens": 5000, "completion_tokens": 5000, "model": "gpt-6-luna"},
            llm_settings({}),
            "gpt-6-luna",
        )
        self.assertEqual(priced["cost_source"], "usage.cost")
        self.assertAlmostEqual(priced["usd"], 3e-6)
        self.assertEqual(llm_settings({})["model"], "openai/gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
