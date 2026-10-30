import os
import json
import time
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# Railway Variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SYMBOLS=BTCUSDT,ETHUSDT
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]

TIMEFRAME_SECONDS = 5 * 60
TIMEFRAME_LABEL = "5m"
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT"

def normalize_symbol(value):
    """Accept BTCUSDT, BTC_USDT or BTC__USDT and normalize to BTC_USDT."""
    s = value.strip().upper().replace("_", "")
    if not s:
        return ""
    if s.endswith("USDT"):
        s = s[:-4]
    return f"{s}_USDT"

SYMBOLS = list(dict.fromkeys(
    normalize_symbol(s)
    for s in os.getenv("SYMBOLS", os.getenv("MEXC_SYMBOLS", DEFAULT_SYMBOLS)).split(",")
    if s.strip()
))
POLL_SECONDS = max(5, int(os.getenv("POLL_SECONDS", "15")))
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
API_URL = os.getenv("MEXC_KLINE_URL", "https://contract.mexc.com/api/v1/contract/kline")
SOURCE_LABEL = "MEXC USDT Perpetual native 5m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("mexc-signal-bot")
SESSION = requests.Session()
SESSION.headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "Mozilla/5.0 (compatible; BTC-ETH-5m-Signal-Bot/1.0)"})


def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M")


def symbol_label(symbol):
    return symbol.replace("_", "")


def candle_color(candle):
    if candle["close"] > candle["open"]:
        return "GREEN"
    if candle["close"] < candle["open"]:
        return "RED"
    return "DOJI"


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object")
        state.setdefault("pending", [])
        state.setdefault("last_processed", None)
        return state
    except FileNotFoundError:
        return {"last_processed": None, "pending": []}
    except Exception as exc:
        log.warning("Не вдалося прочитати стан %s (%s). Починаємо з чистого стану", path, exc)
        return {"last_processed": None, "pending": []}


def save_state(state, path):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def send_telegram(text):
    if not TOKEN or not CHAT_IDS:
        log.error("Telegram не налаштовано: додайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у змінні Railway")
        return False
    success_count = 0
    for chat_id in CHAT_IDS:
        try:
            response = SESSION.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            payload = response.json()
            if response.ok and payload.get("ok"):
                success_count += 1
                log.info("TELEGRAM SENT OK | chat=%s", chat_id)
            else:
                log.error("TELEGRAM ERROR | chat=%s | status=%s | body=%s", chat_id, response.status_code, response.text[:300])
        except Exception:
            log.exception("TELEGRAM EXCEPTION | chat=%s", chat_id)
    return success_count == len(CHAT_IDS)


def fetch_5m_candles(symbol):
    """Fetch native closed 5-minute MEXC USDT perpetual candles."""
    market_symbol = symbol.replace("_", "").upper()
    # MEXC Futures symbols use the underscore form, e.g. BTC_USDT.
    url = f"{API_URL.rstrip('/')}/{symbol}"
    now_s = int(time.time())
    response = SESSION.get(
        url,
        params={"interval": "Min5", "limit": 500, "end": now_s},
        timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"MEXC HTTP {response.status_code} на {response.url}: {response.text[:500]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"MEXC повернув не-JSON відповідь HTTP {response.status_code}: {response.text[:300]}"
        ) from exc
    if not payload.get("success"):
        raise RuntimeError(
            f"MEXC API error: code={payload.get('code')}; message={payload.get('msg') or payload.get('message')}"
        )

    data = payload.get("data") or {}
    required = ("time", "open", "high", "low", "close")
    if not isinstance(data, dict) or any(key not in data for key in required):
        raise RuntimeError(f"MEXC: неочікуваний формат свічок для {market_symbol}: {str(data)[:300]}")

    times = data.get("time", [])
    opens = data.get("open", [])
    highs = data.get("high", [])
    lows = data.get("low", [])
    closes = data.get("close", [])
    volumes = data.get("vol", [0] * len(times))
    count = min(len(times), len(opens), len(highs), len(lows), len(closes), len(volumes))
    candles = []
    live = None
    for i in range(count):
        ts = int(times[i])
        candle = {
            "ts": ts,
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": float(volumes[i]),
        }
        # MEXC may include the currently-forming candle as the last row.
        if ts + TIMEFRAME_SECONDS <= now_s:
            candles.append(candle)
        elif ts <= now_s < ts + TIMEFRAME_SECONDS:
            live = {"ts": ts, "open": candle["open"], "close": candle["close"]}

    candles.sort(key=lambda c: c["ts"])
    if not candles:
        raise RuntimeError(f"MEXC не повернув закритих 5m свічок для {market_symbol}")
    return candles, live


