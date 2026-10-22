# Deploy on Liara

The competition process runs as a Docker app on [Liara](https://liara.ir). The image entrypoint is `python -m trader.entrypoint`. It starts the trading loop and an HTTP server on port 8080. When `WATCHDOG_ENABLED=1` it also starts `python -m trader watchdog` in the same container, tailing the journal on the disk. Liara's Docker platform publishes one HTTP port and curls it (`/health`) before it treats a release as healthy, so the loop alone is not enough. The same port serves a read-only desk at `/`.

Shadow is the default. The container reads public Wallex data and does not send orders. A missing `WALLEX_API_KEY` is logged as `WALLEX_API_KEY absent` and the process keeps serving `/health` and the desk. The log never includes the key value. Nothing in this repository is a secret. Set keys with `liara env set`.

These commands use `@liara/cli` 9.6.0. Do not commit the API token or the trading keys.

## One-time setup

```bash
npm install -g @liara/cli@9.6.0
liara --version
```

Headless login (CI or a machine with no browser). Create the token in the Liara console. The token is an environment variable, not a file in git.

```bash
liara login --api-token "$LIARA_API_TOKEN"
```

Create the Docker app, then a disk for the journal. The disk is what survives a restart.

```bash
liara app create --app wallex-ai-trader --platform docker
liara disk create --app wallex-ai-trader --name data --size 2
```

`--size` is gigabytes. Liara's own disk examples use at least 2.

## Environment

`liara.json` already sets the app id, the Docker platform, port 8080, the Iran build location, and the disk mount `/data`. The variables below are the ones the process reads. Replace the placeholders. Do not paste real keys into the shell history of a shared machine if you can avoid it; the console env screen is the same operation.

```bash
liara env set --app wallex-ai-trader --force \
  TRADER_MODE=shadow \
  TRADER_DATA_DIR=/data \
  TRADER_HEALTH_PORT=8080 \
  TZ=Asia/Tehran \
  LLM_BASE_URL=https://ai.boxapi.ir/ai/v1
```

Secrets are a separate command. Shadow runs with these unset. Pass the shell variables only when you have them; an empty `KEY=` would store a blank and overwrite a key you set earlier.

```bash
liara env set --app wallex-ai-trader --force \
  WALLEX_API_KEY="$WALLEX_API_KEY" \
  WALLEX_SUBACCOUNT_ID="$WALLEX_SUBACCOUNT_ID" \
  LLM_API_KEY="$LLM_API_KEY" \
  DASHBOARD_USER="$DASHBOARD_USER" \
  DASHBOARD_PASSWORD="$DASHBOARD_PASSWORD"
```

| Variable | Role |
| --- | --- |
| `TRADER_MODE` | `shadow` (default, no orders) or `live`. |
| `TRADER_DRY_RUN` | `1` forces live mode to build orders and not send them. |
| `TRADER_DATA_DIR` | Disk root. Default in the image is `/data`. |
| `TRADER_STATE_PATH` | Positions, kill switch, and pending orders. Default `$TRADER_DATA_DIR/state.json`. |
| `TRADER_LOG_DIR` | Decision log, trades, and equity. Default `$TRADER_DATA_DIR`. |
| `TRADER_LLM_USAGE_PATH` | LLM spend ledger. Default `$TRADER_DATA_DIR/llm_usage.json`. |
| `TRADER_SENTIMENT_CACHE` | Fear & Greed cache. Default `$TRADER_DATA_DIR/sentiment_cache.json`. |
| `TRADER_HEALTH_PORT` | HTTP port. Must match `liara.json` (`8080`). |
| `WALLEX_API_KEY` | `X-API-Key`. Required only for live orders. |
| `WALLEX_SUBACCOUNT_ID` | Optional `sub-account-client-id`. |
| `LLM_BASE_URL` | `https://ai.boxapi.ir/ai/v1`. |
| `LLM_API_KEY` | BoxAPI bearer token. Empty means the deterministic fallback. |
| `LLM_MODEL_CHEAP` | Optional. Unset keeps `openai/gpt-6-luna`. |
| `LLM_MODEL_STRONG` | Optional. Unset keeps `openai/gpt-6-sol` for the stakes tier. |
| `DASHBOARD_USERS` | Optional JSON object `{"username": "password"}` for several basic-auth users. Compared with `hmac.compare_digest`. |
| `DASHBOARD_USER` | Fallback basic-auth user for `/` and `/status` when you want one account. |
| `DASHBOARD_PASSWORD` | Password for that fallback user. Compared in constant time. |
| `TELEGRAM_ALERT_BOT_TOKEN` | Optional. Shared by alerts and the trade log. Alerts stay off unless this and `TELEGRAM_ALERT_CHAT_ID` are both set. |
| `TELEGRAM_ALERT_CHAT_ID` | Optional group for a cycle gap, HALT or KILLED, an order reject, or a taker entry fill. |
| `TELEGRAM_TRADES_CHAT_ID` | Optional channel for each BUY/SELL, fill, partial, cancel, and HOLD. Gordon Trades is `-1003898687684`. Unset means the trade log sends nothing. |
| `WATCHDOG_ENABLED` | Optional. Off unless this is `1`, `true`, `yes`, or `on`. The entrypoint then runs the journal sidecar in the same container. It does not place orders or write journals. |
| `TRADER_LIVE_AUTHORIZED` | `1` arms live orders. Leave it unset in the image. The running lock keeps the value already stored in `/data/state.json`. |
| `TRADER_EV_OVERRIDE` | `1` keeps the locked cost-gate override on. A re-freeze does not clear it. Leave it set across the redeploy. |
| `TRADER_APPROVED_CONFIG_HASH` | SHA-256 of `config/default.yaml` for one approved config change. When it matches the file in the new image, the go-live lock re-freezes that hash and the new git SHA and writes an adjustment row. A different file hash halts. `ev_override` is not changed. |
| `TRADER_GIT_SHA` | Commit recorded in the go-live lock. The image has no git checkout, so an unset value stores `unknown`. Set it to the deployed commit before the process starts. |

`LLM_MODEL` is ignored. It does not pin every tier.

## Deploy

This is the deploy command. Run it from the repository root after `liara login`. It does not set `TRADER_LIVE_AUTHORIZED` and it does not require a Wallex key. `liara.json` already carries the same app, platform, port, Iran build, and disk; the flags repeat them so a deploy from another directory still mounts the disk.

```bash
liara deploy \
  --app wallex-ai-trader \
  --platform docker \
  --port 8080 \
  --build-location iran \
  --disks data:/data
```

`--disks data:/data` is the same mount as `liara.json`: disk name `data`, container path `/data`. If the Iran builder cannot download the Python base image or the wheels, repeat the deploy with `--build-location germany`. The image is the same; the push from Germany is slower.

Shadow with no Wallex credentials uses the image defaults (`TRADER_MODE=shadow`, `TRADER_DATA_DIR=/data`, `TRADER_HEALTH_PORT=8080`). Leave `WALLEX_API_KEY` and `WALLEX_SUBACCOUNT_ID` unset. The first log lines are `WALLEX_API_KEY absent; WALLEX_SUBACCOUNT_ID absent; shadow and dry-run keep serving without orders` and `watchdog off`. `/health` returns `{"ok": true}`. `/` and `/status` stay on the same port.

To run the watchdog beside the trader, add it before deploy:

```bash
liara env set --app wallex-ai-trader --force WATCHDOG_ENABLED=1
```

The entrypoint then logs `watchdog enabled` and `watchdog started`. Alerts still send only when both `TELEGRAM_ALERT_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` are set. Trade decisions use that same token and `TELEGRAM_TRADES_CHAT_ID`. They do not go to the alert group.

## Logs, restart, stop

```bash
liara logs --app wallex-ai-trader --follow
liara restart --app wallex-ai-trader
liara stop --app wallex-ai-trader
liara start --app wallex-ai-trader
```

After deploy, `https://wallex-ai-trader.liara.run/health` returns `{"ok": true}` with no credentials. `/` is one auto-refreshing HTML page. `/status` returns the mode, whether orders are dry-run, equity, open symbols, the last cycle time, `entry_cutoff`, and `max_hold` (the configured `time_stop_hours`). The desk shows the same cutoff and hold. Both require HTTP basic auth. `DASHBOARD_USERS` is a JSON object of username to password, for example `{"ada":"...","ben":"..."}`. Each password is compared with `hmac.compare_digest`. `DASHBOARD_USER` and `DASHBOARD_PASSWORD` remain the single-user fallback, and they still work when the JSON map is also set. If neither is configured, those routes return 401 and `/health` still returns 200. Each successful access logs `dashboard login user=<name>` and the page says `logged in as <name>`. The password is not logged. The page reads only `cycles.jsonl`, `decisions.jsonl`, `adjustments.jsonl`, and `reviews.jsonl` on the disk. It shows key presence, never key values, and it has no route that places or cancels orders, changes config, flips a flag, or starts or stops trading.

A restart keeps `/data` because that path is the disk. The decision log is `decisions.jsonl`. Positions and the kill switch are `state.json`. Both sit on the disk. `state.json` `ops` also stores `go_live_sha`, `config_hash`, and `ev_override`.

## Approved config re-freeze

The live book is already locked. A new image whose `config/default.yaml` hash differs from `ops.config_hash` halts unless `TRADER_APPROVED_CONFIG_HASH` is that new file hash. Set it before the process starts. Do not unset `TRADER_EV_OVERRIDE` or `TRADER_LIVE_AUTHORIZED`.

```bash
sha256sum config/default.yaml
liara env set --app wallex-ai-trader --force \
  TRADER_APPROVED_CONFIG_HASH=6f8fc2d60d8c910d133ee5e1d0b4995141a918c09df4436c953155d313673e28 \
  TRADER_GIT_SHA="<deployed commit>" \
  TRADER_EV_OVERRIDE=1 \
  TRADER_LIVE_AUTHORIZED=1
```

`6f8fc2d60d8c910d133ee5e1d0b4995141a918c09df4436c953155d313673e28` is the SHA-256 of this image's `config/default.yaml` (adds `strategy.name` and optional `take_profit_pct`; default behavior stays `uptrend_pullback`). Recompute it if the file changes. `TRADER_GIT_SHA` is the commit you deploy.

The image has no git metadata. Without `TRADER_GIT_SHA` the lock stores `unknown`. Set that variable to the commit being deployed before the new process starts, and do not wipe `/data/state.json`.

The first cycle logs `approved config re-freeze` and appends `CONFIG_REFREEZE` to `adjustments.jsonl`. `ev_override` stays at the locked value. After that cycle the env can stay set; a later unapproved edit still halts because the env will no longer match the file. A locked book that has no `config_hash` yet adopts the running file without a halt, and leaves `go_live_sha` where it was. Set the env on that boot too, so the locked SHA moves to this commit.

## Shadow, then live

Leave `TRADER_MODE=shadow` until the logs show a universe and a cycle with `dry_run=True`. Shadow never calls `POST /v1/account/orders`.

When the credit sub-account is funded and the key is in place:

```bash
liara env set --app wallex-ai-trader --force TRADER_MODE=live WALLEX_API_KEY="$WALLEX_API_KEY"
liara restart --app wallex-ai-trader
```

To rehearse live order bodies without sending them, set `TRADER_MODE=live` and `TRADER_DRY_RUN=1`.

## Local image check

This does not talk to Liara.

```bash
docker build -t wallex-ai-trader .
docker run --rm -p 8080:8080 \
  -e TRADER_MODE=shadow \
  -e TRADER_DATA_DIR=/data \
  -e TRADER_HEALTH_PORT=8080 \
  wallex-ai-trader
```

Do not pass `WALLEX_API_KEY`. The log contains `WALLEX_API_KEY absent` and `starting shadow dry_run=True`. In another shell, `curl -s http://127.0.0.1:8080/health` is `{"ok": true}`. With no `DASHBOARD_USERS` and no `DASHBOARD_USER` / `DASHBOARD_PASSWORD` pair, `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/` is `401` and `/health` is still `200`. With the pair set, the desk is `curl -s -u "$DASHBOARD_USER:$DASHBOARD_PASSWORD" http://127.0.0.1:8080/`. With `DASHBOARD_USERS='{"ada":"the-password"}'`, the same page is `curl -s -u 'ada:the-password' http://127.0.0.1:8080/`. Stop the container with Ctrl-C.

The same image runs the watchdog beside the trader when the flag is set. It still does not need a Wallex key:

```bash
docker run --rm -p 8080:8080 \
  -e TRADER_MODE=shadow \
  -e TRADER_DATA_DIR=/data \
  -e TRADER_HEALTH_PORT=8080 \
  -e WATCHDOG_ENABLED=1 \
  wallex-ai-trader
```

The log then contains `watchdog started`. `/health` stays up.
