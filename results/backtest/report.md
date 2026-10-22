# Wallex backtest

## Desk metrics

| Metric | Value |
| --- | --- |
| PnL after fees (mark-to-market) | -16,767 TMN (-0.335%) |
| PnL versus 5,000,000 TMN base | -16,767 TMN |
| PnL after fees (realized) | -16,767 TMN |
| Sharpe (annualized from the equity spacing) | -4.41 |
| Max drawdown | 0.491% |
| Hit rate | 47.8% (23 closed trades) |
| Decision latency | n/a |

| Other | Value |
| --- | --- |
| Starting equity | 5,000,000 TMN |
| Ending equity | 4,983,233 TMN |
| Average closed-trade PnL | -729 TMN |
| Fees paid | 12,660 TMN |
| Fills | 46 |
| Decision cycles | 741 |
| Agent sources | fallback=695 |
| Logged errors | 0 |

Equity samples: 695.

Residual coin: none.



![Equity](equity.png)

![Drawdown](drawdown.png)

The return is mark-to-market in toman, including fees. Hit rate counts a closed trade as a win when its realized toman PnL is positive. Sharpe uses the median spacing of the equity samples, so an hourly backtest and a short shadow run are not on the same scale. Latency is the decision cycle, from the book fetch through the risk plan.

## Walk-forward and deflated Sharpe

Full sample: per-bar Sharpe -0.0472, selection hurdle 0.0451 at N=6, deflated Sharpe probability 0.0039.

| Fold | Return | Realized PnL | Fees | Fold DSR |
| --- | --- | --- | --- | --- |
| 1 | -0.078% | -3924.18 | 9453.84 | 0.0526 |
| 2 | -0.149% | -7437.09 | 1145.17 | 0.0 |
| 3 | -0.142% | -7099.62 | 2611.56 | 0.0001 |

Folds use the same frozen parameters and a fresh 5,000,000 TMN. The deflated Sharpe probability is Bailey and López de Prado (2014): the chance the observed per-bar Sharpe beats the expected maximum of N null trials, after skew and kurtosis. It is not an annualized Sharpe.

## Random-entry baseline

1000 seeded runs. Entries are random times on the same universe, with the denylist, the configured hold, stop, trail, fees, and size cap left as configured. Each run attempts 23 entries.

Strategy return -0.335% is at the 92.1 percentile of the random-entry returns. Strategy Sharpe -4.41 is at the 68.1 percentile.

|  | p05 | p25 | p50 | p75 | p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| return % | -2.427 | -1.726 | -1.216 | -0.849 | -0.235 |
| Sharpe | -9.85 | -7.31 | -5.46 | -3.70 | -1.10 |

