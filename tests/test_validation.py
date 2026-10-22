import unittest

from trader.engine.validation import deflated_sharpe, norm_ppf


class ValidationTests(unittest.TestCase):
    def test_normal_quantile_is_close_to_the_usual_value(self):
        self.assertAlmostEqual(norm_ppf(0.975), 1.95996398454, places=4)

    def test_many_trials_deflate_a_modest_sharpe(self):
        # A small positive drift. More trials must lower the deflated probability.
        returns = [0.001] * 40 + [-0.0004] * 60
        few = deflated_sharpe(returns, n_trials=2)
        many = deflated_sharpe(returns, n_trials=50)
        self.assertTrue(few["ok"] and many["ok"])
        self.assertGreater(many["hurdle_sharpe"], few["hurdle_sharpe"])
        self.assertLess(many["deflated_sharpe_probability"], few["deflated_sharpe_probability"])

    def test_zero_variance_is_reported_not_invented(self):
        result = deflated_sharpe([0.0] * 20, n_trials=6)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
