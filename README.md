# BTC/ETH 5m Signal Bot (Binance 1m aggregation)

Бот отримує публічні 1-хвилинні свічки Binance USD-M Futures та об’єднує кожні п’ять у завершену 5-хвилинну свічку. Логіка двох правил сигналів збережена. Автоматичні угоди не виконуються.

## Railway Variables
- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота
- `TELEGRAM_CHAT_ID` — ID чату або кілька ID через кому
- `SYMBOLS=BTCUSDT,ETHUSDT`
- `POLL_SECONDS=15`
- `TEST_MODE=false`
- `BINANCE_KLINE_URL` — необов’язково; за замовчуванням `https://fapi.binance.com/fapi/v1/klines`

API ключ Binance не потрібен: використовується публічний endpoint. Доступність endpoint залежить від регіональних обмежень і мережі Railway.

## Формування 5m
Open — перша 1m; High — максимум п’яти; Low — мінімум п’яти; Close — остання 1m; Volume — сума п’яти. Незавершені 5m свічки не передаються в історію правил. Для попередження Rule 2 поточна часткова 5m свічка формується з уже закритих 1m свічок.
