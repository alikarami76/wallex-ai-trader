# Backtest compare

`3880ac429f56f652cffd871d11dc5b05a1bd70ce` versus `32aa7c1de902ab1b43931e54a0cc84cd53521e09`.
Config hash `ca79f960827676f3177a2b1940ee06c2b16bf77c5d75c3c9e39fedccc540fd0d`.
The second ref is HEAD, so its trade list is a replay of the stored candles.

Trades and PnL: **IDENTICAL**.

| | return | closed trades | ending equity | realized PnL | fees |
| --- | ---: | ---: | ---: | ---: | ---: |
| sha_a | -0.14% | 17 | 4993023.91105763 | -1037.642442369688 | 10242.265218794308 |
| sha_b | -0.14% | 17 | 4993023.91105763 | -1037.642442369688 | 10242.265218794308 |

## Sensitivity

Fee multiples and the extra slippage are applied only in this table. The strategy file is unchanged.

| fees | extra slippage | return | closed trades | realized PnL |
| ---: | ---: | ---: | ---: | ---: |
| 1.0× | +0 bps | -0.14% | 17 | -1037.642442369688 |
| 1.5× | +0 bps | 0.0% | 0 | 0.0 |
| 1.0× | +5 bps | -0.142% | 17 | -1158.9417619557353 |
| 1.5× | +5 bps | 0.0% | 0 | 0.0 |
