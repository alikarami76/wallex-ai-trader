# Wallex shadow report

## Desk metrics

| Metric | Value |
| --- | --- |
| PnL after fees (mark-to-market) | 0 TMN (0.000%) |
| PnL after fees (realized) | 0 TMN |
| Sharpe (annualized from the equity spacing) | 0.00 |
| Max drawdown | 0.000% |
| Hit rate | 0.0% (0 closed trades) |
| Decision latency | median 4274 ms, p95 5366 ms (15 cycles) |

| Other | Value |
| --- | --- |
| Starting equity | 5,000,000 TMN |
| Ending equity | 5,000,000 TMN |
| Average closed-trade PnL | 0 TMN |
| Fees paid | 0 TMN |
| Fills | 0 |
| Decision cycles | 17 |
| Agent sources | fallback=15 |
| Logged errors | 0 |

Equity samples: 15.

![Equity](equity.png)

![Drawdown](drawdown.png)

The return is mark-to-market in toman, including fees. Hit rate counts a closed trade as a win when its realized toman PnL is positive. Sharpe uses the median spacing of the equity samples, so an hourly backtest and a short shadow run are not on the same scale. Latency is the decision cycle, from the book fetch through the risk plan.

