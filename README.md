# Wallex AI trader

Fee-aware spot bot for the Starcoach × Wallex credit account (package 183, 5,000,000 TMN). One process, one CLI, the same decision code in backtest, shadow, and live.

The model ranks or vetoes candidates the quantitative layer already proposed. Stops, size caps, the drawdown kill switch, and the Tehran liquidation window sit outside the model. If BoxAPI is down, over the call cap, or the key is missing, a deterministic fallback keeps the loop running.

## Setup

Python 3.11+.

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

| Variable | Required | Purpose |
| --- | --- | --- |
| `WALLEX_API_KEY` | live orders only | `X-API-Key`. Leave empty for backtest, shadow, and `--dry-run`. |
| `WALLEX_SUBACCOUNT_ID` | optional | Sent as `sub-account-client-id` when the credit sub-account key needs it. |
| `LLM_BASE_URL` | live/shadow AI | Default `https://ai.boxapi.ir/ai/v1`. |
| `LLM_API_KEY` | live/shadow AI | BoxAPI bearer token. Empty means the deterministic fallback. |
| `LLM_MODEL` | live/shadow AI | Default `gpt-6-luna`. Also allowed: `gpt-5-mini`, `gpt-5.4-mini`, `gpt-5.6-sol`, `gpt-6-sol`, `gpt-6.1-sol`. |

The client omits `temperature` unless `llm.send_temperature: true` in the YAML. Spend uses the response field `usage.cost` when BoxAPI sends it. Call caps are in `config/default.yaml` (`max_llm_calls_per_hour: 2`, `max_llm_calls_total: 24`, `max_spend_usd: 2.50`). The competition budget is $3.

## Commands

```bash
python3 -m unittest discover -s tests -v

# Historical Wallex hourly candles, fees and slippage. Writes results/backtest/.
python3 -m trader --mode backtest --config config/default.yaml

# Live public data, paper fills, no orders. 15 minutes.
python3 -m trader --mode shadow --config config/default.yaml --duration 900 --interval 45 --fresh --out results/shadow

# Build the POST body from live rules and print it. Does not send.
python3 -m trader --mode check-order --symbol BTCTMN --side BUY --notional 100000

# One live cycle that builds requests and does not send them.
python3 -m trader --mode live --config config/default.yaml --dry-run --once

# Real orders. Needs WALLEX_API_KEY. Flattens into TMN before 2026-10-23 23:30 Tehran.
python3 -m trader --mode live --config config/default.yaml

python3 -m trader --mode report --log-dir results/backtest
```

`./scripts/run.sh shadow --duration 900 --fresh` loads `.env` and calls the same CLI.

## Unattended run

```bash
mkdir -p data/live
nohup python3 -m trader --mode live --config config/default.yaml >> data/live/run.log 2>&1 &
echo $! > data/live/trader.pid
```

A systemd unit is in `deploy/wallex-trader.service`. Point `WorkingDirectory` and `EnvironmentFile` at the checkout, then `systemctl enable --now wallex-trader`. The process reloads state from `state.json`, reconciles balances and open orders, and resumes. Cycle errors are logged and retried with backoff. They do not exit the process.

## What a judge can rerun

1. `python3 -m pip install -r requirements.txt`
2. `python3 -m unittest discover -s tests -v`
3. `python3 -m trader --mode backtest --config config/default.yaml`
4. `python3 -m trader --mode shadow --config config/default.yaml --duration 900 --fresh --out results/shadow`

No API key is required for 1–4. Shadow uses public REST only. Backtest forces the deterministic agent so a stray `LLM_API_KEY` cannot spend the budget. Charts land next to the journal: `report.md`, `equity.png`, `drawdown.png`, `trades.csv`, `equity.csv`, `decisions.jsonl`.

The sample already in the repo is `results/backtest/` (about 29 days of hourly candles, deterministic agent, walk-forward and deflated Sharpe) and `results/shadow/` (about 12 minutes of the live book, no orders). On 2026-10-22 the backtest finished at −0.133% mark-to-market with a 0.44% max drawdown, and the deflated Sharpe probability was 0.05. The shadow stayed flat. Numbers and citations are in [STRATEGY.md](STRATEGY.md).
