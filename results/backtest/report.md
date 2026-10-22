# Wallex backtest

## Desk metrics

| Metric | Value |
| --- | --- |
| PnL after fees (mark-to-market) | -6,639 TMN (-0.133%) |
| PnL after fees (realized) | -349 TMN |
| Sharpe (annualized from the equity spacing) | -1.19 |
| Max drawdown | 0.436% |
| Hit rate | 58.8% (17 closed trades) |
| Decision latency | n/a |

| Other | Value |
| --- | --- |
| Starting equity | 5,000,000 TMN |
| Ending equity | 4,993,361 TMN |
| Average closed-trade PnL | -21 TMN |
| Fees paid | 11,736 TMN |
| Fills | 36 |
| Decision cycles | 731 |
| Agent sources | fallback=695 |
| Logged errors | 0 |

Equity samples: 695.

![Equity](equity.png)

![Drawdown](drawdown.png)

The return is mark-to-market in toman, including fees. Hit rate counts a closed trade as a win when its realized toman PnL is positive. Sharpe uses the median spacing of the equity samples, so an hourly backtest and a short shadow run are not on the same scale. Latency is the decision cycle, from the book fetch through the risk plan.

## Walk-forward and deflated Sharpe

Full sample: per-bar Sharpe -0.0127, selection hurdle 0.0484 at N=6, deflated Sharpe probability 0.0502.

| Fold | Return | Realized PnL | Fees | Fold DSR |
| --- | --- | --- | --- | --- |
| 1 | 0.136% | 6809.93 | 9182.35 | 0.1745 |
| 2 | -0.159% | -7957.95 | 1909.83 | 0.0015 |
| 3 | -0.16% | -8024.62 | 2566.83 | 0.0027 |

Folds use the same frozen parameters and a fresh 5,000,000 TMN. The deflated Sharpe probability is Bailey and López de Prado (2014): the chance the observed per-bar Sharpe beats the expected maximum of N null trials, after skew and kurtosis. It is not an annualized Sharpe.
