"""Walk-forward summaries and a deflated Sharpe ratio.

Bailey and López de Prado (2014) deflate a Sharpe ratio by the Sharpe you
would expect from the best of N uncorrelated trials under a zero-true-Sharpe
null, and by the skew and kurtosis of the return series. N here is the number
of strategy families that were looked at before this one was locked. It is
not a claim that those families were a formal grid.
"""

from __future__ import annotations

import math

EULER = 0.5772156649015329


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal via bisection. Accurate to about 1e-9."""
    if p <= 0.0 or p >= 1.0:
        raise ValueError("probability must be inside (0, 1)")
    target = 2.0 * p - 1.0
    lo, hi = -12.0, 12.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if math.erf(mid / math.sqrt(2.0)) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _moments(returns: list[float]) -> tuple[float, float, float, float]:
    n = len(returns)
    mean = sum(returns) / n
    var = sum((item - mean) ** 2 for item in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return mean, 0.0, 0.0, 3.0
    skew = sum(((item - mean) / std) ** 3 for item in returns) * n / ((n - 1) * (n - 2))
    kurt = sum(((item - mean) / std) ** 4 for item in returns) / n
    return mean, std, skew, kurt


def deflated_sharpe(returns: list[float], n_trials: int) -> dict:
    """Non-annualized Sharpe, the selection hurdle, and the deflated probability.

    Returns an empty-result dict when the series is too short or has no variance.
    """
    clean = [float(item) for item in returns if item is not None and math.isfinite(item)]
    trials = max(2, int(n_trials))
    if len(clean) < 10:
        return {"ok": False, "reason": "fewer than 10 returns"}
    mean, std, skew, kurt = _moments(clean)
    if std == 0:
        return {"ok": False, "reason": "zero variance", "observations": len(clean)}
    sharpe = mean / std
    # Variance of the Sharpe estimator (Bailey and López de Prado, eq. for V[SR]).
    inside = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * sharpe ** 2
    if inside <= 0:
        return {"ok": False, "reason": "sharpe variance was not positive", "sharpe": sharpe}
    sr_variance = inside / (len(clean) - 1)
    z1 = norm_ppf(1.0 - 1.0 / trials)
    z2 = norm_ppf(1.0 - 1.0 / (trials * math.e))
    hurdle = math.sqrt(sr_variance) * ((1.0 - EULER) * z1 + EULER * z2)
    denominator = math.sqrt(inside)
    probability = norm_cdf((sharpe - hurdle) * math.sqrt(len(clean) - 1) / denominator)
    return {
        "ok": True,
        "observations": len(clean),
        "n_trials": trials,
        "sharpe_per_bar": round(sharpe, 4),
        "skew": round(skew, 4),
        "kurtosis": round(kurt, 4),
        "hurdle_sharpe": round(hurdle, 4),
        "deflated_sharpe_probability": round(probability, 4),
    }
