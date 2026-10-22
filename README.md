# Wallex AI trader

Fee-aware spot bot for the Starcoach × Wallex credit account (package 183, 5,000,000 TMN). One process and one CLI. The backtest is **rules only**: it runs the quant and risk layers with the model off, and it keeps the researched 0.9% gross prior. Shadow and live add the menu and the BoxAPI call, and they block new entries on a frozen 0.45% prior unless `ev_override` was locked before go-live. Orders go only to spot `POST /v1/account/orders`. The config allowlist is intersected with the live package list, and TON, NEAR, FET, INJ, TAO, WIF, BONK, PENDLE, and RENDER are denied in every quote.

A forced exit under 50,000 TMN tops up with a spot buy and sells the combined amount. Coin that cannot be flattened is reported. Shadow is the default mode in `scripts/run.sh`.

In shadow and live the model picks one id from a menu of actions the quant and risk layers already validated (hold, buy, sell, reduce, or cancel). Invalid output is hold. Stops, size caps, the drawdown kill switch, and the Tehran liquidation window sit outside the model and can still veto the pick. If BoxAPI is down, over the call cap, or the key is missing, a deterministic fallback keeps the loop running.

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
| `LLM_MODEL` | unused | Ignored. It does not pin every tier. |
| `LLM_MODEL_CHEAP` | optional | Overrides routine, signal, and trade. Default `openai/gpt-6-luna`. |
| `LLM_MODEL_STRONG` | optional | Overrides the rare high-stakes tier. Default `openai/gpt-6-sol`. |

The client omits `temperature` unless `llm.send_temperature: true`. A completion cap is sent as `max_completion_tokens` (`gpt-6-luna` rejects `max_tokens`). `reasoning_effort` is retried once without the field on HTTP 400. Each call logs `usage.cost` and the running total against the $3 budget. The hard cap defaults to $2.50: the router steps down a tier near it, then stops and uses the deterministic fallback. `/key` and `/credits` report a shared upstream account, so the bot does not use them as its balance.

List price per 1M tokens, checked on BoxAPI on 2026-10-22. A decision is about 3,000 input tokens and 500 output tokens. Live bills run about 15% above list, and reasoning tokens bill as output.

| Model | List in / out | ~3k + ~500, list | With ~15% |
| --- | --- | --- | --- |
| `openai/gpt-6-luna` | $0.10 / $0.50 | $0.00055 | ≈ $0.0006 |
| `openai/gpt-6-sol`, `openai/gpt-6.1-sol` | $2 / $10 | $0.011 | ≈ $0.013 |
| `openai/gpt-6-astra` | $10 / $50 | $0.055 | ≈ $0.063 |
| `openai/gpt-5-nano` | $0.05 / $0.40 | $0.00035 | ≈ $0.0004 |

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

# Offline checkpoint draft from local jsonl. No key and no LLM call.
python3 -m trader report --checkpoint C0 --log-dir results/shadow

# Re-parse saved prompts and responses. Does not call BoxAPI.
python3 -m trader replay --log-dir results/live
```

`./scripts/run.sh shadow --duration 900 --fresh` loads `.env` and calls the same CLI.

Liara deployment is in [deploy/LIARA.md](deploy/LIARA.md). The image listens on port 8080 for `/health` and `/status` and runs the shadow loop beside it. State goes to `TRADER_DATA_DIR` so a Liara disk keeps the decision log across restarts. Secrets stay in `liara env set`.

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

The sample already in the repo is `results/backtest/` (about 29 days of hourly candles, deterministic agent, volume liquidity score, Fear & Greed ablation, walk-forward and deflated Sharpe) and `results/shadow/` (a short live-book run, no orders). On 2026-10-22 the backtest finished at −0.140% mark-to-market with a 0.415% max drawdown, and the deflated Sharpe probability was 0.043. Lagged Fear & Greed stayed between 53 and 78, so the extreme-greed gate (80) never fired and was left off. Depth and spread shocks are live-only. Numbers and citations are in [STRATEGY.md](STRATEGY.md).
