# Bybit 1m → 5m signal bot

The bot fetches 1-minute candles from the Bybit V5 public linear market endpoint and aggregates each complete group of five 1m candles into one 5m candle.

Railway variables:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- SYMBOLS=BTCUSDT,ETHUSDT
- POLL_SECONDS=15
- TEST_MODE=false
- Optional: BYBIT_KLINE_URL=https://api.bybit.com/v5/market/kline

Important: changing the interval from 5m to 1m does NOT bypass a country/IP restriction. If Railway's outbound IP is blocked by Bybit/CloudFront, the request will still return HTTP 403 and the bot cannot receive candles from that runtime. The code remains Bybit-only; it does not use Binance.
