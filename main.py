# -*- coding: utf-8 -*-
import os, json, time, logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]
TIMEFRAME_SECONDS = 300
TIMEFRAME_LABEL = "5m"
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT"

def normalize_symbol(value):
    s = value.strip().upper().replace("_", "")
    if not s: return ""
    if s.endswith("USDT"): s = s[:-4]
    return f"{s}_USDT"

SYMBOLS = list(dict.fromkeys(normalize_symbol(s) for s in os.getenv("SYMBOLS", os.getenv("MEXC_SYMBOLS", DEFAULT_SYMBOLS)).split(",") if s.strip()))
POLL_SECONDS = max(1, int(os.getenv("POLL_SECONDS", "1")))
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
API_URL = os.getenv("MEXC_KLINE_URL", "https://contract.mexc.com/api/v1/contract/kline")

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
    if candle["close"] > candle["open"]: return "GREEN"
    if candle["close"] < candle["open"]: return "RED"
    return "DOJI"

def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict): raise ValueError("State must be a JSON object")
        state.setdefault("pending", [])
        state.setdefault("last_processed", None)
        state.setdefault("rule1_waiting", [])
        return state
    except FileNotFoundError:
        return {"last_processed": None, "pending": [], "rule1_waiting": []}
    except Exception as exc:
        log.warning("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ð¿ÑÐ¾ÑÐ¸ÑÐ°ÑÐ¸ ÑÑÐ°Ð½ %s (%s). ÐÐ¾ÑÐ¸Ð½Ð°ÑÐ¼Ð¾ Ð· ÑÐ¸ÑÑÐ¾Ð³Ð¾ ÑÑÐ°Ð½Ñ.", path, exc)
        return {"last_processed": None, "pending": [], "rule1_waiting": []}

def save_state(state, path):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)

def send_telegram(text):
    if not TOKEN or not CHAT_IDS:
        log.error("Telegram Ð½Ðµ Ð½Ð°Ð»Ð°ÑÑÐ¾Ð²Ð°Ð½Ð¾: Ð´Ð¾Ð´Ð°Ð¹ÑÐµ TELEGRAM_BOT_TOKEN Ñ TELEGRAM_CHAT_ID Ñ Railway.")
        return False
    success_count = 0
    for chat_id in CHAT_IDS:
        try:
            started = time.perf_counter()
            response = SESSION.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                    json={"chat_id": chat_id, "text": text}, timeout=15)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            payload = response.json()
            if response.ok and payload.get("ok"):
                success_count += 1
                log.info("TELEGRAM SENT OK | chat=%s | api_response_ms=%s", chat_id, elapsed)
            else:
                log.error("TELEGRAM ERROR | chat=%s | status=%s | body=%s", chat_id, response.status_code, response.text[:300])
        except Exception:
            log.exception("TELEGRAM EXCEPTION | chat=%s", chat_id)
    return success_count == len(CHAT_IDS)

