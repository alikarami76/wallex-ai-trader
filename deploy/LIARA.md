# Deploy on Liara

The competition process runs as a Docker app on [Liara](https://liara.ir). The image starts the trading loop and an HTTP server on port 8080. Liara's Docker platform publishes one HTTP port and curls it (`/health`) before it treats a release as healthy, so the loop alone is not enough. The same port serves a read-only desk at `/`.

Shadow is the default. The container reads public Wallex data and does not send orders. Nothing in this repository is a secret. Set keys with `liara env set`.

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
| `DASHBOARD_USER` | Basic-auth user for `/` and `/status`. If unset, those routes stay closed. |
| `DASHBOARD_PASSWORD` | Basic-auth password. Set it with the keys above, not in the image. |
| `TELEGRAM_ALERT_BOT_TOKEN` | Optional. Alerts stay off unless this and the chat id are both set. |
| `TELEGRAM_ALERT_CHAT_ID` | Optional chat for a cycle gap, HALT or KILLED, an order reject, or a taker entry fill. |
| `WATCHDOG_ENABLED` | Optional. The journal sidecar stays off unless this is `1`. It does not place orders. |

`LLM_MODEL` is ignored. It does not pin every tier.

## Deploy, logs, restart, stop

`liara.json` already carries the port, the Iran build, and the disk. This command repeats them so a deploy from another directory still mounts the disk:

```bash
liara deploy \
  --app wallex-ai-trader \
  --platform docker \
  --port 8080 \
  --build-location iran \
  --disks data:/data
```

`--disks data:/data` is the same mount as `liara.json`: disk name `data`, container path `/data`.

If the Iran builder cannot download the Python base image or the wheels, repeat the deploy with `--build-location germany`. The image is the same; the push from Germany is slower.

```bash
liara logs --app wallex-ai-trader --follow
liara restart --app wallex-ai-trader
liara stop --app wallex-ai-trader
liara start --app wallex-ai-trader
```

After deploy, `https://wallex-ai-trader.liara.run/health` returns `{"ok": true}` with no credentials. `/` is one auto-refreshing HTML page. `/status` returns the mode, whether orders are dry-run, equity, open symbols, and the last cycle time. Both require HTTP basic auth from `DASHBOARD_USER` and `DASHBOARD_PASSWORD`. If either variable is missing, those routes return 401 and `/health` still returns 200. The page reads only `cycles.jsonl`, `decisions.jsonl`, `adjustments.jsonl`, and `reviews.jsonl` on the disk. It shows key presence, never key values, and it has no route that places or cancels orders, changes config, flips a flag, or starts or stops trading.

A restart keeps `/data` because that path is the disk. The decision log is `decisions.jsonl`. Positions and the kill switch are `state.json`. Both sit on the disk.

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

In another shell, `curl -s http://127.0.0.1:8080/health` is `{"ok": true}`. The desk is `curl -s -u "$DASHBOARD_USER:$DASHBOARD_PASSWORD" http://127.0.0.1:8080/`. The log line `starting shadow dry_run=True` means the loop is in shadow. Stop the container with Ctrl-C. No `WALLEX_API_KEY` is required for that check. `/health` does not need the dashboard password.