class SignalEngine:
    """Two strategy rules on native 5m candles, with strict color checks."""
    def __init__(self, symbol):
        self.symbol = symbol
        self.state_path = os.getenv(f"STATE_FILE_{symbol}", f"state_{symbol}.json")
        self.state = load_state(self.state_path)
        self.candles = OrderedDict()
        self.pending = [
            p for p in self.state.get("pending", [])
            if isinstance(p, dict) and p.get("rule") in (1, 2) and "signal_ts" in p
        ] if isinstance(self.state.get("pending", []), list) else []
        self.rule1_waiting = [
            p for p in self.state.get("rule1_waiting", [])
            if isinstance(p, dict) and "start_ts" in p and "warning_ts" in p
        ] if isinstance(self.state.get("rule1_waiting", []), list) else []
        self.pre_sent = set()
        self.initialized = False

    def seed(self, closed):
        self.candles = OrderedDict((c["ts"], c) for c in closed[-250:])
        self.candles = OrderedDict(sorted(self.candles.items()))
        # Do not create historical alerts on startup. Keep saved pending setups.
        self.state["last_processed"] = next(reversed(self.candles), None) if self.candles else None
        self.state["pending"] = self.pending
        self.state["rule1_waiting"] = self.rule1_waiting
        save_state(self.state, self.state_path)
        self.initialized = True
        if self.candles:
            latest = next(reversed(self.candles.values()))
            log.info("INITIALIZED | %s | candles=%d | latest=%s %s | pending=%d", self.symbol, len(self.candles), utc(latest["ts"]), candle_color(latest), len(self.pending))

    def _color_at(self, ts):
        candle = self.candles.get(ts)
        return candle_color(candle) if candle else None

    def _pattern_start(self, start_ts):
        """Return GREEN/RED when #1-#3 are consecutive same-color candles and #4 is opposite.

        Candles before #1 may have any color, including the same color as #1-#3.
        """
        c1 = self.candles.get(start_ts)
        c2 = self.candles.get(start_ts + TIMEFRAME_SECONDS)
        c3 = self.candles.get(start_ts + 2 * TIMEFRAME_SECONDS)
        c4 = self.candles.get(start_ts + 3 * TIMEFRAME_SECONDS)
        if not all((c1, c2, c3, c4)):
            return None
        base = candle_color(c1)
        if base not in ("GREEN", "RED"):
            return None
        if any(candle_color(c) != base for c in (c2, c3)):
            return None
        opposite = "RED" if base == "GREEN" else "GREEN"
        if candle_color(c4) != opposite:
            return None
        return base

    def _direction(self, base):
        return "LONG" if base == "GREEN" else "SHORT"

    def _message(self, kind, direction, rule, start_ts, extra=""):
        heading = {"warning": "⚠️ ПОПЕРЕДЖЕННЯ", "signal": f"🔔 СИГНАЛ {direction}", "win": "✅ WIN", "loss": "❌ LOSS"}[kind]
        parts = [heading, f"{symbol_label(self.symbol)}, ф’ючерси", f"Інтервал: {TIMEFRAME_LABEL}", f"Правило: {rule}", f"Початок патерну: {kyiv(start_ts)} за київським часом"]
        if extra:
            parts.append(extra)
        if kind == "signal":
            parts.append("Це лише сигнал. Автоматичні угоди не виконуються.")
        return "\n".join(parts)

    def maybe_pre_alert(self, live_candle):
        """Rule 2: alert at the start of live candle #7 if its current color matches."""
        if not self.initialized or not live_candle:
            return
        live_ts = live_candle["ts"]
        if live_ts in self.pre_sent or live_ts in self.candles:
            return
        start_ts = live_ts - 6 * TIMEFRAME_SECONDS
        base = self._pattern_start(start_ts)
        if base is None:
            return
        # #5 and #6 are unconstrained; live candle is #7.
        live_color = candle_color(live_candle)
        expected = "RED" if base == "GREEN" else "GREEN"
        if live_color != expected:
            return
        direction = self._direction(base)
        text = self._message("warning", direction, 2, start_ts,
            f"Свічка №7 зараз {live_color}. Якщо вона збереже колір до закриття, буде надіслано сигнал.")
        log.info("ПОПЕРЕДЖЕННЯ ПРАВИЛО 2 | %s | %s | старт=%s", self.symbol, direction, utc(start_ts))
        if send_telegram(text):
            self.pre_sent.add(live_ts)

    def ingest(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        latest_ts = next(reversed(self.candles), None) if self.candles else None
        if latest_ts is None:
            self.seed(closed)
            return 0
        new = sorted((c for c in closed if c["ts"] > latest_ts), key=lambda x: x["ts"])
        if not new:
            return 0
        for candle in new:
            self.candles[candle["ts"]] = candle
            self.candles = OrderedDict(sorted(self.candles.items()))
            # Evaluate before trimming so the candle being processed is always present.
            self.evaluate(candle["ts"])
            while len(self.candles) > 250:
                self.candles.popitem(last=False)
        self.state["pending"] = self.pending
        self.state["rule1_waiting"] = self.rule1_waiting
        self.state["last_processed"] = new[-1]["ts"]
        save_state(self.state, self.state_path)
        return len(new)

    def evaluate(self, ts):
        current_color = self._color_at(ts)

        # First resolve active result windows. Rule 1: #10-#15 (six candles).
        # Rule 2: #8-#14 (seven candles). Doji does not count as either color.
        keep = []
        for setup in self.pending:
            rel = int((ts - setup["signal_ts"]) // TIMEFRAME_SECONDS)
            result_number = setup["result_start_number"] + rel - 1
            if rel < 1:
                keep.append(setup)
                continue
            wanted = "GREEN" if setup["direction"] == "LONG" else "RED"
            opposite = "RED" if setup["direction"] == "LONG" else "GREEN"
            max_number = setup["result_end_number"]
            if result_number > max_number:
                continue
            if current_color == wanted:
                send_telegram(self._message("win", setup["direction"], setup["rule"], setup["start_ts"], f"Результатна свічка: №{result_number}") )
                log.info("WIN | %s | правило=%s | %s | свічка №%d", self.symbol, setup["rule"], setup["direction"], result_number)
                continue
            if result_number == max_number:
                # LOSS only if every candle in the window was opposite; doji prevents strict LOSS.
                window_start = setup["signal_ts"] + TIMEFRAME_SECONDS
                window = [self._color_at(window_start + i * TIMEFRAME_SECONDS) for i in range(max_number - setup["result_start_number"] + 1)]
                expected_count = max_number - setup["result_start_number"] + 1
                if len(window) == expected_count and all(c == opposite for c in window):
                    send_telegram(self._message("loss", setup["direction"], setup["rule"], setup["start_ts"], f"Усі свічки №{setup['result_start_number']}–№{max_number} — {opposite}.") )
                    log.info("LOSS | %s | правило=%s | %s", self.symbol, setup["rule"], setup["direction"])
                else:
                    log.info("NO WIN/LOSS STRICT RULE | %s | правило=%s | у вікні є DOJI або неповні дані", self.symbol, setup["rule"])
                continue
            keep.append(setup)
        self.pending = keep

        # Rule 1: candle #8 closes -> warning; candle #9 closes opposite -> signal.
        waiting_keep = []
        for item in self.rule1_waiting:
            expected_ts = item["warning_ts"] + TIMEFRAME_SECONDS
            if ts != expected_ts:
                if ts < expected_ts:
                    waiting_keep.append(item)
                continue
            base = item["base_color"]
            trigger = "RED" if base == "GREEN" else "GREEN"
            if current_color == trigger:
                direction = self._direction(base)
                signal_text = self._message("signal", direction, 1, item["start_ts"], "Сигнал після закриття свічки №9.")
                if send_telegram(signal_text):
                    self.pending.append({"rule": 1, "start_ts": item["start_ts"], "signal_ts": ts, "direction": direction, "result_start_number": 10, "result_end_number": 15})
                    log.info("СИГНАЛ | правило=1 | %s | %s | старт=%s", self.symbol, direction, utc(item["start_ts"]))
            else:
                log.info("RULE 1 CANCEL | %s | candle #9 color=%s; expected=%s", self.symbol, current_color, trigger)
        self.rule1_waiting = waiting_keep

        # Rule 2: candle #7 closes with the required opposite color -> signal immediately.
        start_ts = ts - 6 * TIMEFRAME_SECONDS
        base = self._pattern_start(start_ts)
        if base is not None:
            expected7 = "RED" if base == "GREEN" else "GREEN"
            if current_color == expected7:
                direction = self._direction(base)
                # avoid duplicate setups for the same rule/start candle
                if not any(p.get("rule") == 2 and p.get("start_ts") == start_ts for p in self.pending):
                    if send_telegram(self._message("signal", direction, 2, start_ts, "Сигнал після закриття свічки №7.")):
                        self.pending.append({"rule": 2, "start_ts": start_ts, "signal_ts": ts, "direction": direction, "result_start_number": 8, "result_end_number": 14})
                        log.info("СИГНАЛ | правило=2 | %s | %s | старт=%s", self.symbol, direction, utc(start_ts))
            # Rule 1 warning is issued only after #8 closes; #7 must match base color.
            expected7_rule1 = base
            if current_color == expected7_rule1:
                start8_ts = ts + TIMEFRAME_SECONDS
                # #8 has no color restriction; the warning is sent when #8 is processed below.

        # Rule 1 warning on candle #8 close: start = current - 7 intervals.
        start8_ts = ts - 7 * TIMEFRAME_SECONDS
        base8 = self._pattern_start(start8_ts)
        if base8 is not None:
            c7_color = self._color_at(start8_ts + 6 * TIMEFRAME_SECONDS)
            # #7 must match the start color, #8 can be any color (but must be non-DOJI? no restriction).
            if c7_color == base8 and not any(p.get("start_ts") == start8_ts for p in self.rule1_waiting):
                direction = self._direction(base8)
                if send_telegram(self._message("warning", direction, 1, start8_ts, "Свічка №8 закрилася. Якщо свічка №9 закриється потрібним кольором, буде надіслано сигнал.")):
                    self.rule1_waiting.append({"start_ts": start8_ts, "base_color": base8, "warning_ts": ts})
                    log.info("ПОПЕРЕДЖЕННЯ ПРАВИЛО 1 | %s | %s | старт=%s", self.symbol, direction, utc(start8_ts))


def main():
    if not SYMBOLS:
        raise RuntimeError("SYMBOLS порожній; задайте BTCUSDT,ETHUSDT")
    log.info("Запуск бота сигналів MEXC USDT Perpetual (native 5m) | інтервал=%s | перевірка кожні %s с", TIMEFRAME_LABEL, POLL_SECONDS)
    log.info("Монети: %s", ", ".join(SYMBOLS))
    log.info("Правило 1: #1-#3 однакові, #4 протилежна, #5-#6 будь-які, #7 як #1, #8 будь-яка, #9 протилежна; результат #10-#15")
    log.info("Правило 2: #1-#3 однакові, #4 протилежна, #5-#6 будь-які, #7 протилежна; результат #8-#14")
    log.info("Telegram налаштовано: %s | кількість чатів: %d", bool(TOKEN), len(CHAT_IDS))
    if TOKEN and CHAT_IDS:
        send_telegram(f"🤖 БОТ ЗАПУЩЕНО\nMEXC USDT Perpetual (native 5m)\nМонети: {', '.join(symbol_label(s) for s in SYMBOLS)}\nІнтервал: {TIMEFRAME_LABEL}\nНадсилаються лише сигнали; автоматичні угоди не виконуються.")

    engines = {symbol: SignalEngine(symbol) for symbol in SYMBOLS}
    last_heartbeat = {}
    while True:
        for symbol, engine in engines.items():
            try:
                closed, live = fetch_5m_candles(symbol)
                if live:
                    engine.maybe_pre_alert(live)
                if not closed:
                    log.warning("%s | MEXC поки не повернув закритих свічок %s", symbol, TIMEFRAME_LABEL)
                    continue
                added = engine.ingest(closed)
                latest = closed[-1]
                if added:
                    log.info("НОВІ ДАНІ | %s | отриманих 5m свічок=%d | закритих=%d | додано=%d | остання=%s %s | відкриття=%s закриття=%s | очікують=%d",
                             symbol, len(closed), len(closed), added, utc(latest["ts"]), candle_color(latest), latest["open"], latest["close"], len(engine.pending))
                elif time.time() - last_heartbeat.get(symbol, 0) >= 60:
                    age = max(0, int(time.time() - (latest["ts"] + TIMEFRAME_SECONDS)))
                    log.info("ЗВ’ЯЗОК Є | %s | остання=%s %s | затримка=%s с | очікують=%d", symbol, utc(latest["ts"]), candle_color(latest), age, len(engine.pending))
                    last_heartbeat[symbol] = time.time()
            except Exception as exc:
                log.exception("ПОМИЛКА ЦИКЛУ | %s | %s", symbol, exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if TEST_MODE:
        # Simple startup/config validation; no real Telegram messages are sent.
        print("ТЕСТОВИЙ РЕЖИМ: ПЕРЕВІРКУ ПРОЙДЕНО")
        print("Монети:", ", ".join(SYMBOLS))
        print("Інтервал:", TIMEFRAME_LABEL, "(нативні 5m свічки)")
        print("Джерело даних: MEXC USDT Perpetual native 5m, API URL:", API_URL)
        print("Правило 1: сигнал після №9, результат №10–№15")
        print("Правило 2: сигнал після №7, результат №8–№14")
    else:
        main()
