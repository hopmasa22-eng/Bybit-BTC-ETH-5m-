# MEXC 5m signal bot

Бот отримує нативні 5-хвилинні свічки MEXC Futures через публічний endpoint `/api/v1/contract/kline/{symbol}` з `interval=Min5`. Свічки не агрегуються з 1m.

Railway variables:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SYMBOLS=BTCUSDT,ETHUSDT` (або `BTC_USDT,ETH_USDT`)
- `POLL_SECONDS=15`
- `TEST_MODE=false`
- Optional: `MEXC_KLINE_URL=https://contract.mexc.com/api/v1/contract/kline`

Сигнали інформаційні; бот не виконує автоматичних угод.
