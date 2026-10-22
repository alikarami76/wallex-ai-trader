# Wallex backtest

| Metric | Value |
| --- | --- |
| Starting equity | 5,000,000 TMN |
| Ending equity | 4,942,865 TMN |
| Net return | -1.143% |
| Max drawdown | 2.153% |
| Sharpe (annualized from the equity spacing) | -2.19 |
| Round trips | 26 |
| Win rate | 53.8% |
| Average closed-trade PnL | -846 TMN |
| Fees paid | 91,840 TMN |
| Fills | 54 |
| Decision cycles | 706 |
| Agent sources | fallback=695 |
| Logged errors | 0 |

Equity samples: 695.

![Equity](equity.png)

![Drawdown](drawdown.png)

The return is mark-to-market in toman, including fees. A closed trade wins when its realized toman PnL is positive. Sharpe uses the median spacing of the equity samples, so an hourly backtest and a 45-second shadow run are not on the same scale.
