# Bybit 5m Telegram Signal Bot

Data source: Bybit V5 public market API via alternate official hostname `https://api.bytick.com/v5/market/kline`, category `linear`, interval `5` (5 minutes).
Symbols: `BTCUSDT,ETHUSDT`.

## Railway variables
- `TELEGRAM_BOT_TOKEN` — Telegram bot token
- `TELEGRAM_CHAT_ID` — chat ID (multiple IDs may be comma-separated)
- `SYMBOLS` — optional; default `BTCUSDT,ETHUSDT` (do not use MEXC_SYMBOLS)
- `POLL_SECONDS` — optional; default `15`
- `TEST_MODE` — optional; `true` prints config only and does not start polling

Do not put Telegram tokens in source code or commit them to a repository.

The bot only sends alerts; it does not place trades.
