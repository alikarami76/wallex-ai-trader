import json
import os
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from trader.config import load_config, runtime_dirs
from trader.health import configure_status, serve_in_background, update_status


class HealthTests(unittest.TestCase):
    def test_health_and_status_do_not_echo_secrets(self):
        root = Path("data/test_health")
        root.mkdir(parents=True, exist_ok=True)
        configure_status(root / "status.json")
        update_status(mode="shadow", dry_run=True, equity_tmn=5_000_000)
        server = serve_in_background("127.0.0.1", 0)
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"ok": True})
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as response:
                body = json.load(response)
            self.assertEqual(body["mode"], "shadow")
            self.assertTrue(body["dry_run"])
            self.assertEqual(body["equity_tmn"], 5_000_000)
            encoded = json.dumps(body)
            self.assertNotIn("WALLEX_API_KEY", encoded)
            self.assertNotIn("LLM_API_KEY", encoded)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/orders", timeout=2)
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_data_dir_is_the_disk_root(self):
        keys = (
            "TRADER_DATA_DIR",
            "TRADER_STATE_PATH",
            "TRADER_LOG_DIR",
            "TRADER_LLM_USAGE_PATH",
            "TRADER_SENTIMENT_CACHE",
        )
        saved = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["TRADER_DATA_DIR"] = "/tmp/wallex-disk"
            os.environ.pop("TRADER_STATE_PATH", None)
            os.environ.pop("TRADER_LOG_DIR", None)
            os.environ.pop("TRADER_LLM_USAGE_PATH", None)
            os.environ.pop("TRADER_SENTIMENT_CACHE", None)
            out, state, logs = runtime_dirs(None, "shadow", True)
            self.assertEqual(out, Path("/tmp/wallex-disk"))
            self.assertEqual(state, Path("/tmp/wallex-disk/state.json"))
            self.assertEqual(logs, Path("/tmp/wallex-disk"))
            cfg = load_config("config/default.yaml")
            self.assertEqual(cfg["llm"]["usage_path"], "/tmp/wallex-disk/llm_usage.json")
            self.assertEqual(cfg["sentiment"]["cache_path"], "/tmp/wallex-disk/sentiment_cache.json")
            os.environ["TRADER_STATE_PATH"] = "/tmp/wallex-disk/positions.json"
            os.environ["TRADER_LOG_DIR"] = "/tmp/wallex-disk/journal"
            _out, state, logs = runtime_dirs(None, "live", True)
            self.assertEqual(state, Path("/tmp/wallex-disk/positions.json"))
            self.assertEqual(logs, Path("/tmp/wallex-disk/journal"))
            explicit, _state, _logs = runtime_dirs("/tmp/other", "shadow", True)
            self.assertEqual(explicit, Path("/tmp/other"))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