def fetch_5m_candles(symbol):
    market_symbol = symbol.replace("_", "").upper()
    url = f"{API_URL.rstrip('/')}/{symbol}"
    now_s = int(time.time())
    response = SESSION.get(url, params={"interval": "Min5", "limit": 500, "end": now_s}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"MEXC HTTP {response.status_code} Ð½Ð° {response.url}: {response.text[:500]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"MEXC Ð¿Ð¾Ð²ÐµÑÐ½ÑÐ² Ð½Ðµ-JSON Ð²ÑÐ´Ð¿Ð¾Ð²ÑÐ´Ñ HTTP {response.status_code}: {response.text[:300]}") from exc
    if not payload.get("success"):
        raise RuntimeError(f"MEXC API error: code={payload.get('code')}; message={payload.get('msg') or payload.get('message')}")
    data = payload.get("data") or {}
    required = ("time", "open", "high", "low", "close")
    if not isinstance(data, dict) or any(k not in data for k in required):
        raise RuntimeError(f"MEXC: Ð½ÐµÐ¾ÑÑÐºÑÐ²Ð°Ð½Ð¸Ð¹ ÑÐ¾ÑÐ¼Ð°Ñ ÑÐ²ÑÑÐ¾Ðº Ð´Ð»Ñ {market_symbol}: {str(data)[:300]}")
    times, opens, highs, lows, closes = (data.get(k, []) for k in ("time", "open", "high", "low", "close"))
    volumes = data.get("vol", [0] * len(times))
    count = min(len(times), len(opens), len(highs), len(lows), len(closes), len(volumes))
    candles, live = [], None
    for i in range(count):
        ts = int(times[i])
        c = {"ts": ts, "open": float(opens[i]), "high": float(highs[i]), "low": float(lows[i]),
             "close": float(closes[i]), "volume": float(volumes[i])}
        if ts + TIMEFRAME_SECONDS <= now_s:
            candles.append(c)
        elif ts <= now_s < ts + TIMEFRAME_SECONDS:
            live = {"ts": ts, "open": c["open"], "close": c["close"]}
    candles.sort(key=lambda c: c["ts"])
    if not candles: raise RuntimeError(f"MEXC Ð½Ðµ Ð¿Ð¾Ð²ÐµÑÐ½ÑÐ² Ð·Ð°ÐºÑÐ¸ÑÐ¸Ñ 5m ÑÐ²ÑÑÐ¾Ðº Ð´Ð»Ñ {market_symbol}")
    return candles, live

class SignalEngine:
    def __init__(self, symbol):
        self.symbol = symbol
        self.state_path = os.getenv(f"STATE_FILE_{symbol}", f"state_{symbol}.json")
        self.state = load_state(self.state_path)
        self.candles = OrderedDict()
        self.pending = [p for p in self.state.get("pending", [])
                        if isinstance(p, dict) and p.get("rule") in (1, 2) and "signal_ts" in p] \
                       if isinstance(self.state.get("pending", []), list) else []
        self.rule1_waiting = [p for p in self.state.get("rule1_waiting", [])
                              if isinstance(p, dict) and "start_ts" in p and "warning_ts" in p] \
                             if isinstance(self.state.get("rule1_waiting", []), list) else []
        self.pre_sent = set()
        self.rule2_signal_sent = set()
        self.rule1_signal_sent = set()
        self.initialized = False

    def seed(self, closed):
        self.candles = OrderedDict((c["ts"], c) for c in closed[-250:])
        self.candles = OrderedDict(sorted(self.candles.items()))
        self.state["last_processed"] = next(reversed(self.candles), None) if self.candles else None
        self.state["pending"], self.state["rule1_waiting"] = self.pending, self.rule1_waiting
        save_state(self.state, self.state_path)
        self.initialized = True
        if self.candles:
            latest = next(reversed(self.candles.values()))
            log.info("INITIALIZED | %s | candles=%d | latest=%s %s | pending=%d",
                     self.symbol, len(self.candles), utc(latest["ts"]), candle_color(latest), len(self.pending))

    def _color_at(self, ts):
        c = self.candles.get(ts)
        return candle_color(c) if c else None

    def _pattern_start(self, start_ts):
        cs = [self.candles.get(start_ts + i * TIMEFRAME_SECONDS) for i in range(4)]
        if not all(cs): return None
        base = candle_color(cs[0])
        if base not in ("GREEN", "RED"): return None
        if candle_color(cs[1]) != base or candle_color(cs[2]) != base: return None
        opposite = "RED" if base == "GREEN" else "GREEN"
        return base if candle_color(cs[3]) == opposite else None

    def _direction(self, base):
        return "LONG" if base == "GREEN" else "SHORT"

    def _message(self, kind, direction, rule, start_ts, extra=""):
        heading = {"warning": "â ï¸ ÐÐÐÐÐ ÐÐÐÐÐÐÐ¯", "signal": f"ð Ð¡ÐÐÐÐÐ {direction}",
                   "win": "â WIN", "loss": "â LOSS"}[kind]
        parts = [heading, f"{symbol_label(self.symbol)}, Ñ'ÑÑÐµÑÑÐ¸", f"ÐÐ½ÑÐµÑÐ²Ð°Ð»: {TIMEFRAME_LABEL}",
                 f"ÐÑÐ°Ð²Ð¸Ð»Ð¾: {rule}", f"ÐÐ¾ÑÐ°ÑÐ¾Ðº Ð¿Ð°ÑÐµÑÐ½Ñ: {kyiv(start_ts)} Ð·Ð° ÐºÐ¸ÑÐ²ÑÑÐºÐ¸Ð¼ ÑÐ°ÑÐ¾Ð¼"]
        if extra: parts.append(extra)
        if kind == "signal": parts.append("Ð¦Ðµ Ð»Ð¸ÑÐµ ÑÐ¸Ð³Ð½Ð°Ð». ÐÐ²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐ½Ñ ÑÐ³Ð¾Ð´Ð¸ Ð½Ðµ Ð²Ð¸ÐºÐ¾Ð½ÑÑÑÑÑÑ.")
        return "\n".join(parts)

    def maybe_pre_alert(self, live_candle):
        # Rule 2 PRE at the start of live candle #8.
        if not self.initialized or not live_candle: return
        live_ts = live_candle["ts"]
        if live_ts in self.pre_sent or live_ts in self.candles: return
        start_ts = live_ts - 7 * TIMEFRAME_SECONDS
        base = self._pattern_start(start_ts)
        if base is None: return
        expected = "RED" if base == "GREEN" else "GREEN"
        if self._color_at(start_ts + 6 * TIMEFRAME_SECONDS) != expected: return  # closed #7
        if candle_color(live_candle) != expected: return
        direction = self._direction(base)
        msg = self._message("warning", direction, 2, start_ts,
            f"Ð¡Ð²ÑÑÐºÐ° â8 ÑÐ¾ÑÐ¼ÑÑÑÑÑÑ ÑÐ° Ð·Ð°ÑÐ°Ð· {expected}. Ð¡Ð¸Ð³Ð½Ð°Ð» Ð¾ÑÑÐºÑÑÑÑÑÑ Ð·Ð° 5 ÑÐµÐºÑÐ½Ð´ Ð´Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ, ÑÐºÑÐ¾ ÐºÐ¾Ð»ÑÑ Ð·Ð±ÐµÑÐµÐ¶ÐµÑÑÑÑ.")
        log.info("PRE RULE 2 | %s | %s | start=%s", self.symbol, direction, utc(start_ts))
        if send_telegram(msg): self.pre_sent.add(live_ts)

    def maybe_rule2_signal(self, live_candle):
        # Rule 2 SIGNAL at T-5 seconds of candle #8, only if #8 is still the required color.
        if not self.initialized or not live_candle: return
        live_ts = live_candle["ts"]
        if live_ts in self.rule2_signal_sent: return
        start_ts = live_ts - 7 * TIMEFRAME_SECONDS
        base = self._pattern_start(start_ts)
        if base is None: return
        expected = "RED" if base == "GREEN" else "GREEN"
        if self._color_at(start_ts + 6 * TIMEFRAME_SECONDS) != expected: return
        if candle_color(live_candle) != expected: return
        remaining = live_ts + TIMEFRAME_SECONDS - time.time()
        if not 0 <= remaining <= 5: return
        if any(p.get("rule") == 2 and p.get("start_ts") == start_ts for p in self.pending):
            self.rule2_signal_sent.add(live_ts)
            return
        direction = self._direction(base)
        msg = self._message("signal", direction, 2, start_ts,
            "Ð¡Ð¸Ð³Ð½Ð°Ð» Ð·Ð° 5 ÑÐµÐºÑÐ½Ð´ Ð´Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ ÑÐ²ÑÑÐºÐ¸ â8. ÐÐ¾Ð»ÑÑ Ð¿ÐµÑÐµÐ²ÑÑÐµÐ½Ð¾ Ð² Ð¼Ð¾Ð¼ÐµÐ½Ñ Ð½Ð°Ð´ÑÐ¸Ð»Ð°Ð½Ð½Ñ.")
        if send_telegram(msg):
            self.pending.append({"rule": 2, "start_ts": start_ts, "signal_ts": live_ts,
                                 "direction": direction, "result_start_number": 9, "result_end_number": 14})
            self.rule2_signal_sent.add(live_ts)
            self.state["pending"] = self.pending
            save_state(self.state, self.state_path)
            log.info("SIGNAL RULE 2 | %s | %s | start=%s | seconds_left=%.2f",
                     self.symbol, direction, utc(start_ts), remaining)

    def maybe_rule1_signal(self, live_candle):
        """Rule 1 SIGNAL: T-5 seconds of live candle #9, if its color matches."""
        if not self.initialized or not live_candle:
            return
        live_ts = live_candle["ts"]
        if live_ts in self.rule1_signal_sent:
            return

        # A Rule 1 waiting item is created when candle #8 closes.
        matching = next(
            (item for item in self.rule1_waiting
             if item["warning_ts"] + TIMEFRAME_SECONDS == live_ts),
            None
        )
        if matching is None:
            return

        base = matching["base_color"]
        trigger = "RED" if base == "GREEN" else "GREEN"
        if candle_color(live_candle) != trigger:
            return

        seconds_left = live_ts + TIMEFRAME_SECONDS - time.time()
        if not 0 <= seconds_left <= 5:
            return

        # Avoid duplicate signals if state already contains this setup.
        if any(p.get("rule") == 1 and p.get("start_ts") == matching["start_ts"]
               for p in self.pending):
            self.rule1_signal_sent.add(live_ts)
            return

        direction = self._direction(base)
        text = self._message(
            "signal", direction, 1, matching["start_ts"],
            "Ð¡Ð¸Ð³Ð½Ð°Ð» Ð·Ð° 5 ÑÐµÐºÑÐ½Ð´ Ð´Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ ÑÐ²ÑÑÐºÐ¸ â9. ÐÐ¾Ð»ÑÑ Ð¿ÐµÑÐµÐ²ÑÑÐµÐ½Ð¾ Ð² Ð¼Ð¾Ð¼ÐµÐ½Ñ Ð½Ð°Ð´ÑÐ¸Ð»Ð°Ð½Ð½Ñ."
        )
        if send_telegram(text):
            self.pending.append({
                "rule": 1,
                "start_ts": matching["start_ts"],
                "signal_ts": live_ts,
                "direction": direction,
                "result_start_number": 10,
                "result_end_number": 15,
            })
            self.rule1_signal_sent.add(live_ts)
            self.state["pending"] = self.pending
            save_state(self.state, self.state_path)
            log.info("SIGNAL RULE 1 | %s | %s | start=%s | seconds_left=%.2f",
                     self.symbol, direction, utc(matching["start_ts"]), seconds_left)

    def ingest(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        latest_ts = next(reversed(self.candles), None) if self.candles else None
        if latest_ts is None:
            self.seed(closed)
            return 0
        new = sorted((c for c in closed if c["ts"] > latest_ts), key=lambda x: x["ts"])
        if not new: return 0
        for candle in new:
            self.candles[candle["ts"]] = candle
            self.candles = OrderedDict(sorted(self.candles.items()))
            self.evaluate(candle["ts"])
            while len(self.candles) > 250: self.candles.popitem(last=False)
        self.state["pending"], self.state["rule1_waiting"] = self.pending, self.rule1_waiting
        self.state["last_processed"] = new[-1]["ts"]
        save_state(self.state, self.state_path)
        return len(new)

    def evaluate(self, ts):
        current_color = self._color_at(ts)
        # Result handling: Rule 1 #10-#15; Rule 2 #9-#14.
        keep = []
        for setup in self.pending:
            rel = int((ts - setup["signal_ts"]) // TIMEFRAME_SECONDS)
            number = setup["result_start_number"] + rel - 1
            if rel < 1:
                keep.append(setup)
                continue
            wanted = "GREEN" if setup["direction"] == "LONG" else "RED"
            opposite = "RED" if setup["direction"] == "LONG" else "GREEN"
            maximum = setup["result_end_number"]
            if number > maximum: continue
            if current_color == wanted:
                send_telegram(self._message("win", setup["direction"], setup["rule"], setup["start_ts"],
                                            f"Ð ÐµÐ·ÑÐ»ÑÑÐ°ÑÐ½Ð° ÑÐ²ÑÑÐºÐ°: â{number}"))
                log.info("WIN | %s | rule=%s | %s | candle #%d",
                         self.symbol, setup["rule"], setup["direction"], number)
                continue
            if number == maximum:
                first_ts = setup["signal_ts"] + TIMEFRAME_SECONDS
                window = [self._color_at(first_ts + i * TIMEFRAME_SECONDS)
                          for i in range(maximum - setup["result_start_number"] + 1)]
                expected_count = maximum - setup["result_start_number"] + 1
                if len(window) == expected_count and all(c == opposite for c in window):
                    send_telegram(self._message("loss", setup["direction"], setup["rule"], setup["start_ts"],
                        f"Ð£ÑÑ ÑÐ²ÑÑÐºÐ¸ â{setup['result_start_number']}ââ{maximum} â {opposite}."))
                    log.info("LOSS | %s | rule=%s | %s", self.symbol, setup["rule"], setup["direction"])
                else:
                    log.info("NO STRICT WIN/LOSS | %s | rule=%s | Ñ Ð²ÑÐºÐ½Ñ Ñ DOJI Ð°Ð±Ð¾ Ð½ÐµÐ¿Ð¾Ð²Ð½Ñ Ð´Ð°Ð½Ñ",
                             self.symbol, setup["rule"])
                continue
            keep.append(setup)
        self.pending = keep

        # RULE 1: candle #9 has already had its early T-5 signal opportunity.
        # At its close, simply retire the waiting item; do not send a second signal.
        waiting_keep = []
        for item in self.rule1_waiting:
            expected_ts = item["warning_ts"] + TIMEFRAME_SECONDS
            if ts < expected_ts:
                waiting_keep.append(item)
            elif ts == expected_ts:
                already_signalled = any(
                    p.get("rule") == 1 and p.get("start_ts") == item["start_ts"]
                    for p in self.pending
                )
                if not already_signalled:
                    log.info("RULE 1 CANCEL | %s | candle #9 closed without an early signal",
                             self.symbol)
        self.rule1_waiting = waiting_keep

        # RULE 1 warning on candle #8 close â unchanged.
        start8_ts = ts - 7 * TIMEFRAME_SECONDS
        base8 = self._pattern_start(start8_ts)
        if base8 is not None:
            c7_color = self._color_at(start8_ts + 6 * TIMEFRAME_SECONDS)
            if c7_color == base8 and not any(p.get("start_ts") == start8_ts for p in self.rule1_waiting):
                direction = self._direction(base8)
                warning = self._message("warning", direction, 1, start8_ts,
                    "Ð¡Ð²ÑÑÐºÐ° â8 Ð·Ð°ÐºÑÐ¸Ð»Ð°ÑÑ. Ð¯ÐºÑÐ¾ ÑÐ²ÑÑÐºÐ° â9 Ð·Ð°ÐºÑÐ¸ÑÑÑÑÑ Ð¿Ð¾ÑÑÑÐ±Ð½Ð¸Ð¼ ÐºÐ¾Ð»ÑÐ¾ÑÐ¾Ð¼, Ð±ÑÐ´Ðµ Ð½Ð°Ð´ÑÑÐ»Ð°Ð½Ð¾ ÑÐ¸Ð³Ð½Ð°Ð».")
                if send_telegram(warning):
                    self.rule1_waiting.append({"start_ts": start8_ts, "base_color": base8, "warning_ts": ts})
                    log.info("PRE RULE 1 | %s | %s | start=%s",
                             self.symbol, direction, utc(start8_ts))

def main():
    if not SYMBOLS: raise RuntimeError("SYMBOLS Ð¿Ð¾ÑÐ¾Ð¶Ð½ÑÐ¹; Ð·Ð°Ð´Ð°Ð¹ÑÐµ BTCUSDT,ETHUSDT")
    log.info("ÐÐ°Ð¿ÑÑÐº Ð±Ð¾ÑÐ° MEXC native 5m | Ð¿ÐµÑÐµÐ²ÑÑÐºÐ° ÐºÐ¾Ð¶Ð½Ñ %s Ñ", POLL_SECONDS)
    log.info("ÐÐ¾Ð½ÐµÑÐ¸: %s", ", ".join(SYMBOLS))
    log.info("ÐÑÐ°Ð²Ð¸Ð»Ð¾ 1: PRE Ð¿ÑÑÐ»Ñ Ð·Ð°ÐºÑÐ¸ÑÑÑ #8; SIGNAL Ð·Ð° 5 Ñ Ð´Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ #9; ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ #10-#15")
    log.info("ÐÑÐ°Ð²Ð¸Ð»Ð¾ 2: #1-#3 Ð¾Ð´Ð½Ð°ÐºÐ¾Ð²Ñ, #4 Ð¿ÑÐ¾ÑÐ¸Ð»ÐµÐ¶Ð½Ð°, #5-#6 Ð±ÑÐ´Ñ-ÑÐºÑ, #7-#8 Ð¿ÑÐ¾ÑÐ¸Ð»ÐµÐ¶Ð½Ñ; PRE Ð½Ð° #8; SIGNAL Ð·Ð° 5 Ñ Ð´Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ #8; ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ #9-#14")
    log.info("Telegram Ð½Ð°Ð»Ð°ÑÑÐ¾Ð²Ð°Ð½Ð¾: %s | ÑÐ°ÑÑÐ²: %d", bool(TOKEN), len(CHAT_IDS))
    if TOKEN and CHAT_IDS:
        send_telegram(f"ð¤ ÐÐÐ¢ ÐÐÐÐ£Ð©ÐÐÐ\nMEXC USDT Perpetual (native 5m)\n"
                      f"ÐÐ¾Ð½ÐµÑÐ¸: {', '.join(symbol_label(s) for s in SYMBOLS)}\n"
                      f"ÐÐ½ÑÐµÑÐ²Ð°Ð»: {TIMEFRAME_LABEL}\nÐÐ°Ð´ÑÐ¸Ð»Ð°ÑÑÑÑÑ Ð»Ð¸ÑÐµ ÑÐ¸Ð³Ð½Ð°Ð»Ð¸; Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐ½Ñ ÑÐ³Ð¾Ð´Ð¸ Ð½Ðµ Ð²Ð¸ÐºÐ¾Ð½ÑÑÑÑÑÑ.")
    engines = {s: SignalEngine(s) for s in SYMBOLS}
    last_heartbeat = {}
    while True:
        for symbol, engine in engines.items():
            try:
                closed, live = fetch_5m_candles(symbol)
                if live:
                    engine.maybe_pre_alert(live)
                    engine.maybe_rule1_signal(live)
                    engine.maybe_rule2_signal(live)
                if not closed:
                    log.warning("%s | MEXC Ð¿Ð¾ÐºÐ¸ Ð½Ðµ Ð¿Ð¾Ð²ÐµÑÐ½ÑÐ² Ð·Ð°ÐºÑÐ¸ÑÐ¸Ñ ÑÐ²ÑÑÐ¾Ðº %s", symbol, TIMEFRAME_LABEL)
                    continue
                added = engine.ingest(closed)
                latest = closed[-1]
                if added:
                    log.info("ÐÐÐÐ ÐÐÐÐ | %s | Ð¾ÑÑÐ¸Ð¼Ð°Ð½Ð¾=%d | Ð´Ð¾Ð´Ð°Ð½Ð¾=%d | Ð¾ÑÑÐ°Ð½Ð½Ñ=%s %s | open=%s close=%s | pending=%d",
                             symbol, len(closed), added, utc(latest["ts"]), candle_color(latest),
                             latest["open"], latest["close"], len(engine.pending))
                elif time.time() - last_heartbeat.get(symbol, 0) >= 60:
                    age = max(0, int(time.time() - (latest["ts"] + TIMEFRAME_SECONDS)))
                    log.info("ÐÐ'Ð¯ÐÐÐ Ð | %s | Ð¾ÑÑÐ°Ð½Ð½Ñ=%s %s | Ð·Ð°ÑÑÐ¸Ð¼ÐºÐ°=%s Ñ | pending=%d",
                             symbol, utc(latest["ts"]), candle_color(latest), age, len(engine.pending))
                    last_heartbeat[symbol] = time.time()
            except Exception as exc:
                log.exception("ÐÐÐÐÐÐÐ Ð¦ÐÐÐÐ£ | %s | %s", symbol, exc)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    if TEST_MODE:
        print("Ð¢ÐÐ¡Ð¢ÐÐÐÐ Ð ÐÐÐÐ: ÐÐÐ ÐÐÐÐ ÐÐ£ ÐÐ ÐÐÐÐÐÐ")
        print("ÐÐ¾Ð½ÐµÑÐ¸:", ", ".join(SYMBOLS))
        print("ÐÐ½ÑÐµÑÐ²Ð°Ð»:", TIMEFRAME_LABEL, "(Ð½Ð°ÑÐ¸Ð²Ð½Ñ 5m ÑÐ²ÑÑÐºÐ¸)")
        print("ÐÐ¶ÐµÑÐµÐ»Ð¾: MEXC USDT Perpetual native 5m; API:", API_URL)
        print("ÐÑÐ°Ð²Ð¸Ð»Ð¾ 1: PRE Ð¿ÑÑÐ»Ñ Ð·Ð°ÐºÑÐ¸ÑÑÑ #8; SIGNAL Ð·Ð° 5 Ñ Ð´Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ #9; ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ #10â#15")
        print("ÐÑÐ°Ð²Ð¸Ð»Ð¾ 2: PRE Ð½Ð° Ð¿Ð¾ÑÐ°ÑÐºÑ #8; SIGNAL Ð·Ð° 5 ÑÐµÐºÑÐ½Ð´ Ð´Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ #8; ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ #9â#14")
    else:
        main()
