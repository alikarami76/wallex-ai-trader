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
| `DASHBOARD_USERS` | optional | JSON object `{"username": "password"}` for several desk users. Compared in constant time. |
| `DASHBOARD_USER` | desk only | Fallback HTTP basic user when `DASHBOARD_USERS` is unset. |
| `DASHBOARD_PASSWORD` | desk only | Password for that fallback user. The page stays closed when no user is configured. |
| `TELEGRAM_ALERT_BOT_TOKEN` | optional | Alert bot token. Alerts stay off unless this and the chat id are both set. |
| `TELEGRAM_ALERT_CHAT_ID` | optional | Chat that receives alerts. The token is not written to the journal. |

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

# Offline veto scorecard. No keys. Reads the journal and the stored candles.
python3 -m trader scorecard

# Replay the stored candles and diff the trade list against a recorded commit.
# Does not rewrite results/backtest/. The second ref may be another commit.
python3 -m trader backtest --compare 3880ac429f56f652cffd871d11dc5b05a1bd70ce HEAD

# Offline journal audit. No keys. Writes results/audit.md and results/audit.json.
python3 -m trader audit --log-dir results/shadow

# Optional sidecar. Off unless WATCHDOG_ENABLED=1. Does not write journals.
# WATCHDOG_ENABLED=1 python3 -m trader watchdog --log-dir results/live --once
```

`./scripts/run.sh shadow --duration 900 --fresh` loads `.env` and calls the same CLI.

Liara deployment is in [deploy/LIARA.md](deploy/LIARA.md). The image listens on port 8080. `/health` is open for the platform check. `/` is a read-only desk and `/status` is the latest cycle snapshot. Both accept `DASHBOARD_USERS` (`{"username": "password"}`) and the `DASHBOARD_USER` / `DASHBOARD_PASSWORD` fallback. The desk tails `cycles.jsonl`, `decisions.jsonl`, `adjustments.jsonl`, and `reviews.jsonl` on `TRADER_DATA_DIR`. It has no route that places or cancels orders, changes config, or starts or stops the loop. Secrets stay in `liara env set`.

## Safety architecture

These checks can only refuse an order, shrink a size, or write a log. They do not raise a size, add a symbol, or change a strategy number: EMA windows, return and pullback bands, ATR cap, spread cap, −4.5% stop, trail arm +2.5% with 1.2% distance, confidence floor 0.55, 12% size cap, 30% gross cap, frozen prior 0.45%, kill levels −1.5% and −3%. Ali approved two clock changes for the live book: the hold is 6 hours, and the entry cutoff is Fri 2026-10-23 18:00 Asia/Tehran. `flat_by` stays Fri 2026-10-23 23:30. A time stop also exits at `flat_by`. `ev_override`, once locked on, stays on. `TRADER_APPROVED_CONFIG_HASH` re-freezes the go-live config hash for that file. The C0 report, the state machine, B1–B10, and the A0–A8 adjustment enum are unchanged. There are no new model calls. The $2.50 BoxAPI cap is unchanged.

### Pre-send order guard

`assess()` in `trader/order_guard.py` is a pure function. The live loop calls it after the price and quantity are known and before every order request. The request is sent only when all of these hold:

- the market is spot TMN
- the symbol is on the package-183 allowlist and is not on the denylist
- a BUY is allowed only in `LIVE_ARMED`, and a SELL quantity is at most the held quantity
- an entry is a post-only maker LIMIT
- notional is at least 50,000 TMN
- after the fill, that symbol is within the size cap and gross exposure is within the gross cap
- the endpoint path is `/v1/account/orders`

A refusal is not sent, does not advance the client-id sequence, and does not change the ops state. The symbol row records gate `order_guard` on `gate_trace` and `first_block`. No module under `trader/` names a withdraw endpoint.

### Model off the exit path

Stop, trail, and flatten actions run before `agent.decide`. That prefix does not wait on BoxAPI, so a hung completion cannot delay a protective exit. A buy that was not already on the menu from before those exits is dropped, so a stop that frees a slot cannot open a new entry in the same cycle.

A hard timeout returns the deterministic fallback and stores `llm_source=fallback`. That fallback may still buy: it is the quant ranking, and the model is not required for the entry. Output that fails the JSON schema is a veto: no decisions and no quant buy. Once `llm_usd_total` reaches $2.25 the client skips the call, logs the fallback, and uses that same deterministic path, which may still buy. The model can only veto or shrink a candidate the rules already ranked. The $2.00 tier step-down and the $2.50 hard cap still apply underneath. A menu that is only HOLD still skips the model. Invalid menu JSON stays HOLD.

### Sealed scorecard

At decision time each symbol row stores `prompt_hash`, `response_hash`, `action`, `size_mult`, and the signal snapshot. `python -m trader scorecard` is offline and needs no keys. It reads the journal and `results/backtest/candles`, then writes `results/scorecard.md` and `results/scorecard.json`. For each vetoed, shrunk, and accepted signal it computes after-fee PnL against a rules-only baseline, exiting at the configured hold or at the stop or trail, whichever comes first. The hold is 6 hours. Shadow-mode rows are counted.

### Decision card

The read-only desk and `python -m trader report` show one card per symbol on the latest cycle: features, `first_block`, the cost-gate inputs (prior, fee in, fee out, half-spread, buffer, hurdle), the model source, confidence, and rationale, and `size_before` → `size_after` with the reason codes. Prompts and keys are not rendered.

### Alerts

When both `TELEGRAM_ALERT_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` are set, the process can alert on a cycle gap over 5 minutes, a `HALT` or `KILLED` state, an exchange order reject, or a taker entry fill. The same alert key stays quiet for 5 minutes after a send. If either variable is unset, nothing is sent. A failed send logs the exception type and not the request URL.

### Compare

`python -m trader backtest --compare <sha_a> <sha_b>` diffs the two trade lists and stamps both SHAs and the config hash. `HEAD` replays the stored candles. Any other second ref uses the trade list committed at that SHA. A sensitivity table reruns the same candles at 1.0× and 1.5× fees, each with 0 and +5 bps extra slippage. Those multiples are copies; `config/default.yaml` is not edited, and `results/backtest/` is not rewritten.

The recorded book on the stored candles, with the 6-hour hold, is −0.335% mark-to-market, 23 closed trades, ending equity 4,983,233.3104847735 TMN. The compare test locks those trade-list bytes. A bar that trades through both the stop and a target exits at the stop. That order was already the engine's behavior.

Random model text cannot raise a size or turn a veto into a buy. The order-guard property tests and the schema veto cover that. Nothing in this section sets `TRADER_LIVE_AUTHORIZED`.

## Audit and evaluation

These commands read journals and stored candles. They do not place orders, call the model, or change C0, the state machine, or B1–B10.

### Journal audit

`python -m trader audit` reads the jsonl files and writes `results/audit.md` and `results/audit.json`. It rebuilds TMN and coin balances from fills and fees and flags equity drift over 1 TMN. `--wallex-snapshot` compares that book with a balances file. It also checks duplicate `client_id`s, cycle gaps over 5 minutes, `ts_utc` against `ts_tehran` (+03:30), and that every submitted order has an all-pass `gate_trace`, `git_sha` equal to `go_live_sha`, and the config hash. Each fill is listed with maker or taker and slippage versus mid, in bps. The fee bill is its own line. A regex hit for a secret-like string fails the audit, and the match is not copied into the report. `python -m trader report --checkpoint C0` (and C1–C4) appends this summary under the playbook text. The suggested action is unchanged.

The shadow journal audits clean: no fills, 0 TMN drift, no secret hits.

### Random-entry baseline

The backtest report includes 1,000 seeded runs on the same universe and denylist, with the same configured hold, stop, trail, fees, and size. Only the entry times are random. On the recorded 6-hour book the strategy return of −0.335% is at the 92.1 percentile of those runs, and the Sharpe of −4.41 is at the 68.1 percentile. Return quantiles are −2.427 / −1.726 / −1.216 / −0.849 / −0.235 percent at p05 / p25 / p50 / p75 / p95. Sharpe quantiles are −9.85 / −7.31 / −5.46 / −3.70 / −1.10. The numbers are in `results/backtest/baseline.json`.

### Watchdog

`trader/watchdog.py` is a separate process. It tails `cycles.jsonl` and `decisions.jsonl` and can send one Telegram alert for a cycle gap over 5 minutes, `HALT` or `KILLED`, rejected orders above zero, or taker entry fills above zero. The same event type is quiet for 15 minutes. The message is the event and the percent PnL. The token comes from `TELEGRAM_ALERT_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID`. The process writes nothing under the journal directory. It stays off unless `WATCHDOG_ENABLED` is set. The container entrypoint starts it beside the trader when that variable is `1`. Shadow still boots with `WALLEX_API_KEY` unset, logs that the key is absent, and keeps serving `/health` and the desk.

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
3. `make e2e` runs the mock-exchange scenarios (`python3 -m trader e2e`). No Wallex key.
4. `python3 -m trader --mode backtest --config config/default.yaml`
5. `python3 -m trader --mode shadow --config config/default.yaml --duration 900 --fresh --out results/shadow`

No API key is required for 1–5. Shadow uses public REST only. Backtest forces the deterministic agent so a stray `LLM_API_KEY` cannot spend the budget. Charts land next to the journal: `report.md`, `equity.png`, `drawdown.png`, `trades.csv`, `equity.csv`, `decisions.jsonl`.

The sample already in the repo is `results/backtest/` (about 29 days of hourly candles, deterministic agent, volume liquidity score, Fear & Greed ablation, walk-forward and deflated Sharpe) and `results/shadow/` (a short live-book run, no orders). On 2026-10-22 the backtest finished at −0.140% mark-to-market with a 0.415% max drawdown, and the deflated Sharpe probability was 0.043. Lagged Fear & Greed stayed between 53 and 78, so the extreme-greed gate (80) never fired and was left off. Depth and spread shocks are live-only. Numbers and citations are in [STRATEGY.md](STRATEGY.md).
